"""Trading service (Phase 1): read-only market and account data.

Phase 0 defined the error contract and the kill-switch gate. This module adds
the shared read-only service every trading entry point uses:

  * symbol + credential parsing (`Market`, `parse_symbol`, `parse_credentials`)
  * exchange resolution from the provider catalogue (`resolve_exchange`)
  * three audited reads (`read_quote`, `read_positions`, `read_balances`)
  * Binance and Coinbase read-only adapters

Every read is fail-closed: the kill-switch is checked first, exactly ONE
exchange provider serves the call (there is no fallback chain), credentials are
picked deterministically, and every outcome is appended to the trading audit
trail. Wire data is snake_case with float amounts; DB columns stay camelCase.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, trading_audit, trading_settings
from .models import Provider, ProviderKey, utcnow

log = logging.getLogger(__name__)

__all__ = [
    "TradingError",
    "Market",
    "TRANSPORT",
    "SUPPORTED_EXCHANGES",
    "ensure_trading_enabled",
    "parse_symbol",
    "parse_credentials",
    "resolve_exchange",
    "read_quote",
    "read_positions",
    "read_balances",
]

SUPPORTED_EXCHANGES = ("binance", "coinbase")

# Concatenated symbols are split on a known quote asset, longest first, so
# `BTCFDUSD` never becomes base `BTCF` + quote `USD`.
_QUOTE_ASSETS = ("FDUSD", "USDT", "USDC", "USD", "EUR", "GBP", "BTC", "ETH", "BNB", "JPY", "KRW")


class TradingError(Exception):
    """Typed trading refusal/error with a protocol-shaped payload."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        retry_after: int | None = None,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.retry_after = retry_after
        self.extra = dict(extra) if isinstance(extra, dict) else None

    def to_payload(self) -> dict:
        payload: dict = {
            "error": {
                "message": self.message,
                "type": "trading_error",
                "code": self.code,
            }
        }
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        if self.extra:
            payload.update(self.extra)
        return payload


def ensure_trading_enabled(db: Session) -> None:
    """Raise a 503 trading_disabled when the operator engaged the kill-switch."""
    if trading_settings.kill_switch_engaged(db):
        raise TradingError(
            503,
            "trading_disabled",
            "Trading is disabled by the operator's kill-switch.",
        )


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Market:
    """A canonical trading pair: `symbol` is always `BASE/QUOTE`."""

    symbol: str
    base: str
    quote: str


def _invalid_symbol(raw: object) -> TradingError:
    return TradingError(
        400,
        "invalid_symbol",
        f"Invalid symbol '{raw}'. Use BASE/QUOTE, e.g. BTC/USDT.",
    )


def parse_symbol(raw: str) -> Market:
    """Normalize `BTC/USDT`, `BTC-USDT`, `BTC_USDT` or `BTCUSDT`."""
    text = str(raw or "").strip().upper()
    if not text:
        raise _invalid_symbol(raw)

    for separator in ("/", "-", "_"):
        if separator in text:
            base, _, quote = text.partition(separator)
            base, quote = base.strip(), quote.strip()
            if not base or not quote or not base.isalnum() or not quote.isalnum():
                raise _invalid_symbol(raw)
            return Market(symbol=f"{base}/{quote}", base=base, quote=quote)

    if not text.isalnum():
        raise _invalid_symbol(raw)
    for quote in _QUOTE_ASSETS:
        if len(text) > len(quote) and text.endswith(quote):
            base = text[: -len(quote)]
            return Market(symbol=f"{base}/{quote}", base=base, quote=quote)
    raise _invalid_symbol(raw)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def _first_text(data: dict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            continue
        return str(value).strip()
    return ""


def parse_credentials(raw: str) -> dict:
    """Parse an exchange credential blob into api_key/api_secret/passphrase.

    Accepts a JSON object (`api_key`/`apiKey`/`key`,
    `api_secret`/`apiSecret`/`secret`, `passphrase`/`password`) or the compact
    `key:secret` / bare-key forms. Never logs or echoes the input.
    """
    text = str(raw or "").strip()
    if not text:
        return {"api_key": "", "api_secret": "", "passphrase": ""}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as err:
            raise TradingError(400, "invalid_credentials", "Exchange credentials are not valid JSON.") from err
        if not isinstance(data, dict):
            raise TradingError(400, "invalid_credentials", "Exchange credentials JSON must be an object.")
        return {
            "api_key": _first_text(data, "api_key", "apiKey", "key"),
            "api_secret": _first_text(data, "api_secret", "apiSecret", "secret"),
            "passphrase": _first_text(data, "passphrase", "password"),
        }
    key, separator, secret = text.partition(":")
    return {
        "api_key": key.strip(),
        "api_secret": secret.strip() if separator else "",
        "passphrase": "",
    }


# --------------------------------------------------------------------------- #
# Exchange resolution
# --------------------------------------------------------------------------- #

def _provider_info(row: Provider) -> dict:
    return {
        "id": row.id,
        "key": row.key or "",
        "name": row.name or "",
        "base_url": (row.baseUrl or "").strip().rstrip("/"),
    }


def resolve_exchange(db: Session, exchange: str | None = None) -> dict:
    """Resolve exactly one enabled `kind='exchange'` provider.

    A named provider must exist and be enabled. When the caller omits the name,
    the deployment must have exactly one enabled exchange; multiple enabled
    exchanges are a typed 400 (`exchange_required`) instead of an arbitrary
    pick, so a read is never silently sent somewhere unexpected.
    """
    wanted = str(exchange or "").strip()
    rows = db.scalars(
        select(Provider).where(Provider.kind == "exchange").order_by(Provider.id)
    ).all()

    if wanted:
        row = next((r for r in rows if (r.key or "").lower() == wanted.lower()), None)
        if row is None:
            raise TradingError(
                404,
                "exchange_not_found",
                f"Exchange '{wanted}' is not configured. Add a provider with kind='exchange'.",
            )
        if not row.enabled:
            raise TradingError(503, "exchange_disabled", f"Exchange '{row.key}' is disabled.")
        return _provider_info(row)

    enabled = [row for row in rows if row.enabled]
    if not enabled:
        raise TradingError(
            503,
            "exchange_not_configured",
            "No enabled exchange provider is configured. Add one with kind='exchange'.",
        )
    if len(enabled) > 1:
        keys = sorted((row.key or "") for row in enabled)
        raise TradingError(
            400,
            "exchange_required",
            "More than one exchange is enabled; pass ?exchange=... to choose one.",
            extra={"exchanges": keys},
        )
    return _provider_info(enabled[0])


def _adapter_for(provider: dict) -> str:
    """Classify the provider by key prefix or base_url hostname."""
    key = str(provider.get("key") or "").lower()
    host = (urlsplit(str(provider.get("base_url") or "")).hostname or "").lower()
    if key.startswith("binance") or "binance" in host:
        return "binance"
    if key.startswith("coinbase") or "coinbase" in host:
        return "coinbase"
    name = provider.get("key") or host or "unknown"
    raise TradingError(
        501,
        "unsupported_exchange",
        f"No read-only adapter for exchange '{name}'. Supported: binance, coinbase.",
    )


# --------------------------------------------------------------------------- #
# Small value helpers
# --------------------------------------------------------------------------- #

def _base_url(provider: dict) -> str:
    return str(provider.get("base_url") or "").rstrip("/")


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any, default: float = 0.0) -> float:
    number = _number(value)
    return default if number is None else number


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_iso(value: Any) -> str:
    try:
        return _iso(datetime.fromtimestamp(int(float(value)) / 1000.0, tz=timezone.utc))
    except (TypeError, ValueError, OSError, OverflowError):
        return _now_iso()


def _exchange_iso(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return _now_iso()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _iso(parsed)
    return _now_iso()


def _canonical_symbol(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return parse_symbol(text).symbol
    except TradingError:
        return text.upper()


# --------------------------------------------------------------------------- #
# HTTP (module-level TRANSPORT is the test seam)
# --------------------------------------------------------------------------- #

TRANSPORT: httpx.AsyncBaseTransport | None = None


def _bad_response(message: str = "The exchange returned an unexpected response.") -> TradingError:
    return TradingError(502, "exchange_bad_response", message)


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError):
        return None


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _upstream_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for name in ("msg", "message", "error"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _raise_exchange_status(response: httpx.Response, payload: Any) -> None:
    status = response.status_code
    if status in (401, 403):
        raise TradingError(
            502,
            "exchange_auth_failed",
            "The exchange rejected the configured API credentials.",
        )
    if status == 429:
        raise TradingError(
            503,
            "exchange_rate_limited",
            "The exchange rate-limited this request.",
            retry_after=_retry_after(response),
        )
    if status >= 500:
        raise TradingError(502, "exchange_error", f"The exchange failed the request (HTTP {status}).")
    if status >= 400:
        message = f"The exchange rejected the request (HTTP {status})."
        detail = _upstream_message(payload)
        if detail:
            message = f"{message} {detail}"
        raise TradingError(400, "exchange_rejected", message)


async def _request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> Any:
    """One HTTP call with the trading timeout, mapped to typed TradingErrors."""
    try:
        async with httpx.AsyncClient(
            timeout=config.TRADING_TIMEOUT_S,
            transport=TRANSPORT,
            trust_env=False,
        ) as client:
            response = await client.request(method, url, params=params, headers=headers)
    except httpx.TimeoutException as err:
        raise TradingError(
            504,
            "exchange_timeout",
            "The exchange did not answer within the trading timeout.",
        ) from err
    except httpx.HTTPError as err:
        raise TradingError(502, "exchange_unreachable", "The exchange could not be reached.") from err

    payload = _json_or_none(response)
    if response.status_code >= 400:
        _raise_exchange_status(response, payload)
    if payload is None:
        raise _bad_response("The exchange returned a non-JSON response.")
    return payload


# --------------------------------------------------------------------------- #
# Credential selection
# --------------------------------------------------------------------------- #

def _pick_provider_key(db: Session, provider_id: int) -> ProviderKey | None:
    """Highest-weight enabled key whose cooldown (if any) has expired."""
    now = utcnow()
    rows = db.scalars(
        select(ProviderKey)
        .where(ProviderKey.providerId == provider_id, ProviderKey.enabled.is_(True))
        .order_by(ProviderKey.weight.desc(), ProviderKey.id.asc())
    ).all()
    for row in rows:
        if row.cooldownUntil is not None and row.cooldownUntil > now:
            continue
        return row
    return None


def _signing_credentials(db: Session, provider: dict, adapter: str) -> dict:
    row = _pick_provider_key(db, int(provider["id"]))
    credentials = parse_credentials(row.apiKey if row is not None else "")
    missing = not credentials["api_key"] or not credentials["api_secret"]
    if adapter == "coinbase" and not credentials["passphrase"]:
        missing = True
    if row is None or missing:
        raise TradingError(
            503,
            "exchange_credentials_missing",
            "This exchange operation needs an API key + secret. Add a key (JSON or key:secret).",
        )
    return credentials


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

def _audit_success(
    *,
    action: str,
    actor: str,
    client_key_id: int | None,
    provider: dict,
    symbol: str,
    count: int,
) -> None:
    trading_audit.record_standalone(
        event="read",
        action=action,
        status="ok",
        actor=actor,
        client_key_id=client_key_id,
        provider_id=provider.get("id"),
        provider_key=provider.get("key", ""),
        exchange=provider.get("key", ""),
        symbol=symbol or "",
        payload={"count": int(count)},
    )


def _audit_refusal(
    *,
    action: str,
    actor: str,
    client_key_id: int | None,
    provider: dict | None,
    exchange: str,
    symbol: str,
    err: TradingError,
) -> None:
    trading_audit.record_standalone(
        event="refusal",
        action=action,
        status="error",
        code=err.code,
        actor=actor,
        client_key_id=client_key_id,
        provider_id=(provider or {}).get("id"),
        provider_key=(provider or {}).get("key") or exchange or "",
        exchange=(provider or {}).get("key") or exchange or "",
        symbol=symbol or "",
        message=err.message,
    )


# --------------------------------------------------------------------------- #
# Binance adapter
# --------------------------------------------------------------------------- #

def _binance_signed_url(
    provider: dict,
    path: str,
    *,
    api_key: str,
    api_secret: str,
    extra: list[tuple[str, str]] | None = None,
) -> tuple[str, dict]:
    query_pairs = [("timestamp", str(int(time.time() * 1000))), ("recvWindow", "5000")]
    if extra:
        query_pairs.extend(extra)
    query = urlencode(query_pairs)
    signature = hmac.new(
        api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{_base_url(provider)}{path}?{query}&signature={signature}", {"X-MBX-APIKEY": api_key}


def _binance_is_futures(provider: dict) -> bool:
    host = (urlsplit(_base_url(provider)).hostname or "").lower()
    return "fapi" in host


async def _binance_quote(provider: dict, market: Market) -> dict:
    path = "/fapi/v1/ticker/24hr" if _binance_is_futures(provider) else "/api/v3/ticker/24hr"
    payload = await _request_json(
        "GET",
        f"{_base_url(provider)}{path}",
        params={"symbol": f"{market.base}{market.quote}"},
    )
    if not isinstance(payload, dict):
        raise _bad_response()
    return {
        "exchange": provider["key"],
        "symbol": market.symbol,
        "base_asset": market.base,
        "quote_asset": market.quote,
        "bid": _number(payload.get("bidPrice")),
        "ask": _number(payload.get("askPrice")),
        "last": _number(payload.get("lastPrice")),
        "ts": _ms_iso(payload.get("closeTime")),
        "source": provider["key"],
    }


async def _binance_balances(db: Session, provider: dict) -> dict:
    credentials = _signing_credentials(db, provider, "binance")
    url, headers = _binance_signed_url(
        provider,
        "/api/v3/account",
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
    )
    payload = await _request_json("GET", url, headers=headers)
    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
        raise _bad_response()

    balances: list[dict] = []
    for row in payload["balances"]:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("asset") or "").strip()
        if not asset:
            continue
        free = _float(row.get("free"))
        locked = _float(row.get("locked"))
        total = free + locked
        if total <= 0:
            continue
        balances.append({"asset": asset, "free": free, "locked": locked, "total": total})
    return {"exchange": provider["key"], "as_of": _now_iso(), "balances": balances}


async def _binance_positions(db: Session, provider: dict) -> dict:
    if not _binance_is_futures(provider):
        raise TradingError(
            501,
            "positions_unsupported",
            "Binance spot does not expose positions; configure the futures API root or use balances.",
        )
    credentials = _signing_credentials(db, provider, "binance")
    url, headers = _binance_signed_url(
        provider,
        "/fapi/v2/positionRisk",
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
    )
    payload = await _request_json("GET", url, headers=headers)
    if not isinstance(payload, list):
        raise _bad_response()

    positions: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        amount = _number(row.get("positionAmt")) or 0.0
        if amount == 0:
            continue
        positions.append({
            "symbol": _canonical_symbol(row.get("symbol")),
            "side": "long" if amount > 0 else "short",
            "quantity": abs(amount),
            "entry_price": _float(row.get("entryPrice")),
            "mark_price": _float(row.get("markPrice")),
            "unrealized_pnl": _float(row.get("unRealizedProfit")),
            "leverage": _integer(row.get("leverage")),
        })
    return {"exchange": provider["key"], "as_of": _now_iso(), "positions": positions}


# --------------------------------------------------------------------------- #
# Coinbase adapter
# --------------------------------------------------------------------------- #

async def _coinbase_quote(provider: dict, market: Market) -> dict:
    payload = await _request_json(
        "GET",
        f"{_base_url(provider)}/products/{market.base}-{market.quote}/ticker",
    )
    if not isinstance(payload, dict):
        raise _bad_response()
    return {
        "exchange": provider["key"],
        "symbol": market.symbol,
        "base_asset": market.base,
        "quote_asset": market.quote,
        "bid": _number(payload.get("bid")),
        "ask": _number(payload.get("ask")),
        "last": _number(payload.get("price")),
        "ts": _exchange_iso(payload.get("time")),
        "source": provider["key"],
    }


def _coinbase_amount(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("value")
    return _float(value)


async def _coinbase_balances(db: Session, provider: dict) -> dict:
    credentials = _signing_credentials(db, provider, "coinbase")
    base = _base_url(provider)
    url = f"{base}/accounts"
    params = {"limit": "250"}
    path = urlsplit(url).path or "/accounts"
    timestamp = str(int(time.time()))
    signing = f"{timestamp}GET{path}?limit=250"
    signature = base64.b64encode(
        hmac.new(
            credentials["api_secret"].encode("utf-8"),
            signing.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    headers = {
        "CB-ACCESS-KEY": credentials["api_key"],
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "CB-ACCESS-PASSPHRASE": credentials["passphrase"],
        "CB-VERSION": "2024-01-01",
    }
    payload = await _request_json("GET", url, params=params, headers=headers)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        rows = payload["accounts"]
    else:
        raise _bad_response()

    balances: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("currency") or "").strip()
        if not asset:
            continue
        if "balance" in row:
            # Exchange /accounts shape: balance is the total, hold is locked,
            # available is the free amount (derive it when absent).
            total = _float(row.get("balance"))
            locked = _float(row.get("hold"))
            free = _number(row.get("available"))
            if free is None:
                free = max(total - locked, 0.0)
        else:
            # Advanced Trade shape: available_balance + hold objects/strings.
            free = _coinbase_amount(row.get("available_balance"))
            if "available_balance" not in row:
                free = _coinbase_amount(row.get("available"))
            locked = _coinbase_amount(row.get("hold"))
            total = free + locked
        if total <= 0:
            continue
        balances.append({"asset": asset, "free": free, "locked": locked, "total": total})
    return {"exchange": provider["key"], "as_of": _now_iso(), "balances": balances}


# --------------------------------------------------------------------------- #
# Public reads
# --------------------------------------------------------------------------- #

async def read_quote(
    db: Session,
    *,
    symbol: str,
    exchange: str | None = None,
    actor: str = "gateway",
    client_key_id: int | None = None,
) -> dict:
    """Read one bid/ask/last quote from exactly one resolved exchange."""
    action = "trading.quote"
    provider: dict | None = None
    market: Market | None = None
    try:
        ensure_trading_enabled(db)
        market = parse_symbol(symbol)
        provider = resolve_exchange(db, exchange)
        adapter = _adapter_for(provider)
        if adapter == "binance":
            data = await _binance_quote(provider, market)
        else:
            data = await _coinbase_quote(provider, market)
        _audit_success(
            action=action,
            actor=actor,
            client_key_id=client_key_id,
            provider=provider,
            symbol=market.symbol,
            count=1,
        )
        return data
    except TradingError as err:
        _audit_refusal(
            action=action,
            actor=actor,
            client_key_id=client_key_id,
            provider=provider,
            exchange=str(exchange or ""),
            symbol=market.symbol if market is not None else str(symbol or ""),
            err=err,
        )
        raise


async def read_positions(
    db: Session,
    *,
    exchange: str | None = None,
    actor: str = "gateway",
    client_key_id: int | None = None,
) -> dict:
    """Read normalized open positions from exactly one resolved exchange."""
    action = "trading.positions"
    provider: dict | None = None
    try:
        ensure_trading_enabled(db)
        provider = resolve_exchange(db, exchange)
        adapter = _adapter_for(provider)
        if adapter == "binance":
            data = await _binance_positions(db, provider)
        else:
            raise TradingError(
                501,
                "positions_unsupported",
                "Coinbase spot accounts do not expose positions; use balances.",
            )
        _audit_success(
            action=action,
            actor=actor,
            client_key_id=client_key_id,
            provider=provider,
            symbol="",
            count=len(data["positions"]),
        )
        return data
    except TradingError as err:
        _audit_refusal(
            action=action,
            actor=actor,
            client_key_id=client_key_id,
            provider=provider,
            exchange=str(exchange or ""),
            symbol="",
            err=err,
        )
        raise


async def read_balances(
    db: Session,
    *,
    exchange: str | None = None,
    actor: str = "gateway",
    client_key_id: int | None = None,
) -> dict:
    """Read normalized balances (zero-total assets filtered) from one exchange."""
    action = "trading.balances"
    provider: dict | None = None
    try:
        ensure_trading_enabled(db)
        provider = resolve_exchange(db, exchange)
        adapter = _adapter_for(provider)
        if adapter == "binance":
            data = await _binance_balances(db, provider)
        else:
            data = await _coinbase_balances(db, provider)
        _audit_success(
            action=action,
            actor=actor,
            client_key_id=client_key_id,
            provider=provider,
            symbol="",
            count=len(data["balances"]),
        )
        return data
    except TradingError as err:
        _audit_refusal(
            action=action,
            actor=actor,
            client_key_id=client_key_id,
            provider=provider,
            exchange=str(exchange or ""),
            symbol="",
            err=err,
        )
        raise
