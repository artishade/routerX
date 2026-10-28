"""Sticky sessions + context handoff.

An agent conversation that re-routes every turn across different models loses
coherence as well as prompt cache. When a request names a conversation
(`x-nova-session` header or `metadata.session_id`), the first model that
answers pins the session for `NOVA_SESSION_TTL` seconds (default 30 minutes)
and later turns prefer that model. The pin is a *preference*, never a
constraint: `reorder()` only moves the pinned model up, so the fallback chain
is intact and a pinned model that is locked out or capping is simply skipped.

When the pinned model fails, `handoff()` records a compact note
`{"from", "to", "reason"}` for `_nova.handoff` and moves the pin to the model
that served the turn, so the conversation stays coherent on its new model.

State is in-process (a TTL map): a sticky session is an optimisation, and
losing it on restart only means the next turn re-pins on whatever serves it.
Off unless a session id is present, so nothing changes for other clients.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .config import SESSION_TTL_S

__all__ = [
    "SessionPin", "SESSIONS", "session_id_from", "get_pin", "pin", "handoff",
    "reorder", "drop", "reset",
]


@dataclass
class SessionPin:
    session_id: str
    model: str
    provider: str = ""
    upstream_model: str = ""
    pinned_at: float = 0.0
    turns: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "model": self.model, "provider": self.provider,
                "upstream_model": self.upstream_model, "turns": self.turns}


class _Store:
    def __init__(self, ttl_s: float = SESSION_TTL_S) -> None:
        self.ttl_s = max(1.0, float(ttl_s))
        self._pins: dict[str, SessionPin] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str, now: float | None = None) -> SessionPin | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            item = self._pins.get(session_id)
            if item is None:
                return None
            if now - item.pinned_at > self.ttl_s:
                self._pins.pop(session_id, None)
                return None
            return item

    def put(self, pin: SessionPin) -> None:
        with self._lock:
            self._prune()
            self._pins[pin.session_id] = pin

    def drop(self, session_id: str) -> bool:
        with self._lock:
            return self._pins.pop(session_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._pins.clear()

    def _prune(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._pins.items() if now - v.pinned_at > self.ttl_s]:
            self._pins.pop(sid, None)


SESSIONS = _Store()


def reset() -> None:
    """Tests: forget every pin."""
    SESSIONS.clear()


def session_id_from(headers: Any, body: dict | None = None) -> str:
    """`x-nova-session` header, else `metadata.session_id`; empty means none."""
    value = ""
    try:
        value = str(headers.get("x-nova-session") or "")
    except Exception:
        value = ""
    if not value and isinstance(body, dict):
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        value = str(metadata.get("session_id") or metadata.get("sessionId") or "")
    return value.strip()[:200]


def get_pin(session_id: str) -> SessionPin | None:
    return SESSIONS.get(session_id) if session_id else None


def pin(session_id: str, model: str, provider: str = "", upstream_model: str = "") -> SessionPin | None:
    """Pin (or move) a session onto the model that served a turn."""
    if not session_id or not model:
        return None
    existing = SESSIONS.get(session_id)
    item = SessionPin(
        session_id=session_id, model=model, provider=provider, upstream_model=upstream_model,
        pinned_at=time.time(), turns=(existing.turns + 1) if existing and existing.model == model else 1,
    )
    SESSIONS.put(item)
    return item


def handoff(session_id: str, from_model: str, to_model: str, reason: str) -> dict[str, str]:
    """Move the pin on failure and return the `_nova.handoff` note."""
    note = {"from": from_model or "", "to": to_model or "", "reason": (reason or "fallback")[:200]}
    if session_id and to_model:
        pin(session_id, to_model)
    return note


def reorder(rows: Sequence[Any], pinned_model: str, model_of=None) -> list[Any]:
    """Move the row serving `pinned_model` to the front, keeping every other row.

    Never removes or adds a candidate: the pinned model is skipped naturally by
    the pipeline when it fails, so the fallback chain is unchanged.
    """
    if not pinned_model:
        return list(rows)
    getter = model_of or (lambda r: getattr(r, "exposedId", "") or getattr(r, "modelId", ""))
    head: list[Any] = []
    rest: list[Any] = []
    for row in rows:
        try:
            matches = getter(row) == pinned_model
        except Exception:
            matches = False
        (head if matches else rest).append(row)
    return head + rest


def drop(session_id: str) -> bool:
    return SESSIONS.drop(session_id)
