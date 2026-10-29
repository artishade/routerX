"""Live model catalogue sync — RouterX catalogue sync.

Shared by:
  • POST /api/admin/models/sync            (manual "Sync models" button)
  • POST /api/admin/providers              (auto-sync right after a provider is added)

Queries each provider's REAL upstream /models endpoint (no static catalogue)
and upserts what is actually available: real ids, context windows, pricing,
free flags. Failures are reported per-provider and never fabricated.
"""
from __future__ import annotations

import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .discovery import discover_provider_fresh
from .freemodels import nova_engine_models
from .models import Model, Provider

# Hard cap per provider — protects the DB from pathological upstream catalogues.
MAX_MODELS_PER_PROVIDER = 500

# Built-in RouterX Free engine catalogue: the `nova/*` tiers plus every free model
# in the bundled catalogue (engine/free-models.json). Resolved per call so
# a refreshed snapshot is picked up without a restart.
def NOVA_ENGINE_MODELS() -> list[dict]:
    return nova_engine_models()


def _default_capabilities(caps: dict | None = None) -> str:
    return json.dumps({"tools": False, "vision": False, "reasoning": False, **(caps or {})})


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _num_or_zero(value) -> float:
    return value if value is not None else 0


# Detailed per-model log lines are capped to keep the live panel readable
# (giant catalogues still sync fully — only the printout is capped).
LOG_DETAIL_CAP = 60


def _noop_log(_msg: str) -> None:
    return None


async def sync_provider(db: Session, provider: dict, log=None) -> dict:
    """Sync ONE provider from its live upstream catalogue (real network calls).

    `provider` carries the Prisma-shaped fields: id, key, name, kind, baseUrl, prefix.
    `log` (optional) receives human-readable progress lines for the live panel.
    Returns a ProviderSyncReport dict: {provider, provider_id, ok, discovered,
    created, updated, error?, duration_ms}.
    """
    log = log or _noop_log
    started = time.monotonic()
    base = {"provider": provider["key"], "provider_id": provider["id"]}

    # ── Exchange providers: no model catalogue, no discovery, no Model rows ──
    if provider["kind"] == "exchange":
        log("exchange provider — no model catalogue to sync (skipped)")
        return {**base, "ok": True, "discovered": 0, "created": 0, "updated": 0,
                "skipped": True, "message": "exchange providers do not expose a model catalogue",
                "duration_ms": _ms(started)}

    # ── Built-in engine: catalogue is the engine's real surface, no network ──
    if provider["kind"] == "builtin":
        log("built-in RouterX Free engine — refreshing the engine catalogue (no network)…")
        catalogue = NOVA_ENGINE_MODELS()
        created = 0
        updated = 0
        for entry in catalogue:
            existing = db.scalars(
                select(Model).where(
                    Model.providerId == provider["id"], Model.modelId == entry["modelId"]
                )
            ).first()
            if existing is not None:
                existing.displayName = entry["displayName"]
                existing.contextLength = entry["ctx"]
                existing.maxOutput = entry["maxOut"]
                existing.capabilities = _default_capabilities(entry["caps"])
                existing.description = entry["description"]
                updated += 1
                log(f"  · {entry['exposedId']} refreshed")
            else:
                db.add(Model(
                    providerId=provider["id"], modelId=entry["modelId"],
                    exposedId=entry["exposedId"], displayName=entry["displayName"],
                    isFree=True, contextLength=entry["ctx"], maxOutput=entry["maxOut"],
                    capabilities=_default_capabilities(entry["caps"]),
                    description=entry["description"],
                ))
                created += 1
                log(f"  + {entry['exposedId']} added ({entry['ctx']} ctx)")
        db.commit()
        log(f"engine catalogue up to date — {created} added, {updated} refreshed")
        return {**base, "ok": True, "discovered": len(catalogue),
                "created": created, "updated": updated, "duration_ms": _ms(started)}

    # ── Everything else: live discovery against the real upstream /models endpoint ──
    log(f"querying live catalogue for “{provider['key']}” at {provider.get('baseUrl') or '(no base URL)'}…")
    try:
        result = await discover_provider_fresh(db, provider["id"], log=log)
        if not result.get("ok"):
            db.rollback()
            error = result.get("error") or "discovery failed"
            log(f"discovery failed — {error}")
            return {**base, "ok": False, "discovered": 0, "created": 0, "updated": 0,
                    "error": error, "duration_ms": _ms(started)}

        discovered = result["models"][:MAX_MODELS_PER_PROVIDER]
        if result["count"] > len(discovered):
            log(f"upstream lists {result['count']} models — capping at {MAX_MODELS_PER_PROVIDER}")
        log(f"discovered {len(discovered)} model(s) — comparing with the database…")
        existing_rows = {
            row.modelId: row
            for row in db.scalars(select(Model).where(Model.providerId == provider["id"])).all()
        }

        created = 0
        updated = 0
        logged = 0
        prefix = provider.get("prefix") or ""

        for m in discovered:
            mid = m["id"]
            data = {
                "displayName": m.get("display_name") or mid,
                "isFree": bool(m.get("is_free")),
                "contextLength": int(m.get("context_length") or 0),
                "maxOutput": int(m.get("max_output") or 0),
                "priceIn": _num_or_zero(m.get("pricing_prompt")),
                "priceOut": _num_or_zero(m.get("pricing_completion")),
            }
            row = existing_rows.get(mid)
            if row is not None:
                row.displayName = data["displayName"]
                row.isFree = data["isFree"]
                row.contextLength = data["contextLength"]
                row.maxOutput = data["maxOutput"]
                row.priceIn = data["priceIn"]
                row.priceOut = data["priceOut"]
                updated += 1
                if logged < LOG_DETAIL_CAP:
                    log(f"  · {prefix}{mid} refreshed")
                    logged += 1
            else:
                obj = Model(
                    providerId=provider["id"], modelId=mid,
                    exposedId=f"{prefix}{mid}", status="unknown",
                    capabilities=_default_capabilities(), description=None,
                    **data,
                )
                db.add(obj)
                db.flush()  # assign the id now so a duplicate id in this batch updates
                existing_rows[mid] = obj
                created += 1
                if logged < LOG_DETAIL_CAP:
                    free = " (free)" if data["isFree"] else ""
                    ctx = f" · {data['contextLength']} ctx" if data["contextLength"] else ""
                    log(f"  + {prefix}{mid}{free}{ctx}")
                    logged += 1
        if logged >= LOG_DETAIL_CAP:
            log(f"  … (+{created + updated - logged} more not printed)")
        db.commit()
        log(f"catalogue saved — {created} new, {updated} refreshed ({_ms(started)}ms)")
    except Exception as err:  # a failing provider must never break the whole sync
        db.rollback()
        log(f"sync error — {str(err)[:160]}")
        return {**base, "ok": False, "discovered": 0, "created": 0, "updated": 0,
                "error": str(err)[:200], "duration_ms": _ms(started)}

    return {**base, "ok": True, "discovered": result["count"],
            "created": created, "updated": updated, "duration_ms": _ms(started)}


async def sync_providers(db: Session, provider_id: int | None = None, log=None) -> list[dict]:
    """Sync enabled providers sequentially (optionally just one); returns reports."""
    stmt = (
        select(Provider)
        .where(Provider.enabled.is_(True))
        .order_by(Provider.priority.asc(), Provider.id.asc())
    )
    if provider_id is not None:
        stmt = stmt.where(Provider.id == provider_id)
    providers = db.scalars(stmt).all()

    reports: list[dict] = []
    for p in providers:
        reports.append(await sync_provider(db, {
            "id": p.id, "key": p.key, "name": p.name,
            "kind": p.kind, "baseUrl": p.baseUrl, "prefix": p.prefix,
        }, log=log))
    return reports
