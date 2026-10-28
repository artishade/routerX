"""Per-(provider, model, key) usage windows + limits, and upstream key cooldowns.

The gateway must never send a request it already knows will be refused for
quota. That is what this module is for: `UsageWindow` rows (see
`nova/models.py`) count requests and tokens per provider/model/key per open
minute and day, `capped()` answers "which (provider, model) pairs are out of
budget right now" for `route_request`, and `record_usage()` books a served
request.

Limits are permissive by default. The catalogue-free, self-hosted deployment
must route exactly as it did before this module existed, so a limit is
*unlimited* unless an operator configured it:

  * env defaults apply to every provider:
        NOVA_DEFAULT_RPM / NOVA_DEFAULT_TPM / NOVA_DEFAULT_RPD / NOVA_DEFAULT_TPD
  * the `rate_limits` SystemConfig JSON knob overrides per provider key:
        {"groq": {"rpm": 30, "tpm": 6000, "rpd": 1000, "tpd": 0},
         "*":    {"rpm": 120}}

A value of 0 (or a missing key) means unlimited, so a `{"rpm": 30}` override
only caps the minute - it does not invent a token budget.

Key cooldowns: `cooldown_seconds()` turns an upstream failure (status +
`Retry-After`) into the seconds a `ProviderKey` should sleep, and
`set_cooldown()` / `clear_cooldown()` persist `cooldownUntil` / `lastError` /
`errCount` on the row. Selection itself lives in the gateway because it needs
every key for the provider at once - see `routers.gateway.select_upstream_key_sync`.

Nothing here touches the network. All helpers that take a `Session` do their
own commit and never let bookkeeping break a served response.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from sqlalchemy import and_, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import (
    DEFAULT_RPD, DEFAULT_RPM, DEFAULT_TPD, DEFAULT_TPM,
    KEY_COOLDOWN_401_S, KEY_COOLDOWN_429_S, KEY_COOLDOWN_5XX_S, KEY_COOLDOWN_MAX_S,
)
from .models import ProviderKey, UsageWindow, utcnow

__all__ = [
    "CFG_RATE_LIMITS", "WINDOW_MINUTE", "WINDOW_DAY", "LIMIT_KEYS",
    "bucket", "limits_from", "normalize_limits", "is_exhausted", "merge_records",
    "load_limits", "save_limits", "record_usage", "record_result", "capped",
    "prune_usage", "estimate_tokens", "reset_prune_clock",
    "usage_snapshot", "cooldown_seconds", "parse_retry_after", "set_cooldown",
    "clear_cooldown", "is_cooling", "pick_key",
]

CFG_RATE_LIMITS = "rate_limits"
WINDOW_MINUTE = "minute"
WINDOW_DAY = "day"
LIMIT_KEYS = ("rpm", "tpm", "rpd", "tpd")
# The UsageWindow unique index columns, in table order (upsert target).
WINDOW_UNIQUE_COLS = ("providerId", "modelId", "keyId", "windowKind", "bucket")
ZERO_LIMITS: dict[str, int] = {k: 0 for k in LIMIT_KEYS}

# Pruning runs at most once an hour in this process.
_PRUNE_TS = 0.0
_PRUNE_LOCK = threading.Lock()
PRUNE_KEEP_DAYS = 2


# --------------------------------------------------------------------------- #
# Buckets
# --------------------------------------------------------------------------- #

def bucket(now: float | datetime | None = None, kind: str = WINDOW_MINUTE) -> int:
    """Epoch minute / epoch day the timestamp falls in (UTC).

    Accepts a unix timestamp or a naive-UTC `datetime` (the DB convention).
    """
    if now is None:
        ts = time.time()
    elif isinstance(now, datetime):
        # The DB convention is a *naive UTC* datetime; `.timestamp()` would read
        # it as local wall-clock time. Attach UTC first, then convert.
        aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        ts = aware.timestamp()
    else:
        ts = float(now)
    size = 60 if kind == WINDOW_MINUTE else 86_400
    return int(ts) // size


def _window_kinds() -> tuple[str, str]:
    return WINDOW_MINUTE, WINDOW_DAY


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #

def normalize_limits(raw: Any) -> dict[str, int]:
    """Clamp one limit mapping to non-negative ints, defaulting every key to 0."""
    out = dict(ZERO_LIMITS)
    if not isinstance(raw, dict):
        return out
    for key in LIMIT_KEYS:
        value = raw.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            out[key] = max(0, int(float(value)))
        except (TypeError, ValueError):
            continue
    return out


def limits_from(entry: dict[str, Any] | None = None, env: dict[str, int] | None = None) -> dict[str, int]:
    """Effective limits for one provider: env defaults overlaid by the entry.

    `entry` is a provider's slice of the `rate_limits` knob (may be missing
    keys); `env` defaults to the `NOVA_DEFAULT_*` values read at import time.
    """
    base = dict(ZERO_LIMITS)
    defaults = env if env is not None else {
        "rpm": DEFAULT_RPM, "tpm": DEFAULT_TPM, "rpd": DEFAULT_RPD, "tpd": DEFAULT_TPD,
    }
    base.update(normalize_limits(defaults))
    overrides = normalize_limits(entry)
    if isinstance(entry, dict):
        for key in LIMIT_KEYS:
            if entry.get(key) is not None and not isinstance(entry.get(key), bool):
                base[key] = overrides[key]
    return base


def load_limits(db: Session) -> dict[str, dict[str, int]]:
    """The `rate_limits` knob, normalized (`{provider_key: limits}`)."""
    from .kv import get_config_json

    raw = get_config_json(db, CFG_RATE_LIMITS, {}) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): normalize_limits(v) for k, v in raw.items() if isinstance(v, dict)}


def save_limits(db: Session, limits: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Persist the knob. Unknown provider keys are kept (a provider may be added later)."""
    from .kv import set_config_json

    clean = {str(k): normalize_limits(v) for k, v in (limits or {}).items() if isinstance(v, dict)}
    set_config_json(db, CFG_RATE_LIMITS, clean)
    return clean


def is_exhausted(usage: dict[str, Any] | None, limits: dict[str, int] | None) -> bool:
    """True when any configured limit is reached or crossed by `usage`.

    `usage` keys are `requests`, `tokensIn`, `tokensOut` (missing counts are 0).
    Any limit that is 0 / missing is unlimited and never exhausts.
    """
    limits = limits or {}
    requests = int((usage or {}).get("requests", 0) or 0)
    tokens = int((usage or {}).get("tokensIn", 0) or 0) + int((usage or {}).get("tokensOut", 0) or 0)
    rpm = int(limits.get("rpm", 0) or 0)
    rpd = int(limits.get("rpd", 0) or 0)
    tpm = int(limits.get("tpm", 0) or 0)
    tpd = int(limits.get("tpd", 0) or 0)
    return (rpm > 0 and requests >= rpm) or (rpd > 0 and requests >= rpd) \
        or (tpm > 0 and tokens >= tpm) or (tpd > 0 and tokens >= tpd)


def merge_records(records: Any) -> dict[str, dict[str, int]]:
    """Fold usage rows (dicts or ORM rows) into `{kind: {requests, tokensIn, tokensOut}}`.

    Accepts `UsageWindow` instances or plan dicts with `window_kind`/`bucket`/
    counters; unknown kinds are ignored so a future window type never breaks a
    decision.
    """
    out = {kind: {"requests": 0, "tokensIn": 0, "tokensOut": 0} for kind in _window_kinds()}
    for rec in records or ():
        if isinstance(rec, dict):
            kind = rec.get("windowKind") or rec.get("window_kind")
            requests = rec.get("requests", 0)
            tin = rec.get("tokensIn", rec.get("tokens_in", 0))
            tout = rec.get("tokensOut", rec.get("tokens_out", 0))
        else:
            kind = getattr(rec, "windowKind", None)
            requests = getattr(rec, "requests", 0)
            tin = getattr(rec, "tokensIn", 0)
            tout = getattr(rec, "tokensOut", 0)
        slot = out.get(str(kind))
        if slot is None:
            continue
        slot["requests"] += int(requests or 0)
        slot["tokensIn"] += int(tin or 0)
        slot["tokensOut"] += int(tout or 0)
    return out


def _exhausted_by_window(kind: str, usage: dict[str, int], limits: dict[str, int]) -> bool:
    """Per-window check: the minute window only looks at rpm/tpm, the day one at rpd/tpd."""
    if kind == WINDOW_MINUTE:
        return is_exhausted(usage, {"rpm": limits.get("rpm", 0), "tpm": limits.get("tpm", 0)})
    return is_exhausted(usage, {"rpd": limits.get("rpd", 0), "tpd": limits.get("tpd", 0)})


# --------------------------------------------------------------------------- #
# Usage rows
# --------------------------------------------------------------------------- #

def _as_utc(now: float | datetime | None) -> datetime:
    """Naive-UTC `datetime` for a timestamp (the DB's storage convention)."""
    if isinstance(now, datetime):
        return now.replace(tzinfo=None) if now.tzinfo is None else now.astimezone(timezone.utc).replace(tzinfo=None)
    if now is None:
        return utcnow()
    return datetime.fromtimestamp(float(now), tz=timezone.utc).replace(tzinfo=None)


def record_usage(db: Session, provider_id: int | None, model_id: str, key_id: int | None,
                 tokens_in: int = 0, tokens_out: int = 0, now: float | None = None) -> None:
    """Book one served request into the open minute and day windows.

    Pruning is deliberately NOT done here: a write path must never delete rows
    (a mis-scoped delete would silently reset a cap). `prune_usage` is called by
    the hourly maintenance loop instead - see `main.usage_maintenance`.

    SQLAlchemy upsert on the unique (provider, model, key, kind, bucket) tuple,
    so the increment is atomic under concurrent writers (INSERT..ON CONFLICT on
    SQLite/Postgres, ON DUPLICATE KEY on MySQL) instead of a read-then-write
    race. Never raises: bookkeeping must not break a served response.
    """
    provider_id = int(provider_id or 0)
    key_id = int(key_id or 0)
    tokens_in = max(0, int(tokens_in or 0))
    tokens_out = max(0, int(tokens_out or 0))
    model_text = str(model_id if model_id is not None else "")
    written_at = _as_utc(now)
    for kind in _window_kinds():
        values = {
            "providerId": provider_id, "modelId": model_text, "keyId": key_id,
            "windowKind": kind, "bucket": bucket(written_at, kind),
            "requests": 1, "tokensIn": tokens_in, "tokensOut": tokens_out, "ts": written_at,
        }
        try:
            stmt = _upsert_statement(db, values)
            db.execute(stmt)
            db.commit()
        except Exception:  # bookkeeping never breaks a served response
            db.rollback()


def _upsert_statement(db: Session, values: dict[str, Any]):
    """INSERT .. ON CONFLICT / ON DUPLICATE KEY increment for the open window."""
    from sqlalchemy.dialects import mysql, postgresql, sqlite

    increment = {
        "requests": UsageWindow.requests + 1,
        "tokensIn": UsageWindow.tokensIn + values["tokensIn"],
        "tokensOut": UsageWindow.tokensOut + values["tokensOut"],
        "ts": values["ts"],
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = postgresql.insert(UsageWindow).values(**values)
        return stmt.on_conflict_do_update(index_elements=WINDOW_UNIQUE_COLS, set_=increment)
    if dialect in ("mysql", "mariadb"):
        stmt = mysql.insert(UsageWindow).values(**values)
        return stmt.on_duplicate_key_update(**increment)
    stmt = sqlite.insert(UsageWindow).values(**values)
    return stmt.on_conflict_do_update(index_elements=WINDOW_UNIQUE_COLS, set_=increment)


def load_usage(db: Session, now: float | None = None,
               provider_id: int | None = None) -> dict[tuple[int, str], dict[str, dict[str, int]]]:
    """Current-minute/day usage keyed by `(providerId, modelId)` -> merged windows.

    Summed across upstream keys: the cap is a budget for the provider+model,
    whichever key pays it.
    """
    minute, day = _window_kinds()
    stmt = select(UsageWindow).where(
        UsageWindow.windowKind.in_((minute, day)),
        UsageWindow.bucket.in_((bucket(now, minute), bucket(now, day))),
    )
    if provider_id is not None:
        stmt = stmt.where(UsageWindow.providerId == int(provider_id))
    out: dict[tuple[int, str], dict[str, dict[str, int]]] = {}
    for row in db.scalars(stmt).all():
        slot = out.setdefault((int(row.providerId), str(row.modelId)), {
            minute: {"requests": 0, "tokensIn": 0, "tokensOut": 0},
            day: {"requests": 0, "tokensIn": 0, "tokensOut": 0},
        })
        w = slot[row.windowKind]
        w["requests"] += int(row.requests or 0)
        w["tokensIn"] += int(row.tokensIn or 0)
        w["tokensOut"] += int(row.tokensOut or 0)
    return out


def capped(db: Session, provider_keys: dict[int, str] | None = None,
           limits: dict[str, dict[str, int]] | None = None,
           now: float | None = None) -> set[tuple[int, str]]:
    """`{(providerId, modelId)}` pairs whose open window is already exhausted.

    `route_request` drops these before ordering, so a request that would be
    refused for quota is served by the next candidate instead of burning a
    round trip. With no configured limits this returns an empty set (the
    default deployment). `limits` defaults to the `rate_limits` knob; provide
    `provider_keys` (id -> provider key) to apply per-provider overrides.
    """
    try:
        limits = load_limits(db) if limits is None else limits
        if not limits and not any((DEFAULT_RPM, DEFAULT_TPM, DEFAULT_RPD, DEFAULT_TPD)):
            return set()
        usage = load_usage(db, now)
        out: set[tuple[int, str]] = set()
        for (pid, model_id), windows in usage.items():
            entry: dict[str, Any] = {}
            if limits:
                pkey = (provider_keys or {}).get(pid)
                entry = limits.get(pkey, {}) if pkey else {}
                entry = {**limits.get("*", {}), **entry} if isinstance(entry, dict) else {}
            effective = limits_from(entry)
            if not any(effective.values()):
                continue
            if any(_exhausted_by_window(kind, w, effective) for kind, w in windows.items()):
                out.add((pid, model_id))
        return out
    except Exception:  # never break routing on bookkeeping
        return set()


def estimate_tokens(text: str | None) -> int:
    """The gateway token estimate (chars/4) for usage rows without a usage block."""
    return max(1, (len(text if text is not None else "") + 3) // 4)


def record_result(db: Session, provider_id: int | None, model_id: str, key_id: int | None,
                  result: dict[str, Any] | None = None, messages: Any = None,
                  content: str | None = None, now: float | None = None) -> None:
    """Book a completed non-streaming/streaming result by (provider, model, key).

    Token counts come from the upstream usage block when the provider sent one,
    else from the same chars/4 estimate the gateway uses for RequestLog, so the
    window never has to know how a particular provider reports usage.
    """
    usage = (result or {}).get("usage") if isinstance(result, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens")
    tokens_out = usage.get("completion_tokens") or usage.get("output_tokens")
    if tokens_in is None:
        tokens_in = estimate_tokens("\n".join(
            f"{m.get('role')}:{m.get('content')}" for m in (messages or []) if isinstance(m, dict)
        ))
    if tokens_out is None:
        tokens_out = estimate_tokens(content or (result or {}).get("content") if isinstance(result, dict) else content)
    record_usage(db, provider_id, model_id, key_id, int(tokens_in or 0), int(tokens_out or 0), now=now)


def usage_snapshot(db: Session, now: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Flat per-(provider, model) usage rows for the admin usage surface."""
    rows = load_usage(db, now)
    out = []
    for (pid, model_id), windows in rows.items():
        minute = windows.get(WINDOW_MINUTE, {})
        day = windows.get(WINDOW_DAY, {})
        out.append({
            "provider_id": pid,
            "model": model_id,
            "minute": {**minute, "tokens": minute.get("tokensIn", 0) + minute.get("tokensOut", 0)},
            "day": {**day, "tokens": day.get("tokensIn", 0) + day.get("tokensOut", 0)},
        })
    out.sort(key=lambda r: (-r["day"]["requests"], r["provider_id"], r["model"]))
    return out[: max(1, int(limit))]


def prune_usage(db: Session, keep_days: int = PRUNE_KEEP_DAYS) -> int:
    """Delete windows older than `keep_days`; returns the number of rows removed."""
    # `ts` (last write) is the only comparable column across window kinds:
    # epoch-minute buckets and epoch-day buckets are different scales.
    cutoff = utcnow() - timedelta(days=max(1, int(keep_days)))
    try:
        result = db.execute(delete(UsageWindow).where(UsageWindow.ts < cutoff))
        db.commit()
        return int(result.rowcount or 0)
    except Exception:
        db.rollback()
        return 0


def _prune_if_due(db: Session, now: float | None = None) -> bool:
    """Prune at most once an hour per process (cheap in-process guard)."""
    global _PRUNE_TS
    ts = time.time() if now is None else float(now)
    with _PRUNE_LOCK:
        if ts - _PRUNE_TS < 3600:
            return False
        _PRUNE_TS = ts
    prune_usage(db)
    return True


def reset_prune_clock() -> None:
    global _PRUNE_TS
    with _PRUNE_LOCK:
        _PRUNE_TS = 0.0


# --------------------------------------------------------------------------- #
# Upstream key cooldowns
# --------------------------------------------------------------------------- #

def parse_retry_after(value: Any) -> float | None:
    """Seconds from a `Retry-After` header (delta-seconds or an HTTP-date)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def cooldown_seconds(status: int, retry_after: Any = None, err_count: int = 0) -> float:
    """How long a key sleeps after a failure.

    429 / 401 / 5xx honour `Retry-After` when the provider sent one, otherwise
    use the per-status default (`NOVA_KEY_COOLDOWN_*`) doubled per prior
    consecutive error, capped at `NOVA_KEY_COOLDOWN_MAX`. Any other status
    returns 0: only quota, auth and server faults are the key's fault.
    """
    status = int(status or 0)
    if status == 429:
        base = KEY_COOLDOWN_429_S
    elif status in (401, 403):
        base = KEY_COOLDOWN_401_S
    elif 500 <= status < 600:
        base = KEY_COOLDOWN_5XX_S
    else:
        return 0.0
    retry = parse_retry_after(retry_after)
    if retry is not None and retry > 0:
        return min(max(retry, 1.0), KEY_COOLDOWN_MAX_S)
    backoff = base * (2 ** max(0, min(int(err_count or 0), 6)))
    return float(min(max(backoff, 1.0), KEY_COOLDOWN_MAX_S))


def set_cooldown(db: Session, key_id: int | None, seconds: float, error: str = "") -> None:
    """Persist a cooldown + lastError + one more errCount on the key row."""
    if key_id is None:
        return
    try:
        row = db.get(ProviderKey, int(key_id))
        if row is None:
            return
        row.cooldownUntil = utcnow() + timedelta(seconds=max(1.0, float(seconds)))
        row.lastError = (error or "upstream failure")[:500]
        row.errCount = int(row.errCount or 0) + 1
        db.commit()
    except Exception:
        db.rollback()


def clear_cooldown(db: Session, key_id: int | None, *, bump_usage: bool = True) -> None:
    """Clear the cooldown after a success and book `reqCount` / `lastUsedAt`."""
    if key_id is None:
        return
    try:
        row = db.get(ProviderKey, int(key_id))
        if row is None:
            return
        row.cooldownUntil = None
        if bump_usage:
            row.reqCount = int(row.reqCount or 0) + 1
            row.lastUsedAt = utcnow()
        db.commit()
    except Exception:
        db.rollback()


def is_cooling(key: ProviderKey, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return key.cooldownUntil is not None and key.cooldownUntil > now


def pick_key(rows: list[ProviderKey], now: datetime | None = None) -> tuple[ProviderKey | None, bool]:
    """Choose the key to use from a provider's enabled keys.

    Returns `(key, used_cooling_fallback)`.

    Policy: the highest-weight key that is not cooling down wins. A cooling key
    is used **only** when every enabled key for the provider is cooling down,
    and then the one whose window expires soonest is picked, because a dead
    provider is better than no provider and the soonest-expiring key is the one
    most likely to work. `used_cooling_fallback` lets the caller stop retrying
    further keys for that provider (they are all cooling too).
    """
    if not rows:
        return None, False
    now = now or utcnow()
    ready = [k for k in rows if not is_cooling(k, now)]
    if ready:
        return ready[0], False
    soonest = min(rows, key=lambda k: (k.cooldownUntil or now, -(k.weight or 1), k.id))
    return soonest, True