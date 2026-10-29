"""Phase-1 trading service: symbols, exchange resolution, adapters, audits.

Offline: in-memory SQLite (StaticPool) and an injected httpx.MockTransport.
`trading_audit.record_standalone` is mocked so the service never needs the
global application database.
"""
import base64
import hashlib
import hmac
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import trading, trading_settings
from nova.models import Base, Provider, ProviderKey, SystemConfig


def make_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


class TradingServiceBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.Session = sessionmaker(bind=cls.engine, expire_on_commit=False)

    def setUp(self):
        with self.Session() as db:
            db.execute(delete(ProviderKey))
            db.execute(delete(Provider))
            db.execute(delete(SystemConfig))
            db.commit()
        trading.TRANSPORT = None
        self.calls = []
        self.audit_patch = patch("nova.trading.trading_audit.record_standalone")
        self.audit = self.audit_patch.start()

    def tearDown(self):
        self.audit_patch.stop()
        trading.TRANSPORT = None

    # -- fixtures ---------------------------------------------------------- #

    def seed_exchange(
        self,
        key="binance",
        base_url="https://api.binance.com",
        enabled=True,
        api_key="key:secret",
        weight=10,
    ):
        with self.Session() as db:
            provider = Provider(
                key=key, name=key.title(), kind="exchange",
                baseUrl=base_url, enabled=enabled,
            )
            db.add(provider)
            db.flush()
            provider_id = provider.id
            if api_key is not None:
                db.add(ProviderKey(
                    providerId=provider_id, label="main",
                    apiKey=api_key, weight=weight, enabled=True,
                ))
            db.commit()
            return provider_id

    def use_transport(self, handler):
        def wrapped(request):
            self.calls.append(request)
            return handler(request)

        trading.TRANSPORT = httpx.MockTransport(wrapped)


class ParseSymbolTests(TradingServiceBase):
    def test_accepts_all_documented_forms(self):
        for raw in ("BTC/USDT", "btc-usdt", "BTC_USDT", "btcusdt", " Btc/Usdt "):
            market = trading.parse_symbol(raw)
            self.assertEqual(market.symbol, "BTC/USDT", raw)
            self.assertEqual(market.base, "BTC", raw)
            self.assertEqual(market.quote, "USDT", raw)

    def test_matches_longest_quote_asset_first(self):
        self.assertEqual(trading.parse_symbol("BTCFDUSD").symbol, "BTC/FDUSD")
        self.assertEqual(trading.parse_symbol("ethbtc").symbol, "ETH/BTC")

    def test_rejects_invalid_symbols(self):
        for raw in ("", "   ", "USDT", "BTC/", "/USDT", "BTC/USDT/XX", "BTC$USDT"):
            with self.assertRaises(trading.TradingError) as ctx:
                trading.parse_symbol(raw)
            self.assertEqual(ctx.exception.status, 400, raw)
            self.assertEqual(ctx.exception.code, "invalid_symbol", raw)


class ParseCredentialsTests(TradingServiceBase):
    def test_json_aliases(self):
        parsed = trading.parse_credentials(
            '{"apiKey": "k", "apiSecret": "s", "password": "p"}'
        )
        self.assertEqual(parsed, {"api_key": "k", "api_secret": "s", "passphrase": "p"})

    def test_compact_and_bare_forms(self):
        self.assertEqual(
            trading.parse_credentials("key:secret"),
            {"api_key": "key", "api_secret": "secret", "passphrase": ""},
        )
        self.assertEqual(
            trading.parse_credentials("bare-key"),
            {"api_key": "bare-key", "api_secret": "", "passphrase": ""},
        )
        self.assertEqual(
            trading.parse_credentials(""),
            {"api_key": "", "api_secret": "", "passphrase": ""},
        )


class ResolveExchangeTests(TradingServiceBase):
    def test_named_missing_is_404(self):
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                trading.resolve_exchange(db, "binance")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "exchange_not_found")

    def test_disabled_is_503(self):
        self.seed_exchange(enabled=False)
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                trading.resolve_exchange(db, "binance")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "exchange_disabled")

    def test_zero_enabled_is_503(self):
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                trading.resolve_exchange(db, None)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "exchange_not_configured")

    def test_multiple_enabled_requires_an_explicit_choice(self):
        self.seed_exchange("binance")
        self.seed_exchange("coinbase", base_url="https://api.coinbase.com")
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                trading.resolve_exchange(db, None)
        err = ctx.exception
        self.assertEqual(err.status, 400)
        self.assertEqual(err.code, "exchange_required")
        self.assertEqual(err.to_payload()["exchanges"], ["binance", "coinbase"])

    def test_exact_provider_shape(self):
        provider_id = self.seed_exchange("binance")
        with self.Session() as db:
            resolved = trading.resolve_exchange(db, "BINANCE")
        self.assertEqual(resolved, {
            "id": provider_id, "key": "binance", "name": "Binance",
            "base_url": "https://api.binance.com",
        })


class BinanceReadTests(TradingServiceBase):
    async def test_quote_is_normalized(self):
        self.seed_exchange()

        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/api/v3/ticker/24hr")
            self.assertEqual(request.url.params["symbol"], "BTCUSDT")
            return httpx.Response(200, json={
                "bidPrice": "100.5",
                "askPrice": "101.5",
                "lastPrice": "101.0",
                "closeTime": 1700000000000,
            })

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_quote(db, symbol="btc/usdt", client_key_id=7)

        self.assertEqual(data, {
            "exchange": "binance",
            "symbol": "BTC/USDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "bid": 100.5,
            "ask": 101.5,
            "last": 101.0,
            "ts": "2023-11-14T22:13:20Z",
            "source": "binance",
        })
        self.audit.assert_called_once()
        kwargs = self.audit.call_args.kwargs
        self.assertEqual(kwargs["event"], "read")
        self.assertEqual(kwargs["action"], "trading.quote")
        self.assertEqual(kwargs["status"], "ok")
        self.assertEqual(kwargs["client_key_id"], 7)
        self.assertEqual(kwargs["provider_key"], "binance")
        self.assertEqual(kwargs["payload"], {"count": 1})

    async def test_futures_quote_uses_fapi_ticker(self):
        self.seed_exchange(base_url="https://fapi.binance.com")

        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/fapi/v1/ticker/24hr")
            self.assertEqual(request.url.params["symbol"], "BTCUSDT")
            return httpx.Response(200, json={
                "bidPrice": "200.5",
                "askPrice": "201.5",
                "lastPrice": "201.0",
                "closeTime": 1700000000000,
            })

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_quote(db, symbol="BTC/USDT")

        self.assertEqual(data["exchange"], "binance")
        self.assertEqual(data["symbol"], "BTC/USDT")
        self.assertEqual((data["bid"], data["ask"], data["last"]),
                         (200.5, 201.5, 201.0))
        self.assertEqual(data["ts"], "2023-11-14T22:13:20Z")

    async def test_balances_are_signed_and_zero_totals_filtered(self):
        self.seed_exchange(api_key='{"api_key": "key", "api_secret": "secret"}')

        def handler(request):
            self.assertEqual(request.url.path, "/api/v3/account")
            self.assertEqual(request.headers["X-MBX-APIKEY"], "key")
            raw_query = request.url.query.decode()
            query, _, signature = raw_query.rpartition("&signature=")
            self.assertIn("timestamp=", query)
            self.assertIn("recvWindow=5000", query)
            expected = hmac.new(
                b"secret", query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            self.assertEqual(signature, expected)
            return httpx.Response(200, json={"balances": [
                {"asset": "BTC", "free": "1.5", "locked": "0.5"},
                {"asset": "ETH", "free": "0", "locked": "0"},
                {"asset": "USDT", "free": "10", "locked": "0"},
            ]})

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_balances(db, client_key_id=3)

        self.assertEqual(data["exchange"], "binance")
        self.assertTrue(data["as_of"].endswith("Z"))
        self.assertEqual(data["balances"], [
            {"asset": "BTC", "free": 1.5, "locked": 0.5, "total": 2.0},
            {"asset": "USDT", "free": 10.0, "locked": 0.0, "total": 10.0},
        ])
        kwargs = self.audit.call_args.kwargs
        self.assertEqual(kwargs["event"], "read")
        self.assertEqual(kwargs["action"], "trading.balances")
        self.assertEqual(kwargs["payload"], {"count": 2})

    async def test_futures_positions_are_normalized(self):
        self.seed_exchange(base_url="https://fapi.binance.com")

        def handler(request):
            self.assertEqual(request.url.path, "/fapi/v2/positionRisk")
            self.assertEqual(request.headers["X-MBX-APIKEY"], "key")
            return httpx.Response(200, json=[
                {"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100",
                 "markPrice": "101", "unRealizedProfit": "0.5", "leverage": "10"},
                {"symbol": "ETHUSDT", "positionAmt": "-1.2", "entryPrice": "20",
                 "markPrice": "19", "unRealizedProfit": "1.2", "leverage": "5"},
                {"symbol": "SOLUSDT", "positionAmt": "0", "entryPrice": "0",
                 "markPrice": "0", "unRealizedProfit": "0", "leverage": "3"},
            ])

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_positions(db)

        self.assertEqual(data["exchange"], "binance")
        self.assertEqual(data["positions"], [
            {"symbol": "BTC/USDT", "side": "long", "quantity": 0.5,
             "entry_price": 100.0, "mark_price": 101.0,
             "unrealized_pnl": 0.5, "leverage": 10},
            {"symbol": "ETH/USDT", "side": "short", "quantity": 1.2,
             "entry_price": 20.0, "mark_price": 19.0,
             "unrealized_pnl": 1.2, "leverage": 5},
        ])
        self.assertEqual(self.audit.call_args.kwargs["payload"], {"count": 2})

    async def test_spot_positions_are_unsupported_without_http(self):
        self.seed_exchange()

        def handler(request):
            raise AssertionError("spot positions must not reach the exchange")

        self.use_transport(handler)
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                await trading.read_positions(db)
        self.assertEqual(ctx.exception.status, 501)
        self.assertEqual(ctx.exception.code, "positions_unsupported")
        self.assertEqual(self.calls, [])
        self.assertEqual(
            self.audit.call_args.kwargs["code"], "positions_unsupported"
        )

    async def test_missing_credentials_is_a_503(self):
        self.seed_exchange(api_key=None)

        def handler(request):
            raise AssertionError("unsigned balances request must not reach the exchange")

        self.use_transport(handler)
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                await trading.read_balances(db)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "exchange_credentials_missing")
        self.assertEqual(self.calls, [])


class CoinbaseReadTests(TradingServiceBase):
    async def test_quote_is_normalized(self):
        self.seed_exchange("coinbase", base_url="https://api.coinbase.com")

        def handler(request):
            self.assertEqual(request.url.path, "/products/BTC-USDT/ticker")
            return httpx.Response(200, json={
                "bid": "10.0", "ask": "11.0", "price": "10.5",
                "time": "2024-01-02T03:04:05Z",
            })

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_quote(db, symbol="BTC-USDT")

        self.assertEqual(data, {
            "exchange": "coinbase",
            "symbol": "BTC/USDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "bid": 10.0,
            "ask": 11.0,
            "last": 10.5,
            "ts": "2024-01-02T03:04:05Z",
            "source": "coinbase",
        })

    async def test_positions_are_unsupported_without_http(self):
        self.seed_exchange("coinbase", base_url="https://api.coinbase.com")

        def handler(request):
            raise AssertionError("coinbase positions must not reach the exchange")

        self.use_transport(handler)
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                await trading.read_positions(db)
        self.assertEqual(ctx.exception.status, 501)
        self.assertEqual(ctx.exception.code, "positions_unsupported")
        self.assertEqual(self.calls, [])

    async def test_balances_support_both_account_shapes(self):
        self.seed_exchange(
            "coinbase", base_url="https://api.coinbase.com",
            api_key='{"api_key": "key", "api_secret": "secret", "passphrase": "pass"}',
        )

        def handler(request):
            self.assertEqual(request.url.path, "/accounts")
            self.assertEqual(request.url.params["limit"], "250")
            self.assertEqual(request.headers["CB-ACCESS-KEY"], "key")
            self.assertEqual(request.headers["CB-ACCESS-PASSPHRASE"], "pass")
            timestamp = request.headers["CB-ACCESS-TIMESTAMP"]
            expected = base64.b64encode(hmac.new(
                b"secret",
                f"{timestamp}GET/accounts?limit=250".encode("utf-8"),
                hashlib.sha256,
            ).digest()).decode("ascii")
            self.assertEqual(request.headers["CB-ACCESS-SIGN"], expected)
            return httpx.Response(200, json={"accounts": [
                {"currency": "BTC", "available_balance": {"value": "1.0"},
                 "hold": {"value": "0.25"}},
                {"currency": "ETH", "balance": "2.0", "hold": "0.5",
                 "available": "1.5"},
                {"currency": "DOGE", "available_balance": {"value": "0"},
                 "hold": {"value": "0"}},
            ]})

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_balances(db)

        self.assertEqual(data["exchange"], "coinbase")
        self.assertEqual(data["balances"], [
            {"asset": "BTC", "free": 1.0, "locked": 0.25, "total": 1.25},
            {"asset": "ETH", "free": 1.5, "locked": 0.5, "total": 2.0},
        ])


class ReadGuardsTests(TradingServiceBase):
    async def test_kill_switch_blocks_before_any_http(self):
        self.seed_exchange()
        with self.Session() as db:
            trading_settings.save(db, kill_switch=True)

        def handler(request):
            raise AssertionError("kill switch must stop the call before HTTP")

        self.use_transport(handler)
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                await trading.read_quote(db, symbol="BTC/USDT")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "trading_disabled")
        self.assertEqual(self.calls, [])
        kwargs = self.audit.call_args.kwargs
        self.assertEqual(kwargs["event"], "refusal")
        self.assertEqual(kwargs["code"], "trading_disabled")

    async def test_unsupported_exchange_is_501(self):
        self.seed_exchange("kraken", base_url="https://api.kraken.com")

        def handler(request):
            raise AssertionError("unsupported exchange must not reach HTTP")

        self.use_transport(handler)
        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                await trading.read_quote(db, symbol="BTC/USDT", exchange="kraken")
        self.assertEqual(ctx.exception.status, 501)
        self.assertEqual(ctx.exception.code, "unsupported_exchange")
        self.assertIn("binance, coinbase", ctx.exception.message)
        self.assertEqual(self.calls, [])

    async def test_explicit_exchange_makes_exactly_one_call(self):
        self.seed_exchange("binance")
        self.seed_exchange("coinbase", base_url="https://api.coinbase.com")

        with self.Session() as db:
            with self.assertRaises(trading.TradingError) as ctx:
                await trading.read_quote(db, symbol="BTC/USDT")
        self.assertEqual(ctx.exception.code, "exchange_required")
        self.assertEqual(self.calls, [])

        def handler(request):
            return httpx.Response(200, json={
                "bidPrice": "1", "askPrice": "2", "lastPrice": "1.5",
                "closeTime": 1700000000000,
            })

        self.use_transport(handler)
        with self.Session() as db:
            data = await trading.read_quote(
                db, symbol="BTC/USDT", exchange="binance"
            )
        self.assertEqual(data["exchange"], "binance")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].url.host, "api.binance.com")


if __name__ == "__main__":
    unittest.main()
