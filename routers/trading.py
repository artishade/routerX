"""Read-only trading gateway (Phase 1).

  GET /v1/trading/quote?symbol=BTC/USDT[&exchange=binance]
  GET /v1/trading/positions[?exchange=binance]
  GET /v1/trading/balances[?exchange=binance]

Mounted by `routers/gateway.py` inside the gateway router, so the same routes
are served under `/api/v1` as well. Trading never inherits anonymous access:
every endpoint requires a `ClientKey` with the endpoint's trading scope (or the
`trading:read` umbrella). There is deliberately no fallback chain here - one
typed decision per call - but the response keeps the gateway envelope and every
call writes one RequestLog row.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from nova import auth as nova_auth
from nova import trading
from nova.database import get_db
from nova.models import RequestLog

log = logging.getLogger("nova.trading.gateway")

router = APIRouter()


def _write_log(db: Session, **payload) -> None:
    """One RequestLog row per trading call. Never raises to the caller.

    `write_log` lives in routers/gateway.py, which imports this module at the
    bottom - importing it lazily at call time avoids the import cycle. A local
    writer is the fallback if that import is unavailable (e.g. a test app that
    mounts only this router).
    """
    try:
        from .gateway import write_log

        write_log(db, **payload)
        return
    except Exception:
        pass
    try:
        db.add(RequestLog(
            model=payload.get("model", ""),
            upstreamModel=payload.get("upstream_model", ""),
            providerName=payload.get("provider_name", ""),
            endpoint=payload.get("endpoint", ""),
            status=payload.get("status", 200),
            latencyMs=payload.get("latency_ms", 0),
            tokensIn=payload.get("tokens_in", 0),
            tokensOut=payload.get("tokens_out", 0),
            via=payload.get("via", ""),
            spoofed=payload.get("spoofed", False),
            error=payload.get("error", ""),
            clientKeyId=payload.get("client_key_id"),
            clientName=payload.get("client_name", ""),
            ttftMs=payload.get("ttft_ms", 0),
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


async def _serve(
    *,
    action: str,
    scope: str,
    endpoint: str,
    model: str,
    exchange: str | None,
    principal: nova_auth.ClientPrincipal,
    db: Session,
    call,
) -> JSONResponse:
    started = time.perf_counter()
    status = 200
    error = ""
    skip_log = False
    provider_name = str(exchange or "")
    try:
        nova_auth.require_trading_scope(principal, scope)
        data = await call()
        provider_name = str(data.get("exchange") or provider_name)
        return JSONResponse({
            "object": action,
            "data": data,
            "_nova": {
                "exchange": provider_name,
                "provider": provider_name,
                "fallback": False,
                "cached": False,
                "stage": 1,
            },
        }, status_code=200)
    except nova_auth.AuthError as err:
        status = err.status
        error = f"{err.code}: {err.message}"
        skip_log = True
        raise
    except trading.TradingError as err:
        status = err.status
        error = f"{err.code}: {err.message}"
        headers = (
            {"Retry-After": str(err.retry_after)}
            if err.retry_after is not None
            else None
        )
        return JSONResponse(err.to_payload(), status_code=err.status, headers=headers)
    except Exception:
        status = 502
        error = "trading_internal_error: Trading read failed"
        log.exception("trading read failed (action=%s)", action)
        return JSONResponse({
            "error": {
                "message": "Trading read failed",
                "type": "trading_error",
                "code": "trading_internal_error",
            }
        }, status_code=502)
    finally:
        if not skip_log:
            _write_log(
                db,
                model=model or "",
                provider_name=provider_name,
                endpoint=endpoint,
                status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                via="trading",
                error=error,
                client_key_id=getattr(principal, "key_id", None),
                client_name=getattr(principal, "name", ""),
            )


@router.get("/trading/quote")
async def trading_quote(
    symbol: str = "",
    exchange: str | None = None,
    principal: nova_auth.ClientPrincipal = Depends(nova_auth.gateway_auth),
    db: Session = Depends(get_db),
):
    async def call():
        return await trading.read_quote(
            db,
            symbol=symbol,
            exchange=exchange,
            actor="gateway",
            client_key_id=principal.key_id,
        )

    return await _serve(
        action="trading.quote",
        scope="trading:quote",
        endpoint="/v1/trading/quote",
        model=symbol,
        exchange=exchange,
        principal=principal,
        db=db,
        call=call,
    )


@router.get("/trading/positions")
async def trading_positions(
    exchange: str | None = None,
    principal: nova_auth.ClientPrincipal = Depends(nova_auth.gateway_auth),
    db: Session = Depends(get_db),
):
    async def call():
        return await trading.read_positions(
            db,
            exchange=exchange,
            actor="gateway",
            client_key_id=principal.key_id,
        )

    return await _serve(
        action="trading.positions",
        scope="trading:positions",
        endpoint="/v1/trading/positions",
        model="",
        exchange=exchange,
        principal=principal,
        db=db,
        call=call,
    )


@router.get("/trading/balances")
async def trading_balances(
    exchange: str | None = None,
    principal: nova_auth.ClientPrincipal = Depends(nova_auth.gateway_auth),
    db: Session = Depends(get_db),
):
    async def call():
        return await trading.read_balances(
            db,
            exchange=exchange,
            actor="gateway",
            client_key_id=principal.key_id,
        )

    return await _serve(
        action="trading.balances",
        scope="trading:balances",
        endpoint="/v1/trading/balances",
        model="",
        exchange=exchange,
        principal=principal,
        db=db,
        call=call,
    )


__all__ = ["router"]
