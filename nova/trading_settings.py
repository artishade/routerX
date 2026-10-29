"""Operator trading settings (currently the kill-switch).

The kill-switch is a hard stop that every trading entry point checks through
`nova.trading.ensure_trading_enabled`. It lives in `SystemConfig` under
`trading_settings` so it survives restarts, and every change is written to the
append-only trade audit trail in the SAME transaction: a kill-switch can never
be flipped without a matching audit row, and an audit row can never claim a
flip that was not persisted.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from . import trading_audit
from .kv import get_config_json
from .models import SystemConfig

__all__ = ["CFG_TRADING", "DEFAULT", "load", "save", "kill_switch_engaged"]

CFG_TRADING = "trading_settings"
DEFAULT = {"kill_switch": False}


def load(db: Session) -> dict:
    """Current settings; a missing or malformed row means the safe default."""
    raw = get_config_json(db, CFG_TRADING, {})
    if not isinstance(raw, dict):
        raw = {}
    return {"kill_switch": bool(raw.get("kill_switch", False))}


def save(db: Session, *, kill_switch: bool) -> dict:
    """Persist the kill-switch and append its audit row in one commit."""
    engaged = bool(kill_switch)
    value = json.dumps({"kill_switch": engaged})
    row = db.get(SystemConfig, CFG_TRADING)
    if row is None:
        db.add(SystemConfig(key=CFG_TRADING, value=value))
    else:
        row.value = value
    trading_audit.record(
        db,
        event="kill_switch",
        action="trading.kill_switch",
        status="engaged" if engaged else "released",
        actor="admin",
        message=(
            "Trading kill-switch engaged: all trading requests are refused."
            if engaged
            else "Trading kill-switch released: trading requests may proceed."
        ),
        payload={"kill_switch": engaged},
    )
    db.commit()
    return load(db)


def kill_switch_engaged(db: Session) -> bool:
    return load(db)["kill_switch"]