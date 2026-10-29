"""Admin client keys — list, mint, update, revoke.

Python port of:
  src/app/api/admin/client-keys/route.ts       (GET + POST)
  src/app/api/admin/client-keys/[id]/route.ts  (PATCH + DELETE)

Mounted at /api/admin/client-keys by main.py.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from nova.database import get_db
from nova.models import ClientKey

from ._common import epoch_ms, invalid_id, not_found, parse_int

router = APIRouter()


def _num(v: Any, dflt: int) -> int:
    """TS num(): finite number/numeric-string → max(0, round(n)), else default."""
    n = _finite(v)
    return max(0, round(n)) if n is not None else dflt


def _finite(v: Any) -> float | None:
    """TS `typeof v === 'number' ? v : Number(v)` → finite value or None."""
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #

def to_client_key(k: ClientKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "token": k.token,
        "enabled": k.enabled,
        "allowed_models": k.allowedModels,
        "trading_scopes": k.tradingScopes or "",
        "rpm_limit": k.rpmLimit,
        "tpd_limit": k.tpdLimit,
        "tokens_in": k.tokensIn,
        "tokens_out": k.tokensOut,
        "req_count": k.reqCount,
        "last_used_at": epoch_ms(k.lastUsedAt),
        "created_at": epoch_ms(k.createdAt),
    }


# --------------------------------------------------------------------------- #
# GET /api/admin/client-keys — dashboard client keys, descending by created_at
# --------------------------------------------------------------------------- #

@router.get("")
def list_client_keys(db: Session = Depends(get_db)):
    rows = db.scalars(select(ClientKey).order_by(ClientKey.createdAt.desc(), ClientKey.id.desc())).all()
    return JSONResponse([to_client_key(k) for k in rows])


# --------------------------------------------------------------------------- #
# POST /api/admin/client-keys — mint a new client key
# --------------------------------------------------------------------------- #

@router.post("")
def create_client_key(body: dict = Body(default={}), db: Session = Depends(get_db)):
    name = body["name"].strip() if isinstance(body.get("name"), str) else ""
    if not name:
        return JSONResponse({"error": "Client key name is required"}, status_code=400)

    token = f"nova-sk-{(uuid.uuid4().hex + uuid.uuid4().hex).replace('-', '')[:32]}"

    allowed_models = body.get("allowed_models")
    trading_scopes = body.get("trading_scopes")
    created = ClientKey(
        name=name,
        token=token,
        allowedModels=allowed_models.strip()
        if isinstance(allowed_models, str) and allowed_models.strip() != "" else "*",
        tradingScopes=trading_scopes.strip() if isinstance(trading_scopes, str) else "",
        rpmLimit=_num(body.get("rpm_limit"), 60),
        tpdLimit=_num(body.get("tpd_limit"), 0),
    )
    db.add(created)
    db.commit()

    return JSONResponse(to_client_key(created))


# --------------------------------------------------------------------------- #
# PATCH /api/admin/client-keys/{id} — update a client key
# --------------------------------------------------------------------------- #

@router.patch("/{id}")
def update_client_key(id: str, body: dict = Body(default={}), db: Session = Depends(get_db)):
    key_id = parse_int(id)
    if key_id is None:
        return invalid_id("Invalid client key id")

    existing = db.get(ClientKey, key_id)
    if existing is None:
        return not_found("Client key not found")

    if isinstance(body.get("enabled"), bool):
        existing.enabled = body["enabled"]
    if isinstance(body.get("name"), str) and body["name"].strip() != "":
        existing.name = body["name"].strip()
    if body.get("rpm_limit") is not None:
        n = _finite(body.get("rpm_limit"))
        if n is not None:
            existing.rpmLimit = max(0, round(n))
    if body.get("tpd_limit") is not None:
        n = _finite(body.get("tpd_limit"))
        if n is not None:
            existing.tpdLimit = max(0, round(n))
    if isinstance(body.get("allowed_models"), str) and body["allowed_models"].strip() != "":
        existing.allowedModels = body["allowed_models"].strip()
    if "trading_scopes" in body and isinstance(body["trading_scopes"], str):
        existing.tradingScopes = body["trading_scopes"].strip()

    db.commit()

    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# DELETE /api/admin/client-keys/{id} — revoke a client key
# --------------------------------------------------------------------------- #

@router.delete("/{id}")
def delete_client_key(id: str, db: Session = Depends(get_db)):
    key_id = parse_int(id)
    if key_id is None:
        return invalid_id("Invalid client key id")

    existing = db.get(ClientKey, key_id)
    if existing is None:
        return not_found("Client key not found")

    db.delete(existing)
    db.commit()

    return JSONResponse({"ok": True})
