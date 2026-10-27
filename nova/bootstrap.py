"""Database bootstrap — ADDITIVE-ONLY, idempotent, NON-DESTRUCTIVE.

Data-safety contract (the "git push wiped my data" fix):
  1. Missing tables are created via `create_all` — purely additive schema sync
     (works identically on SQLite and Postgres). Existing columns/rows are
     never dropped or altered destructively.
  2. An EMPTY database gets the real minimum seeded once: the built-in
     RouterX Free engine (provider + its model catalogue: the `nova/*` tiers plus
     every free model from ClawLabsAI/free-ai-models), `gateway_started_at`,
     and the real free-GPU service catalogue. Nothing fake.
  3. A NON-EMPTY database is never cleaned, purged or overwritten — not at
     boot, not after a deploy, not ever. Providers, models, keys, routes,
     configs, logs and files survive every restart and every `git push` (as
     long as DATABASE_URL points at a persistent store). The only writes to an
     existing database are strictly ADD-ONLY: missing SystemConfig keys, missing
     built-in engine models, and missing rows of the OmniRoute provider/model
     catalogue are filled in, exactly like an additive schema migration.
     Existing rows are never modified or deleted.
"""
from __future__ import annotations

import json
import logging
import os

from sqlalchemy import (
    Boolean, DateTime, Float, Integer, Numeric, String, Text, func, inspect, select, text,
)
from sqlalchemy.orm import Session

from .catalog import providers as catalog_providers
from .database import engine
from .freemodels import nova_engine_models
from .kv import get_config, set_config, set_config_json
from .models import (
    ALL_TABLES,
    Base,
    Model,
    Provider,
    StorageProviderRow,
)

log = logging.getLogger("nova.bootstrap")

# The built-in engine's model catalogue: the `nova/*` tiers plus the free
# models tracked by ClawLabsAI/free-ai-models (engine/free-models.json).
NOVA_ENGINE_MODELS = nova_engine_models()

# Real free-GPU service catalogue (real providers, real free tiers) — same
# nature as the provider preset catalogue: configuration, not telemetry.
GPU_CATALOGUE = [
    {"id": "novafree_gpu", "name": "RouterX Free Compute", "gpu": "shared", "vram_gb": 8,
     "free_tier": "Built-in gateway compute", "status": "available", "connected": True,
     "hours_free": "unlimited", "region": "global", "note": "Built-in pool used by the RouterX Free engine."},
    {"id": "colab", "name": "Google Colab", "gpu": "T4 16GB", "vram_gb": 16,
     "free_tier": "Free tier GPU sessions", "status": "available", "connected": False,
     "hours_free": "~4h sessions", "region": "us/eu", "note": "Attach via notebook + ngrok bridge."},
    {"id": "kaggle", "name": "Kaggle Notebooks", "gpu": "T4 x2 / P100", "vram_gb": 16,
     "free_tier": "30h/week free GPU", "status": "available", "connected": False,
     "hours_free": "30h/week", "region": "us", "note": "Attach via kernel + API bridge."},
    {"id": "lightning", "name": "Lightning AI", "gpu": "T4 16GB", "vram_gb": 16,
     "free_tier": "22 free GPU hours monthly", "status": "available", "connected": False,
     "hours_free": "22h/month", "region": "us/eu", "note": "Free studio credits on signup."},
    {"id": "paperspace", "name": "Paperspace Gradient", "gpu": "M4000 / Free GPU", "vram_gb": 8,
     "free_tier": "Free GPU notebooks", "status": "available", "connected": False,
     "hours_free": "6h sessions", "region": "us/eu", "note": "Free tier notebooks with periodic availability."},
]


def create_schema() -> None:
    """Create missing tables only — never drops, never destroys existing data."""
    Base.metadata.create_all(bind=engine)


def sync_schema_columns() -> list[str]:
    """Add columns that exist in `nova.models` but not in an existing table.

    `create_all` creates missing TABLES but never ALTERs an existing one. An
    upgraded deployment could therefore keep an old `ModelRoute` table without
    `.auto` or a `Provider` without `.kind`: `/v1/models` still works (it only
    reads `Provider`/`Model`), while every inference request fails with a DB
    error — exactly the "discovery works, inference returns 500" symptom.
    This is the additive half of the schema sync: missing columns are added with
    a constant default; no row is read, rewritten or deleted.
    """
    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        # Case-insensitive: PostgreSQL folds unquoted identifiers, and a
        # case-only difference must never be treated as a missing column (that
        # would add a duplicate "baseUrl" next to an existing "baseurl").
        existing = {str(col["name"]).lower() for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name.lower() in existing:
                continue
            ddl = _add_column_sql(table.name, column)
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception as err:  # noqa: BLE001 — boot must never fail on one column
                log.warning("schema sync: could not add %s.%s: %s", table.name, column.name, err)
                continue
            added.append(f"{table.name}.{column.name}")
            log.warning("schema sync: added missing column %s.%s (additive, no rows touched)",
                        table.name, column.name)
    return added


def _add_column_sql(table_name: str, column) -> str:
    preparer = engine.dialect.identifier_preparer
    col_type = column.type.compile(dialect=engine.dialect)
    ddl = (f"ALTER TABLE {preparer.quote(table_name)} "
           f"ADD COLUMN {preparer.quote(column.name)} {col_type}")
    default = _column_default_sql(column)
    return f"{ddl} DEFAULT {default}" if default is not None else ddl


def _column_default_sql(column) -> str | None:
    """Constant SQL default for a newly added column.

    NOT NULL is deliberately omitted: old rows must stay valid, and SQLite
    refuses non-constant defaults in ALTER TABLE (CURRENT_TIMESTAMP). A constant
    default backfills existing NULL-free reads and new writes still come from
    the model's Python default.
    """
    server_default = column.server_default
    if server_default is not None:
        arg = getattr(server_default, "arg", None)
        if isinstance(arg, str):
            return arg
        arg_text = getattr(arg, "text", None)
        if isinstance(arg_text, str):
            return arg_text
    default = column.default
    if default is not None and not getattr(default, "is_callable", False):
        value = getattr(default, "arg", None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
    if isinstance(column.type, Boolean):
        return "FALSE"
    if isinstance(column.type, (Integer, Float, Numeric)):
        return "0"
    if isinstance(column.type, DateTime):
        return "'1970-01-01 00:00:00'"
    if isinstance(column.type, (String, Text)):
        return "''"
    return None


def bootstrap_minimum(db: Session) -> None:
    """Seed the real minimum into an empty database. Additive-only:
    existing rows (user providers, keys, configs, logs…) are never touched."""
    provider_count = db.scalar(select(func.count()).select_from(Provider)) or 0
    if provider_count == 0:
        log.info("empty database — bootstrapping the real minimum…")
        novafree = Provider(
            key="novafree", name="RouterX Free Engine", kind="builtin",
            baseUrl="internal://nova-engine", prefix="nova/", priority=1,
            color="#10b981",
            freeTier="Built-in — every model here is free, no key required",
            docsUrl="https://github.com/artishade/Novarouter",
        )
        db.add(novafree)
        db.flush()
        seeded = ensure_engine_models(db, novafree)
        log.info("bootstrapped: RouterX Free Engine (%s free models)", seeded)
    else:
        log.info("database intact — %s provider(s) found, nothing removed "
                 "(boot is additive-only; deploys never wipe data)", provider_count)

    # Missing-but-expected config keys are filled in (add-only, never overwritten).
    if get_config(db, "gateway_started_at") is None:
        set_config(db, "gateway_started_at", str(_now_ms()))

    # Existing deployments: top up the built-in engine catalogue additively so
    # the free-ai-models base reaches databases seeded before this change.
    if provider_count != 0:
        provider = db.scalars(select(Provider).where(Provider.key == "novafree")).first()
        if provider is not None:
            added = ensure_engine_models(db, provider)
            if added:
                log.info("engine catalogue topped up: +%s free model(s) added", added)

    # Storage catalogue: only when the table is completely empty.
    if not db.scalar(select(func.count()).select_from(StorageProviderRow)):
        _seed_gpu_catalogue(db)

    # OmniRoute catalogue: every routable provider + its active models. Strictly
    # additive — providers the operator already has keep their own base URL,
    # prefix, priority and colour, and models are only ever inserted, never
    # updated or removed. Seeded providers start disabled: they have no key yet.
    if get_config(db, "seed_registry_models") != "off" and os.environ.get("NOVA_SEED_REGISTRY") != "off":
        seeded = ensure_registry_catalogue(db)
        if seeded["providers"] or seeded["models"]:
            log.info("OmniRoute catalogue: +%s provider(s), +%s model(s)",
                     seeded["providers"], seeded["models"])


def ensure_registry_catalogue(db: Session) -> dict[str, int]:
    """ADDITIVE-ONLY: materialise the OmniRoute provider/model catalogue.

    Never updates, reorders or deletes an existing row. A provider the operator
    already configured (matching key) is left exactly as it is; only its
    missing models are added, using that provider's own prefix.
    """
    by_key: dict[str, Provider] = {p.key: p for p in db.scalars(select(Provider)).all()}
    known_models = {
        (m.providerId, m.modelId)
        for m in db.scalars(select(Model)).all()
    }

    added_providers = 0
    added_models = 0

    for entry in catalog_providers():
        key = entry["key"]
        provider = by_key.get(key)

        if provider is None:
            provider = Provider(
                key=key,
                name=entry["name"],
                kind=entry["kind"],
                baseUrl=entry["base_url"] or "",
                prefix=f"{key}/",
                enabled=False,          # no key yet — an operator turns it on
                priority=int(entry.get("priority", 100)),
                color=entry.get("color") or "#10b981",
                requiresAuth=bool(entry.get("requires_key", True)),
                docsUrl=entry.get("models_url") or None,
            )
            db.add(provider)
            db.flush()
            by_key[key] = provider
            added_providers += 1

        prefix = provider.prefix or f"{key}/"
        for model in entry["models"]:
            if (provider.id, model["id"]) in known_models:
                continue
            db.add(Model(
                providerId=provider.id,
                modelId=model["id"],
                exposedId=f"{prefix}{model['id']}",
                displayName=model.get("name") or model["id"],
                isFree=False,  # unpriced until a sync or the operator says otherwise
                contextLength=int(model.get("ctx", 0) or 0),
                maxOutput=int(model.get("max_out", 0) or 0),
                capabilities=json.dumps({
                    "tools": bool(model.get("caps", {}).get("tools")),
                    "vision": bool(model.get("caps", {}).get("vision")),
                    "reasoning": bool(model.get("caps", {}).get("reasoning")),
                }),
                description=f"{entry['name']} · from the OmniRoute provider registry",
            ))
            known_models.add((provider.id, model["id"]))
            added_models += 1

    if added_providers or added_models:
        db.commit()
    return {"providers": added_providers, "models": added_models}


def ensure_engine_models(db: Session, provider: Provider) -> int:
    """ADDITIVE-ONLY: insert engine models the provider is missing.

    Never updates, reorders or deletes an existing row — a model the user
    disabled, renamed or re-priced stays exactly as they left it.
    """
    existing = {
        row.modelId
        for row in db.scalars(select(Model).where(Model.providerId == provider.id)).all()
    }
    added = 0
    for m in nova_engine_models():
        if m["modelId"] in existing:
            continue
        db.add(Model(
            providerId=provider.id, modelId=m["modelId"], exposedId=m["exposedId"],
            displayName=m["displayName"], isFree=True,
            contextLength=m["ctx"], maxOutput=m["maxOut"],
            capabilities=_json_caps(m["caps"]), description=m["description"],
        ))
        existing.add(m["modelId"])
        added += 1
    if added:
        db.commit()
    return added


def _json_caps(caps: dict) -> str:
    return json.dumps({"tools": False, "vision": False, "reasoning": False, **caps})


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _seed_gpu_catalogue(db: Session) -> None:
    db.add(StorageProviderRow(
        id="local_disk", name="Local Disk", type="local", status="connected",
        isBuiltin=True, active=True, freeTier="Uses the server's own disk",
        quotaMb=0, region="local", docsUrl=None, authUrl=None,
    ))
    set_config_json(db, "gpu_providers", GPU_CATALOGUE)
    db.commit()


def run_bootstrap() -> None:
    create_schema()
    sync_schema_columns()
    with Session(engine) as db:
        bootstrap_minimum(db)


__all__ = ["run_bootstrap", "create_schema", "sync_schema_columns",
           "ensure_registry_catalogue", "ALL_TABLES"]
