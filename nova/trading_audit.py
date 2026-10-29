"""Append-only audit trail for the trading subsystem.

Every trading decision path (intent, refusal, submission, fill, kill-switch,
read, cancel) writes one `TradeAuditLog` row. History must never be rewritten,
so ORM UPDATE and DELETE are blocked at the mapper level: there is deliberately
no code path that can mutate a stored audit record, and a future one would fail
loudly instead of silently editing the trail.

`record()` participates in the caller's transaction (flush, no commit) so a
trading decision and its audit row land atomically. `record_standalone()` is
for read paths and background jobs where an audit failure must never break the
caller.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from .models import TradeAuditLog

__all__ = [
    "TradeAuditImmutable",
    "record",
    "record_standalone",
    "serialize_audit",
]

log = logging.getLogger(__name__)


class TradeAuditImmutable(Exception):
    """Raised when an ORM update or delete targets a TradeAuditLog row."""


@event.listens_for(TradeAuditLog, "before_update")
def _block_audit_update(_mapper: Any, _connection: Any, target: TradeAuditLog) -> None:
    raise TradeAuditImmutable(
        "TradeAuditLog is append-only: update refused "
        f"(id={getattr(target, 'id', None)}, event={getattr(target, 'event', '')!r})"
    )


@event.listens_for(TradeAuditLog, "before_delete")
def _block_audit_delete(_mapper: Any, _connection: Any, target: TradeAuditLog) -> None:
    raise TradeAuditImmutable(
        "TradeAuditLog is append-only: delete refused "
        f"(id={getattr(target, 'id', None)}, event={getattr(target, 'event', '')!r})"
    )


def _encode_payload(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return "{}"
    try:
        return json.dumps(payload, default=str)
    except Exception:
        return "{}"


def record(
    db: Session,
    *,
    event: str,
    action: str = "",
    status: str = "",
    code: str = "",
    actor: str = "",
    client_key_id: int | None = None,
    provider_id: int | None = None,
    provider_key: str = "",
    exchange: str = "",
    symbol: str = "",
    order_public_id: str = "",
    message: str = "",
    payload: dict | None = None,
    mode: str = "",
) -> TradeAuditLog:
    """Insert one audit row in the caller's transaction (flush, no commit)."""
    event_name = str(event or "").strip()
    if not event_name:
        raise ValueError("audit event is required")
    row = TradeAuditLog(
        event=event_name,
        action=action or "",
        status=status or "",
        code=code or "",
        actor=actor or "",
        clientKeyId=client_key_id,
        providerId=provider_id,
        providerKey=provider_key or "",
        exchange=exchange or "",
        symbol=symbol or "",
        orderPublicId=order_public_id or "",
        message=message or "",
        payload=_encode_payload(payload),
        mode=mode or "",
    )
    db.add(row)
    db.flush()
    return row


def record_standalone(**kwargs: Any) -> None:
    """Write an audit row on a private session; never raises to the caller."""
    try:
        from .database import SessionLocal

        with SessionLocal() as db:
            record(db, **kwargs)
            db.commit()
    except Exception as err:  # noqa: BLE001 - auditing must not break a read path
        log.warning("trade audit write failed (event=%r): %s", kwargs.get("event"), err)


def _ts_ms(dt: datetime | None) -> int:
    """Naive-UTC DB datetime -> epoch milliseconds (0 when unset)."""
    if dt is None:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _decode_payload(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def serialize_audit(row: TradeAuditLog) -> dict:
    """Wire shape: snake_case keys, epoch-ms ts, payload as a dict."""
    return {
        "id": row.id,
        "ts": _ts_ms(row.ts),
        "event": row.event,
        "action": row.action,
        "status": row.status,
        "code": row.code,
        "actor": row.actor,
        "client_key_id": row.clientKeyId,
        "provider_id": row.providerId,
        "provider_key": row.providerKey,
        "exchange": row.exchange,
        "symbol": row.symbol,
        "order_public_id": row.orderPublicId,
        "message": row.message,
        "payload": _decode_payload(row.payload),
        "mode": row.mode,
    }