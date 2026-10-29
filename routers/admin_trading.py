"""Admin surface for the trading subsystem (Phase 0).

  GET  /api/admin/trading          kill-switch state + exchange provider count
  POST /api/admin/trading          engage / release the kill-switch (audited)
  GET  /api/admin/trading/audit    newest-first slice of the append-only trail

The audit trail is exposed read-only on purpose: no update or delete route
exists, and `nova.trading_audit` additionally blocks ORM mutation of the rows.
Mounted by `main.py` under `/api/admin`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nova import trading_audit, trading_settings
from nova.database import get_db
from nova.models import Provider, TradeAuditLog

router = APIRouter()

AUDIT_LIMIT_DEFAULT = 50
AUDIT_LIMIT_MAX = 200


async def _body(request: Request) -> dict:
    """Tolerant body parse: invalid/empty/non-object bodies become {}."""
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _exchange_providers(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Provider)
            .where(Provider.enabled.is_(True), Provider.kind == "exchange")
        )
        or 0
    )


def _state(db: Session) -> dict:
    return {
        "kill_switch": trading_settings.kill_switch_engaged(db),
        "exchange_providers": _exchange_providers(db),
    }


@router.get("")
def get_trading(db: Session = Depends(get_db)):
    return {"trading": _state(db)}


@router.post("")
async def post_trading(request: Request, db: Session = Depends(get_db)):
    body = await _body(request)
    value = body.get("kill_switch")
    if not isinstance(value, bool):
        return JSONResponse({"error": "kill_switch must be a boolean"}, status_code=400)
    trading_settings.save(db, kill_switch=value)
    return {"ok": True, "trading": _state(db)}


@router.get("/audit")
def get_audit(limit: int = AUDIT_LIMIT_DEFAULT, db: Session = Depends(get_db)):
    limit = max(1, min(int(limit), AUDIT_LIMIT_MAX))
    rows = db.scalars(
        select(TradeAuditLog)
        .order_by(TradeAuditLog.ts.desc(), TradeAuditLog.id.desc())
        .limit(limit)
    ).all()
    return {"audit": [trading_audit.serialize_audit(row) for row in rows]}


__all__ = ["router"]