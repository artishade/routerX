"""Opt-in response cache for non-streaming completions.

A deterministic completion (same model, same messages, same generation knobs,
same tools) does not need to be paid for twice. This is a content-addressed,
in-memory TTL + LRU store: `key_for()` hashes the normalized request, `get()`
returns the stored body while it is fresh, `put()` stores one.

Deliberately opt-in. `NOVA_RESPONSE_CACHE=on` or the `response_cache`
SystemConfig knob ({"enabled": true, "ttl": 300, "max_entries": 256}) turns it
on; every default is OFF, so an existing deployment behaves exactly as before.
Only NON-streaming 200 completions with no tool_calls are stored - a cache hit
must never replace a tool call the agent loop depends on - and a request that
carries `x-nova-no-cache: 1` bypasses both read and write.

Nothing here performs I/O. Stats feed `GET /api/admin/cache/stats`.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any

from sqlalchemy.orm import Session

from .config import RESPONSE_CACHE, RESPONSE_CACHE_MAX, RESPONSE_CACHE_TTL_S

CFG_CACHE = "response_cache"

__all__ = [
    "ResponseCache", "CACHE", "key_for", "get", "put", "settings", "is_enabled",
    "clear", "stats", "reset", "cacheable", "normalize_messages",
]

# Fields that change the answer. Everything else in the body (stream, user,
# metadata, x-* headers) is deliberately excluded: it does not alter the text a
# provider generates for the same prompt.
_KEY_FIELDS = ("model", "temperature", "max_tokens", "max_completion_tokens", "top_p", "tools")

_NORMALIZED_ROLES = {"developer": "system"}


def _normalize_content(content: Any) -> Any:
    """Flatten OpenAI content parts to text so the same prompt hashes the same."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return str(content)


def normalize_messages(messages: Any) -> list[dict]:
    """Role + text + tool identity of each message, in order."""
    out: list[dict] = []
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        role = _NORMALIZED_ROLES.get(str(m.get("role") or "user"), str(m.get("role") or "user"))
        entry: dict[str, Any] = {"role": role, "content": _normalize_content(m.get("content"))}
        if m.get("tool_calls"):
            entry["tool_calls"] = [
                {"id": tc.get("id"), "name": (tc.get("function") or {}).get("name"),
                 "arguments": (tc.get("function") or {}).get("arguments")}
                for tc in m["tool_calls"] if isinstance(tc, dict)
            ]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


def key_for(body: dict, messages: Any = None) -> str:
    """sha256 over the normalized request - the cache address."""
    source = messages if messages is not None else body.get("messages")
    payload = {"messages": normalize_messages(source), "model": str(body.get("model") or "")}
    for field in _KEY_FIELDS[1:]:
        payload[field] = body.get(field)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _body_has_tool_calls(body: dict) -> bool:
    for choice in body.get("choices") or ():
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if message.get("tool_calls") or choice.get("finish_reason") == "tool_calls":
            return True
    return False


def cacheable(body: dict, status: int = 200) -> bool:
    """Only a plain 200 text completion is worth storing."""
    return status == 200 and isinstance(body, dict) and not _body_has_tool_calls(body)


class ResponseCache:
    """Thread-safe TTL + LRU store of completion bodies."""

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 256) -> None:
        self.ttl_s = max(1.0, float(ttl_s))
        self.max_entries = max(1, int(max_entries))
        self._data: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            stored_at, value = item
            if now - stored_at > self.ttl_s:
                del self._data[key]
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return json.loads(json.dumps(value))  # callers mutate the body freely

    def put(self, key: str, value: dict, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (now, json.loads(json.dumps(value)))
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self._evictions += 1

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data.clear()
            return n

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._data),
                "max_entries": self.max_entries,
                "ttl_s": self.ttl_s,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": round((self._hits / total) * 100, 1) if total else 0.0,
            }

    def reset_counters(self) -> None:
        with self._lock:
            self._hits = self._misses = self._evictions = 0


CACHE = ResponseCache(RESPONSE_CACHE_TTL_S, RESPONSE_CACHE_MAX)


# --------------------------------------------------------------------------- #
# Configuration + module-level facade (the gateway calls these)
# --------------------------------------------------------------------------- #

def settings(db: Session | None = None) -> dict[str, Any]:
    """Effective cache configuration: env, then the SystemConfig knob."""
    enabled = RESPONSE_CACHE
    ttl = float(RESPONSE_CACHE_TTL_S)
    max_entries = int(RESPONSE_CACHE_MAX)
    if db is not None:
        try:
            from .kv import get_config_json

            raw = get_config_json(db, CFG_CACHE, {}) or {}
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            value = raw.get("enabled")
            if value is True or str(value or "").strip().lower() in ("1", "true", "on"):
                enabled = True
            if raw.get("ttl") is not None and not RESPONSE_CACHE_TTL_S:
                ttl = float(raw.get("ttl"))
            if raw.get("max_entries") is not None and not RESPONSE_CACHE_MAX:
                max_entries = int(raw.get("max_entries"))
    return {"enabled": bool(enabled), "ttl": ttl, "max_entries": max_entries}


def is_enabled(db: Session | None = None) -> bool:
    return bool(settings(db)["enabled"])


def get(key: str) -> dict | None:
    return CACHE.get(key)


def put(key: str, value: dict) -> None:
    if cacheable(value):
        CACHE.put(key, value)


def clear() -> int:
    return CACHE.clear()


def stats() -> dict[str, Any]:
    return CACHE.stats()


def reset() -> None:
    """Tests: empty the store and its counters."""
    CACHE.clear()
    CACHE.reset_counters()
