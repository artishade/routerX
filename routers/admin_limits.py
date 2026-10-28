"""Admin surface for the gateway's limits + cache (all JSON, snake_case).

  GET  /api/admin/usage             per-provider and per-(provider, model) request/
                                    token counts for the current minute + day, the
                                    caps in effect, and upstream key cooldowns
  GET  /api/admin/rate-limits       the `rate_limits` knob + env defaults
  POST /api/admin/rate-limits       replace the knob (per provider override)
  GET  /api/admin/cache/stats       response cache counters + configuration
  POST /api/admin/cache/clear       empty the response cache
  GET  /api/admin/sessions          live sticky-session pins (diagnostics)

Reads are cheap. Writes go through `nova.ratelimit.save_limits`, which is the
only writer of the knob, so a malformed payload can only be rejected, never
half-applied. No API key material is ever echoed: key rows are masked with
`mask_key()`.

Mounted by `main.py` under `/api/admin`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from nova import auth as nova_auth
from nova import cache as nova_cache
from nova import ratelimit as nova_ratelimit
from nova import session as nova_session
from nova.config import (
    DEFAULT_RPD, DEFAULT_RPM, DEFAULT_TPD, DEFAULT_TPM, REQUIRE_API_KEY,
)
from nova.database import get_db
from nova.models import ClientKey, Provider, ProviderKey
from ._common import epoch_ms, mask_key

router = APIRouter()


async def _body(request: Request) -> dict:
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# GET /usage
# --------------------------------------------------------------------------- #

@router.get("/usage")
def usage(db: Session = Depends(get_db)):
    """Today's intake per provider / per (provider, model), plus live caps.

    Totals come from `RequestLog` (the durable record, one row per request);
    the current minute/day windows come from `UsageWindow` (what the caps are
    enforced against). Both are reported so the UI can show intake and pressure.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from nova.models import RequestLog, utcnow

    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    providers = {p.id: p for p in db.scalars(select(Provider)).all()}

    day_rows = db.execute(
        select(RequestLog.providerName, RequestLog.status,
               func.count(), func.coalesce(func.sum(RequestLog.tokensIn), 0),
               func.coalesce(func.sum(RequestLog.tokensOut), 0))
        .where(RequestLog.ts >= day_start, RequestLog.providerName != "")
        .group_by(RequestLog.providerName, RequestLog.status)
    ).all()
    per_provider: dict[str, dict[str, Any]] = {}
    for name, status, count, tin, tout in day_rows:
        slot = per_provider.setdefault(str(name), {"requests": 0, "tokens_in": 0,
                                                   "tokens_out": 0, "errors": 0})
        slot["requests"] += int(count or 0)
        slot["tokens_in"] += int(tin or 0)
        slot["tokens_out"] += int(tout or 0)
        if int(status) >= 400:
            slot["errors"] += int(count or 0)

    knob = nova_ratelimit.load_limits(db)
    env_defaults = {"rpm": DEFAULT_RPM, "tpm": DEFAULT_TPM, "rpd": DEFAULT_RPD, "tpd": DEFAULT_TPD}

    providers_out = []
    for pid, provider in providers.items():
        stats = per_provider.get(provider.name, {"requests": 0, "tokens_in": 0,
                                                 "tokens_out": 0, "errors": 0})
        override = {**knob.get("*", {}), **knob.get(provider.key, {})} if knob else {}
        limits = nova_ratelimit.limits_from(override, env_defaults)
        providers_out.append({
            "provider_id": pid,
            "provider": provider.key,
            "name": provider.name,
            **stats,
            "tokens": stats["tokens_in"] + stats["tokens_out"],
            "limits": limits,
            "capped": any(limits.values()),
        })
    providers_out.sort(key=lambda p: (-p["requests"], p["provider_id"]))

    keys = []
    for k in db.scalars(select(ProviderKey).order_by(ProviderKey.providerId, ProviderKey.id)).all():
        provider = providers.get(k.providerId)
        keys.append({
            "id": k.id,
            "provider_id": k.providerId,
            "provider": provider.key if provider else "",
            "label": k.label,
            "api_key_preview": mask_key(k.apiKey),
            "enabled": k.enabled,
            "cooling": nova_ratelimit.is_cooling(k),
            "cooldown_until": epoch_ms(k.cooldownUntil),
            "last_error": k.lastError,
            "req_count": k.reqCount,
            "err_count": k.errCount,
            "last_used_at": epoch_ms(k.lastUsedAt),
        })

    models = []
    for row in nova_ratelimit.usage_snapshot(db):
        provider = providers.get(row["provider_id"])
        models.append({
            "provider_id": row["provider_id"],
            "provider": provider.key if provider else "",
            "model": row["model"],
            "minute": row["minute"],
            "day": row["day"],
        })

    return JSONResponse({
        "generated_at": epoch_ms(now),
        "window": {"minute_start": epoch_ms(now - timedelta(seconds=now.second, microseconds=now.microsecond)),
                   "day_start": epoch_ms(day_start)},
        "env_defaults": env_defaults,
        "rate_limits": knob,
        "providers": providers_out,
        "models": models,
        "upstream_keys": keys,
        "client_keys": _client_keys(db, day_start),
        "warning": None if not _auth_risk(db) else
        ("Client keys exist: requests without a valid key are now refused. "
         "Send 'Authorization: Bearer <token>'."),
    })


def _auth_risk(db: Session) -> bool:
    return nova_auth.has_enabled_keys(db) or REQUIRE_API_KEY


def _client_keys(db: Session, day_start) -> list[dict]:
    """Per-client-key intake for today (usage is attributed by the gateway)."""
    from sqlalchemy import func

    from nova.models import RequestLog

    rows = db.execute(
        select(RequestLog.clientKeyId, func.count(), func.coalesce(func.sum(RequestLog.tokensIn), 0),
               func.coalesce(func.sum(RequestLog.tokensOut), 0))
        .where(RequestLog.ts >= day_start, RequestLog.clientKeyId.is_not(None))
        .group_by(RequestLog.clientKeyId)
    ).all()
    per_key = {int(k): (int(c or 0), int(i or 0), int(o or 0)) for k, c, i, o in rows}
    out = []
    for key in db.scalars(select(ClientKey).order_by(ClientKey.id)).all():
        reqs, tin, tout = per_key.get(int(key.id), (0, 0, 0))
        out.append({
            "id": key.id,
            "name": key.name,
            "enabled": key.enabled,
            "allowed_models": key.allowedModels,
            "rpm_limit": key.rpmLimit,
            "rpm_used": nova_auth.RPM.used(int(key.id)),
            "tpd_limit": key.tpdLimit,
            "tokens_today": tin + tout,
            "tokens_in": tin,
            "tokens_out": tout,
            "requests_today": reqs,
            "req_count": key.reqCount,
            "last_used_at": epoch_ms(key.lastUsedAt),
        })
    return out


# --------------------------------------------------------------------------- #
# GET / POST /rate-limits
# --------------------------------------------------------------------------- #

@router.get("/rate-limits")
def get_rate_limits(db: Session = Depends(get_db)):
    return JSONResponse({
        "rate_limits": nova_ratelimit.load_limits(db),
        "env_defaults": {"rpm": DEFAULT_RPM, "tpm": DEFAULT_TPM,
                         "rpd": DEFAULT_RPD, "tpd": DEFAULT_TPD},
        "keys": list(nova_ratelimit.LIMIT_KEYS),
        "note": "0 or missing means unlimited; '*' applies to every provider.",
    })


@router.post("/rate-limits")
async def post_rate_limits(request: Request, db: Session = Depends(get_db)):
    body = await _body(request)
    raw = body.get("rate_limits", body.get("limits", body))
    if not isinstance(raw, dict):
        return JSONResponse({"error": "rate_limits must be an object of provider overrides"},
                            status_code=400)
    bad = [k for k, v in raw.items() if not isinstance(v, dict)]
    if bad:
        return JSONResponse({"error": f"Overrides must be objects: {', '.join(map(str, bad))}"},
                            status_code=400)
    saved = nova_ratelimit.save_limits(db, raw)
    return JSONResponse({"ok": True, "rate_limits": saved})


# --------------------------------------------------------------------------- #
# GET /cache/stats, POST /cache/clear
# --------------------------------------------------------------------------- #

@router.get("/cache/stats")
def cache_stats(db: Session = Depends(get_db)):
    settings = nova_cache.settings(db)
    return JSONResponse({**nova_cache.stats(), "enabled": settings["enabled"],
                         "configured_ttl_s": settings["ttl"],
                         "configured_max_entries": settings["max_entries"]})


@router.post("/cache/clear")
def cache_clear():
    return JSONResponse({"ok": True, "cleared": nova_cache.clear()})


# --------------------------------------------------------------------------- #
# GET /sessions - live sticky pins
# --------------------------------------------------------------------------- #

@router.get("/sessions")
def sessions(db: Session = Depends(get_db)):
    return JSONResponse({
        "ttl_s": nova_session.SESSIONS.ttl_s,
        "pins": [p.to_dict() for p in getattr(nova_session.SESSIONS, "_pins", {}).values()],
    })


__all__ = ["router"]
