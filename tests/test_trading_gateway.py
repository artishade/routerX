"""Gateway trading endpoints: scopes, envelopes, typed errors, RequestLog.

Builds a tiny FastAPI app that mounts the real trading router under /v1 and
/api/v1, overrides get_db with in-memory SQLite, and patches the service reads
with AsyncMock fixtures. No exchange HTTP is involved.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import auth as nova_auth
from nova import trading
from nova.database import get_db
from nova.models import Base, ClientKey, RequestLog
from routers import trading as trading_router

QUOTE = {
    "exchange": "binance", "symbol": "BTC/USDT", "base_asset": "BTC",
    "quote_asset": "USDT", "bid": 100.0, "ask": 101.0, "last": 100.5,
    "ts": "2024-01-01T00:00:00Z", "source": "binance",
}
POSITIONS = {"exchange": "binance", "as_of": "2024-01-01T00:00:00Z", "positions": []}
BALANCES = {"exchange": "binance", "as_of": "2024-01-01T00:00:00Z", "balances": []}


class TradingGatewayApi:
    def __init__(self):
        self.engine = create_engine(
            "sqlite://", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = FastAPI()

        @self.app.exception_handler(nova_auth.AuthError)
        async def _auth_error(request: Request, exc: nova_auth.AuthError):
            return JSONResponse(
                exc.to_payload(), status_code=exc.status, headers=exc.headers()
            )

        self.app.include_router(trading_router.router, prefix="/v1")
        self.app.include_router(trading_router.router, prefix="/api/v1")

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = _db
        self.client = TestClient(self.app)

    def add_key(self, token="nova-sk-trade", scopes="", **overrides):
        with self.Session() as db:
            key = ClientKey(
                name=overrides.pop("name", "trader"),
                token=token,
                tradingScopes=scopes,
                **overrides,
            )
            db.add(key)
            db.commit()
            return key.id

    def headers(self, token="nova-sk-trade"):
        return {"Authorization": f"Bearer {token}"}

    def request_logs(self):
        with self.Session() as db:
            return db.scalars(select(RequestLog).order_by(RequestLog.id)).all()


def patched_services(quote=None, positions=None, balances=None):
    return (
        patch("nova.trading.read_quote", new=quote or AsyncMock(return_value=QUOTE)),
        patch("nova.trading.read_positions", new=positions or AsyncMock(return_value=POSITIONS)),
        patch("nova.trading.read_balances", new=balances or AsyncMock(return_value=BALANCES)),
    )


class TradingAuthTests(unittest.TestCase):
    def setUp(self):
        nova_auth.reset_state()
        self.api = TradingGatewayApi()

    def test_trading_requires_a_key_even_when_text_is_fail_open(self):
        response = self.api.client.get(
            "/v1/trading/quote", params={"symbol": "BTC/USDT"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_MISSING_KEY)

    def test_missing_symbol_without_key_is_still_401(self):
        response = self.api.client.get("/v1/trading/quote")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_MISSING_KEY)

    def test_key_without_trading_scopes_is_403(self):
        self.api.add_key("nova-sk-none", scopes="")
        response = self.api.client.get(
            "/v1/trading/quote", params={"symbol": "BTC/USDT"},
            headers=self.api.headers("nova-sk-none"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_SCOPE_FORBIDDEN)

    def test_narrow_quote_scope_does_not_grant_positions_or_balances(self):
        self.api.add_key("nova-sk-quote", scopes="trading:quote")
        quote_mock, positions_mock, balances_mock = patched_services()
        with quote_mock, positions_mock, balances_mock:
            headers = self.api.headers("nova-sk-quote")
            quote = self.api.client.get(
                "/v1/trading/quote", params={"symbol": "BTC/USDT"}, headers=headers
            )
            positions = self.api.client.get("/v1/trading/positions", headers=headers)
            balances = self.api.client.get("/v1/trading/balances", headers=headers)

        self.assertEqual(quote.status_code, 200)
        self.assertEqual(positions.status_code, 403)
        self.assertEqual(balances.status_code, 403)
        self.assertEqual(
            positions.json()["error"]["code"], nova_auth.CODE_SCOPE_FORBIDDEN
        )
        self.assertEqual(
            balances.json()["error"]["code"], nova_auth.CODE_SCOPE_FORBIDDEN
        )

    def test_trading_read_umbrella_grants_all_three(self):
        self.api.add_key("nova-sk-read", scopes="trading:read")
        quote_mock, positions_mock, balances_mock = patched_services()
        with quote_mock, positions_mock, balances_mock:
            headers = self.api.headers("nova-sk-read")
            responses = [
                self.api.client.get(
                    "/v1/trading/quote", params={"symbol": "BTC/USDT"}, headers=headers
                ),
                self.api.client.get("/v1/trading/positions", headers=headers),
                self.api.client.get("/v1/trading/balances", headers=headers),
            ]
        self.assertEqual([r.status_code for r in responses], [200, 200, 200])

    def test_star_scope_grants_all_three(self):
        self.api.add_key("nova-sk-star", scopes="*")
        quote_mock, positions_mock, balances_mock = patched_services()
        with quote_mock, positions_mock, balances_mock:
            headers = self.api.headers("nova-sk-star")
            responses = [
                self.api.client.get(
                    "/v1/trading/quote", params={"symbol": "BTC/USDT"}, headers=headers
                ),
                self.api.client.get("/v1/trading/positions", headers=headers),
                self.api.client.get("/v1/trading/balances", headers=headers),
            ]
        self.assertEqual([r.status_code for r in responses], [200, 200, 200])

    def test_api_v1_prefix_serves_the_same_route(self):
        self.api.add_key("nova-sk-alias", scopes="trading:quote")
        quote_mock, positions_mock, balances_mock = patched_services()
        with quote_mock, positions_mock, balances_mock:
            response = self.api.client.get(
                "/api/v1/trading/quote", params={"symbol": "BTC/USDT"},
                headers=self.api.headers("nova-sk-alias"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "trading.quote")


class TradingEnvelopeTests(unittest.TestCase):
    def setUp(self):
        nova_auth.reset_state()
        self.api = TradingGatewayApi()
        self.api.add_key("nova-sk-envelope", scopes="trading:read")

    def test_response_envelope_has_no_fallback(self):
        quote_mock, positions_mock, balances_mock = patched_services()
        with quote_mock as quote, positions_mock, balances_mock:
            response = self.api.client.get(
                "/v1/trading/quote", params={"symbol": "BTC/USDT"},
                headers=self.api.headers("nova-sk-envelope"),
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "trading.quote")
        self.assertEqual(body["data"], QUOTE)
        self.assertEqual(body["_nova"], {
            "exchange": "binance", "provider": "binance",
            "fallback": False, "cached": False, "stage": 1,
        })
        quote.assert_awaited_once()
        kwargs = quote.await_args.kwargs
        self.assertEqual(kwargs["actor"], "gateway")
        self.assertIsNotNone(kwargs["client_key_id"])

    def test_trading_error_is_a_typed_payload(self):
        err = trading.TradingError(
            503, "trading_disabled",
            "Trading is disabled by the operator's kill-switch.",
        )
        quote_mock, positions_mock, balances_mock = patched_services(
            quote=AsyncMock(side_effect=err)
        )
        with quote_mock, positions_mock, balances_mock:
            response = self.api.client.get(
                "/v1/trading/quote", params={"symbol": "BTC/USDT"},
                headers=self.api.headers("nova-sk-envelope"),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), err.to_payload())

    def test_missing_symbol_is_typed_invalid_symbol(self):
        with patch("nova.trading.trading_audit.record_standalone"):
            response = self.api.client.get(
                "/v1/trading/quote",
                headers=self.api.headers("nova-sk-envelope"),
            )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["type"], "trading_error")
        self.assertEqual(body["error"]["code"], "invalid_symbol")
        self.assertIn("Invalid symbol", body["error"]["message"])

    def test_success_writes_one_request_log(self):
        quote_mock, positions_mock, balances_mock = patched_services()
        with quote_mock, positions_mock, balances_mock:
            response = self.api.client.get(
                "/v1/trading/quote", params={"symbol": "BTC/USDT"},
                headers=self.api.headers("nova-sk-envelope"),
            )
        self.assertEqual(response.status_code, 200)
        rows = self.api.request_logs()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.endpoint, "/v1/trading/quote")
        self.assertEqual(row.via, "trading")
        self.assertEqual(row.status, 200)
        self.assertEqual(row.model, "BTC/USDT")
        self.assertEqual(row.providerName, "binance")
        self.assertEqual(row.error, "")
        self.assertEqual(row.clientName, "trader")
        self.assertIsNotNone(row.clientKeyId)

    def test_service_is_called_once_without_fallback(self):
        service = AsyncMock(side_effect=trading.TradingError(
            502, "exchange_error", "The exchange failed the request (HTTP 500)."
        ))
        quote_mock, positions_mock, balances_mock = patched_services(quote=service)
        with quote_mock, positions_mock, balances_mock as balances:
            response = self.api.client.get(
                "/v1/trading/quote", params={"symbol": "BTC/USDT"},
                headers=self.api.headers("nova-sk-envelope"),
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "exchange_error")
        self.assertEqual(service.await_count, 1)
        balances.assert_not_awaited()


class TradingAuthLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Auth failures are logged once, by the app's AuthError handler."""

    def setUp(self):
        nova_auth.reset_state()
        self.api = TradingGatewayApi()

    async def test_auth_failure_does_not_write_a_request_log(self):
        db = self.api.Session()
        try:
            with patch.object(trading_router, "_write_log") as write_log:
                with self.assertRaises(nova_auth.AuthError):
                    await trading_router.trading_quote(
                        symbol="BTC/USDT",
                        exchange=None,
                        principal=nova_auth.ClientPrincipal(anonymous=True),
                        db=db,
                    )
                write_log.assert_not_called()
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
