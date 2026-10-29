"""Gateway client-key authentication + per-key quota enforcement.

Today a `/v1/*` request may arrive anonymously, and a minted `ClientKey` row has
no effect at all: `rpmLimit`, `tpdLimit` and `allowedModels` are stored and
never read. This module is the single place that turns those columns into
behaviour:

  * `authenticate(request)` resolves `Authorization: Bearer <token>` or
    `x-api-key` against enabled `ClientKey.token` rows. A missing credential is
    `None` (anonymous). An unknown or disabled token raises `AuthError(401)`
    with a stable `error.code`, because silently treating a revoked key as
    anonymous would defeat revocation entirely.
  * `enforce(client_key, requested_model)` applies the per-key rules:
    `allowedModels` (comma/space list of globs, `*` = all) -> 403, the sliding
    one-minute request counter (`rpmLimit`) -> 429 + `Retry-After`, and the
    daily token budget (`tpdLimit` against `tokensIn + tokensOut`) -> 429.
  * `attribute(...)` / `update_usage(...)` are the ONE place usage is written
    back (`reqCount`, `lastUsedAt`, `tokensIn/Out`), called by the gateway at
    the end of every request, so no endpoint has to remember to do it.

Anonymous access stays allowed while the deployment has zero enabled client
keys (the self-hosted default, backwards compatible); `NOVA_REQUIRE_API_KEY`
forces a key even then. The `gateway_auth` dependency wires all of this into
every gateway route in `routers/gateway.py` and never touches admin/UI routes.

Lockout state for repeated *failed* authentications is an in-process sliding
window (`ClientLockouts` from `nova.routing`), so a token-guessing client is
throttled without a DB write per attempt.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import routing
from .catalog import glob_match
from .config import REQUIRE_API_KEY
from .database import SessionLocal, get_db
from .models import ClientKey, utcnow

__all__ = [
    "AuthError", "ClientPrincipal", "authenticate", "enforce", "attribute",
    "update_usage", "parse_allowed_models", "model_allowed", "RpmCounter",
    "has_enabled_keys", "reset_state", "gateway_auth", "require_client_key",
    "CODE_MISSING_KEY", "CODE_INVALID_KEY", "CODE_MODEL_FORBIDDEN",
    "CODE_RPM_EXCEEDED", "CODE_TPD_EXCEEDED", "CODE_SCOPE_FORBIDDEN",
    "TRADING_READ_SCOPES", "parse_scopes", "scope_allowed",
    "require_trading_scope", "seconds_until_utc_midnight",
]

# Stable `error.code` values: clients match on these, not on the prose.
CODE_MISSING_KEY = "missing_api_key"
CODE_INVALID_KEY = "invalid_api_key"
CODE_MODEL_FORBIDDEN = "model_not_allowed"
CODE_RPM_EXCEEDED = "rate_limit_exceeded"
CODE_TPD_EXCEEDED = "token_budget_exceeded"
CODE_SCOPE_FORBIDDEN = "scope_not_allowed"

# Scopes that cover the read-only trading namespace. `trading:read` is the
# umbrella scope: a key holding it may call every read endpoint, while the
# narrower scopes gate exactly one endpoint each.
TRADING_READ_SCOPES = (
    "trading:read", "trading:quote", "trading:positions", "trading:balances",
)


class AuthError(Exception):
    """A refused request. `to_payload()` is the wire body the gateway returns."""

    def __init__(self, status: int, code: str, message: str,
                 retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = message
        self.retry_after = retry_after

    def to_payload(self) -> dict:
        error = {"message": self.message, "type": "gateway_auth_error", "code": self.code}
        if self.retry_after is not None:
            error["retry_after"] = self.retry_after
        return {"error": error}

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}


@dataclass
class ClientPrincipal:
    """What the gateway knows about the caller for one request."""

    key: ClientKey | None = None
    anonymous: bool = False
    limit_status: str = "ok"
    extra: dict = field(default_factory=dict)

    @property
    def key_id(self) -> int | None:
        return self.key.id if self.key is not None else None

    @property
    def name(self) -> str:
        if self.key is None:
            return "anonymous"
        return self.key.name or f"key-{self.key.id}"


# --------------------------------------------------------------------------- #
# allowedModels
# --------------------------------------------------------------------------- #

def parse_allowed_models(raw: str | None) -> list[str]:
    """`\"gpt-*, claude-*\"` -> `[\"gpt-*\", \"claude-*\"]`; empty means all."""
    text = (raw or "").replace(";", ",").replace("\t", ",").replace("\n", ",")
    out: list[str] = []
    for part in text.split(","):
        for token in part.split():
            token = token.strip()
            if token and token not in out:
                out.append(token)
    return out


def model_allowed(allowed: list[str], model: str) -> bool:
    """True when `model` matches one of the globs (or the list is empty/`*`)."""
    if not allowed:
        return True
    for pattern in allowed:
        if pattern == "*" or glob_match(pattern, model):
            return True
    return False


# --------------------------------------------------------------------------- #
# Trading scopes
# --------------------------------------------------------------------------- #

def parse_scopes(raw: str | None) -> list[str]:
    """`"trading:read, trading:quote"` -> the two scopes (same list syntax)."""
    return parse_allowed_models(raw)


def scope_allowed(allowed: list[str], scope: str) -> bool:
    """True when `scope` is granted. FAIL-CLOSED: an empty list grants nothing.

    Exact matches, `*`, a namespace wildcard (`trading:*`) and glob patterns
    (`trading:*`, `trading:quo*`, ...) are accepted.
    """
    if not allowed:
        return False
    for pattern in allowed:
        if not pattern:
            continue
        if pattern == "*" or pattern == scope or glob_match(pattern, scope):
            return True
        if pattern.endswith(":*") and scope.startswith(pattern[:-1]):
            return True
    return False


def require_trading_scope(principal: object, scope: str) -> None:
    """Enforce an explicit client key plus its trading scopes.

    Trading never inherits the anonymous/text fail-open rule: a request without
    a resolved `ClientKey` is refused with 401, and a key whose `tradingScopes`
    do not cover `scope` (or the `trading:read` umbrella) is refused with 403.
    """
    key = getattr(principal, "key", None)
    if key is None:
        raise AuthError(401, CODE_MISSING_KEY,
                        "An API key is required for trading endpoints.")
    allowed = parse_scopes(getattr(key, "tradingScopes", "") or "")
    if scope_allowed(allowed, scope) or scope_allowed(allowed, "trading:read"):
        return
    raise AuthError(
        403, CODE_SCOPE_FORBIDDEN,
        f"This API key is not allowed to use trading scope '{scope}'.",
    )


# --------------------------------------------------------------------------- #
# RPM sliding-minute counter (in memory, per client-key id)
# --------------------------------------------------------------------------- #

class RpmCounter:
    """Sliding 60s request counter per client key.

    Memory-only on purpose: the limit is a safety valve for one running
    gateway, and writing a counter row per request would double the DB writes
    on the hot path. `used()` is exposed so the admin usage surface can show it.
    """

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = float(window_s)
        self._hits: dict[int, list[float]] = {}
        self._lock = threading.Lock()

    def used(self, key_id: int, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        cutoff = now - self.window_s
        with self._lock:
            hits = [t for t in self._hits.get(key_id, ()) if t > cutoff]
            if hits:
                self._hits[key_id] = hits
            else:
                self._hits.pop(key_id, None)
        return len(hits)

    def check(self, key_id: int, limit: int, now: float | None = None) -> tuple[bool, int]:
        """`(allowed, retry_after_seconds)`. Records a hit when allowed."""
        if limit <= 0:
            return True, 0
        now = time.time() if now is None else float(now)
        cutoff = now - self.window_s
        with self._lock:
            hits = [t for t in self._hits.get(key_id, ()) if t > cutoff]
            if len(hits) >= limit:
                self._hits[key_id] = hits
                # The oldest hit leaves the window at `oldest + window_s`.
                retry_after = max(1, int(self.window_s - (now - hits[0])) + 1)
                return False, retry_after
            hits.append(now)
            self._hits[key_id] = hits
        return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


RPM = RpmCounter()
INVALID_TOKEN_LOCK = "invalid-token"
# Failed-auth throttle: 10 bad tokens in 5 minutes lock the invalid-token window
# for 5 min, so guessing credentials is bounded without a DB write per attempt.
AUTH_FAILURES = routing.ClientLockouts(max_attempts=10, duration_ms=300_000, window_ms=300_000)


def reset_state() -> None:
    """Drop in-memory counters (tests + a config reload)."""
    RPM.reset()
    AUTH_FAILURES.reset()


# --------------------------------------------------------------------------- #
# Core decisions
# --------------------------------------------------------------------------- #

def _credential(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    api_key = (request.headers.get("x-api-key") or "").strip()
    return api_key or None


def has_enabled_keys(db: Session) -> bool:
    return bool(db.scalar(
        select(func.count()).select_from(ClientKey).where(ClientKey.enabled.is_(True))
    ))


def authenticate(request: Request, db: Session | None = None) -> ClientPrincipal | None:
    """Resolve the caller's `ClientKey`, or `None` for anonymous access.

    Raises `AuthError(401)` for an unknown/disabled/empty token, and
    `AuthError(401)` for a missing token when `NOVA_REQUIRE_API_KEY` is set or
    the deployment has at least one enabled client key.

    The resolved principal is also stashed on `request.state` so the request
    logger and the usage attribution can see which key served the call.
    """
    token = _credential(request)
    # One process-wide failure window for invalid tokens: a credential-guessing
    # client is throttled regardless of which token value it cycles through.
    # A missing token (401 missing_api_key) is not a failure attempt.
    failure_key = INVALID_TOKEN_LOCK

    returned_db = db is None
    session = db or SessionLocal()
    try:
        lock = AUTH_FAILURES.check(failure_key)
        if lock.get("locked"):
            retry_after = max(1, int(lock.get("retry_after_ms", 0) / 1000))
            raise AuthError(429, CODE_RPM_EXCEEDED, "Too many failed authentication attempts.",
                            retry_after=retry_after)

        if token:
            row = session.scalars(select(ClientKey).where(ClientKey.token == token)).first()
            if row is None or not row.enabled:
                AUTH_FAILURES.record_failure(failure_key)
                raise AuthError(401, CODE_INVALID_KEY, "Invalid or disabled API key.")
            AUTH_FAILURES.record_success(failure_key)
            principal = ClientPrincipal(key=row)
        else:
            if REQUIRE_API_KEY or has_enabled_keys(session):
                raise AuthError(401, CODE_MISSING_KEY,
                                "An API key is required - send 'Authorization: Bearer <token>'.")
            principal = ClientPrincipal(anonymous=True)
    finally:
        if returned_db:
            session.close()

    request.state.client_principal = principal
    if principal.key is not None:
        request.state.client_key = principal.key
    return principal


def enforce(client_key: ClientKey | None, requested_model: str) -> None:
    """Apply the key's model allowlist, RPM and daily token budget.

    Raises `AuthError` (403/429) with `Retry-After` where it is meaningful.
    A `None` key (anonymous) is never limited here: anonymity is gated by
    `authenticate`, not by a fabricated quota.
    """
    if client_key is None:
        return
    allowed = parse_allowed_models(client_key.allowedModels)
    if not model_allowed(allowed, requested_model):
        raise AuthError(
            403, CODE_MODEL_FORBIDDEN,
            f"This API key is not allowed to use model '{requested_model}'.",
        )
    rpm = int(client_key.rpmLimit or 0)
    ok, retry_after = RPM.check(int(client_key.id), rpm)
    if not ok:
        raise AuthError(
            429, CODE_RPM_EXCEEDED,
            f"Rate limit exceeded for this API key ({rpm} requests/minute).",
            retry_after=retry_after,
        )
    tpd = int(client_key.tpdLimit or 0)
    if tpd > 0 and (int(client_key.tokensIn or 0) + int(client_key.tokensOut or 0)) >= tpd:
        raise AuthError(
            429, CODE_TPD_EXCEEDED,
            f"Daily token budget exhausted for this API key ({tpd} tokens/day).",
            retry_after=seconds_until_utc_midnight(),
        )


def seconds_until_utc_midnight() -> int:
    now = utcnow()
    tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return max(1, int((tomorrow - now).total_seconds()))


# --------------------------------------------------------------------------- #
# Attribution - the single writer of per-key usage
# --------------------------------------------------------------------------- #

def attribute(client_key: ClientKey | None, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Bump `reqCount` / `lastUsedAt` and add tokens on the row in memory."""
    if client_key is None:
        return
    client_key.reqCount = int(client_key.reqCount or 0) + 1
    client_key.tokensIn = int(client_key.tokensIn or 0) + max(0, int(tokens_in or 0))
    client_key.tokensOut = int(client_key.tokensOut or 0) + max(0, int(tokens_out or 0))
    client_key.lastUsedAt = utcnow()


def update_usage(db: Session, client_key_id: int | None, tokens_in: int = 0,
                 tokens_out: int = 0) -> None:
    """Persist one request's usage for a client key. Never raises.

    Streaming endpoints call this from their finalizer with their own session,
    so usage is written even though the key row was loaded by another session.
    """
    if client_key_id is None:
        return
    try:
        row = db.get(ClientKey, int(client_key_id))
        if row is None:
            return
        attribute(row, tokens_in, tokens_out)
        db.commit()
    except Exception:  # usage bookkeeping must never break a served response
        try:
            db.rollback()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# FastAPI dependency
# --------------------------------------------------------------------------- #

def gateway_auth(request: Request, db: Session = Depends(get_db)) -> ClientPrincipal | None:
    """Dependency for every gateway route: authenticate, then enforce RPM.

    Model-scoped rules (`allowedModels`) need the parsed body, so endpoints call
    `enforce(principal.key, requested_model)` once they know the model. This
    dependency owns the credential plus the RPM budget, which applies to every
    call regardless of payload.
    """
    principal = authenticate(request, db)
    if principal is not None and principal.key is not None:
        rpm = int(principal.key.rpmLimit or 0)
        ok, retry_after = RPM.check(int(principal.key.id), rpm)
        if not ok:
            raise AuthError(
                429, CODE_RPM_EXCEEDED,
                f"Rate limit exceeded for this API key ({rpm} requests/minute).",
                retry_after=retry_after,
            )
    return principal


def require_client_key(request: Request) -> ClientKey | None:
    """The resolved `ClientKey` for a gateway request (or `None`)."""
    principal = getattr(request.state, "client_principal", None)
    return principal.key if isinstance(principal, ClientPrincipal) else None
