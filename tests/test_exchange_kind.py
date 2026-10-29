"""Exchange provider kind: registered, creatable, and never chat-routable.

Phase 0 of the trading roadmap adds a read-only `exchange` provider kind. An
exchange has credentials and a base URL but no model catalogue and no chat
endpoint, so every model/chat path must refuse it *before* any HTTP call:
provider creation, discovery, the sync engine, model pings, the auto-fallback
pool and GET /v1/models.

Run: python -m unittest discover -s tests -p "test_exchange_kind.py" -v
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import discovery, registry, syncengine
from nova.models import Base, Model, Provider
from routers import admin_misc, admin_models, admin_providers, gateway
from ui.tabs import providers as providers_ui


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _request(path="/v1/models"):
    return Request({
        "type": "http", "method": "GET", "path": path, "scheme": "http",
        "server": ("testserver", 80), "root_path": "", "query_string": b"",
        "headers": [],
    })


class RegistryKindTests(unittest.TestCase):
    def test_exchange_is_a_known_kind(self):
        self.assertIn("exchange", registry.KNOWN_KINDS)

    def test_shipped_catalogue_still_validates(self):
        self.assertEqual(registry.validate(), [])


class ProviderKindTests(unittest.TestCase):
    def test_exchange_is_a_creatable_kind(self):
        self.assertIn("exchange", admin_providers.PROVIDER_KINDS)

    def test_custom_exchange_preset(self):
        preset = next(p for p in admin_providers.PRESETS if p["key"] == "custom-exchange")
        self.assertEqual(preset["name"], "Exchange (read-only)")
        self.assertEqual(preset["kind"], "exchange")
        self.assertEqual(preset["base_url"], "")
        self.assertEqual(preset["prefix"], "exchange/")
        self.assertFalse(preset["requires_signin"])
        self.assertEqual(preset["priority"], 95)

    def test_exchange_base_url_must_be_a_root(self):
        self.assertIsNone(admin_providers.validate_base_url("https://api.binance.com", "exchange"))
        for url in ("https://api.binance.com/api/v3/quote",
                    "https://api.binance.com/api/v3/positions",
                    "https://api.binance.com/api/v3/balances",
                    "https://api.binance.com/api/v3/ticker",
                    "https://api.binance.com/api/v3/models",
                    "https://user:secret@api.binance.com",
                    "https://api.binance.com?key=secret",
                    "https://api.binance.com#frag",
                    "ftp://api.binance.com"):
            self.assertIsNotNone(admin_providers.validate_base_url(url, "exchange"), url)

    def test_openai_validation_unchanged(self):
        self.assertIsNone(admin_providers.validate_base_url("https://api.openai.com/v1", "openai"))
        self.assertIsNotNone(admin_providers.validate_base_url(
            "https://api.openai.com/v1/chat/completions", "openai"))


class _FakeJsonRequest:
    """Only the surface admin_providers.create_provider touches."""

    def __init__(self, body):
        self._body = body
        self.query_params = {}

    async def json(self):
        return self._body


class AdminProviderExchangeTests(unittest.IsolatedAsyncioTestCase):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            raise AssertionError("an exchange provider must never GET /models")

        async def post(self, *args, **kwargs):
            raise AssertionError("an exchange provider must never POST /models")

    async def test_probe_upstream_never_fetches_models(self):
        with patch.object(admin_providers.httpx, "AsyncClient", self.ExplodingClient):
            probe = await admin_providers.probe_upstream(
                "binance", "exchange", "https://api.binance.com", "secret")
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["models_found"], 0)
        self.assertIn("verify read access", probe["detail"])
        self.assertNotIn("reachable", probe["detail"].lower())

    async def test_create_exchange_short_circuits_model_sync(self):
        engine, Session = _db()
        try:
            with patch.object(admin_providers, "SessionLocal", Session), \
                 patch.object(admin_providers, "sync_provider",
                              side_effect=AssertionError("exchange providers must not sync models")):
                response = await admin_providers.create_provider(_FakeJsonRequest({
                    "name": "Exchange (read-only)",
                    "kind": "exchange",
                    "base_url": "https://api.binance.com",
                }))
            payload = json.loads(response.body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["models_added"], 0)
            self.assertIn("sync_skipped", payload)
        finally:
            engine.dispose()


class UiCreatePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_mode_passes_the_exchange_skip_note_to_the_dialog(self):
        async def form_dict(_request):
            return {
                "name": "Exchange (read-only)",
                "kind": "exchange",
                "base_url": "https://api.binance.com",
            }

        async def post(path, **kwargs):
            return {
                "id": 7,
                "keys_added": 1,
                "models_added": 0,
                "sync_skipped": "exchange providers do not expose a model catalogue",
            }

        with patch.object(providers_ui, "form_dict", form_dict), \
             patch.object(providers_ui.api, "post", post):
            response = await providers_ui.create_provider(SimpleNamespace(query_params={"logs": "1"}))

        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models_added"], 0)
        self.assertIn("model catalogue", payload["sync_skipped"])
        self.assertIsNone(payload["stream"])


class UiEndpointValidationTests(unittest.TestCase):
    def test_exchange_roots_are_validated(self):
        self.assertIsNone(providers_ui._endpoint_error("exchange", "https://api.binance.com"))
        for url in ("https://api.binance.com/api/v3/quote",
                    "https://api.binance.com/api/v3/positions",
                    "https://api.binance.com/api/v3/balances",
                    "https://api.binance.com/api/v3/ticker",
                    "https://api.binance.com/api/v3/models",
                    "https://user:secret@api.binance.com",
                    "https://api.binance.com?key=secret",
                    "https://api.binance.com#frag"):
            self.assertIsNotNone(providers_ui._endpoint_error("exchange", url), url)

    def test_openai_and_anthropic_behaviour_is_unchanged(self):
        self.assertIsNone(providers_ui._endpoint_error("openai", "https://api.openai.com/v1"))
        self.assertIsNone(providers_ui._endpoint_error("anthropic", "https://api.anthropic.com"))


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _db()

    def tearDown(self):
        self.engine.dispose()

    def test_meta_lists_exchange_kind_and_preset(self):
        with self.Session() as db:
            meta = admin_misc.meta(_request("/api/admin/meta"), db)
        self.assertIn("exchange", meta["kinds"])
        self.assertTrue(any(p["key"] == "custom-exchange" for p in meta["presets"]))


class AdminModelsPingTests(unittest.IsolatedAsyncioTestCase):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("an exchange provider must never POST /chat/completions")

    async def test_exchange_model_ping_is_a_typed_error(self):
        engine, Session = _db()
        try:
            with Session() as db:
                provider = Provider(key="binance", name="Binance", kind="exchange",
                                    baseUrl="https://api.binance.com")
                db.add(provider)
                db.flush()
                model = Model(providerId=provider.id, modelId="BTCUSDT",
                              exposedId="exchange/BTCUSDT")
                db.add(model)
                db.commit()
                model_id = model.id

            with patch.object(admin_models, "SessionLocal", Session), \
                 patch.object(admin_models.httpx, "AsyncClient", self.ExplodingClient):
                response = await admin_models.ping_model(str(model_id))

            self.assertEqual(response.status_code, 400)
            self.assertIn("do not serve chat models", json.loads(response.body)["error"])
        finally:
            engine.dispose()


class _NoHTTPClient:
    async def post(self, *args, **kwargs):
        raise AssertionError("an exchange provider must never reach the upstream HTTP path")


class GatewayExchangeGuardTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _exchange_row():
        provider = SimpleNamespace(id=1, key="binance", name="Binance", kind="exchange",
                                   baseUrl="https://api.binance.com", enabled=True)
        return SimpleNamespace(provider=provider, modelId="BTCUSDT",
                               exposedId="exchange/BTCUSDT")

    async def test_attempt_upstream_returns_none_before_http(self):
        with patch.object(gateway, "NONSTREAM_CLIENT", _NoHTTPClient()):
            result = await gateway.attempt_upstream(
                self._exchange_row(), [{"role": "user", "content": "hi"}], None)
        self.assertIsNone(result)

    async def test_stream_attempt_fails_before_http(self):
        with patch.object(gateway, "UPSTREAM_CLIENT", _NoHTTPClient()):
            with self.assertRaises(gateway.UpstreamFailure):
                await gateway.attempt_upstream_stream(
                    self._exchange_row(), [{"role": "user", "content": "hi"}], None)

    def test_auto_standins_excludes_exchange_models(self):
        engine, Session = _db()
        try:
            with Session() as db:
                exchange = Provider(key="binance", name="Binance", kind="exchange",
                                    baseUrl="https://api.binance.com")
                openai = Provider(key="openai", name="OpenAI", kind="openai",
                                  baseUrl="https://api.openai.com/v1")
                db.add_all([exchange, openai])
                db.flush()
                db.add_all([
                    Model(providerId=exchange.id, modelId="BTCUSDT",
                          exposedId="exchange/BTCUSDT", status="healthy"),
                    Model(providerId=openai.id, modelId="gpt-4o",
                          exposedId="openai/gpt-4o", status="healthy"),
                ])
                db.commit()

                picked = gateway.auto_standins(db, "auto", set())
                ids = {m.exposedId for m in picked}
            self.assertNotIn("exchange/BTCUSDT", ids)
            self.assertIn("openai/gpt-4o", ids)
        finally:
            engine.dispose()

    async def test_models_listing_hides_a_stray_exchange_row(self):
        engine, Session = _db()
        try:
            with Session() as db:
                exchange = Provider(key="binance", name="Binance", kind="exchange",
                                    baseUrl="https://api.binance.com")
                openai = Provider(key="openai", name="OpenAI", kind="openai",
                                  baseUrl="https://api.openai.com/v1")
                db.add_all([exchange, openai])
                db.flush()
                db.add_all([
                    Model(providerId=exchange.id, modelId="BTCUSDT",
                          exposedId="exchange/BTCUSDT", status="healthy"),
                    Model(providerId=openai.id, modelId="gpt-4o",
                          exposedId="openai/gpt-4o", status="healthy"),
                ])
                db.commit()

            with patch("nova.database.SessionLocal", Session):
                response = await gateway.list_models(_request())
            ids = [entry["id"] for entry in json.loads(response.body)["data"]]
            self.assertNotIn("exchange/BTCUSDT", ids)
            self.assertIn("openai/gpt-4o", ids)
        finally:
            engine.dispose()


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_provider_is_skipped_without_an_http_call(self):
        engine, Session = _db()
        try:
            with Session() as db:
                provider = Provider(key="binance-discovery", name="Binance", kind="exchange",
                                    baseUrl="https://api.binance.com")
                db.add(provider)
                db.commit()
                provider = db.get(Provider, provider.id)

                calls = []

                async def no_fetch(*args, **kwargs):
                    calls.append(args)
                    raise AssertionError("an exchange provider must never GET /models")

                with patch.object(discovery, "_fetch_json", no_fetch):
                    one = await discovery._discover_one(db, provider, "secret")
                    results = await discovery.discover_provider_models(db, provider.key)

            self.assertFalse(one["ok"])
            self.assertIn("do not expose a model catalogue", one["error"])
            self.assertFalse(results[0]["ok"])
            self.assertIn("do not expose a model catalogue", results[0]["error"])
            self.assertEqual(calls, [])
        finally:
            engine.dispose()

    async def test_syncengine_skips_exchange_without_model_rows(self):
        engine, Session = _db()
        try:
            with Session() as db:
                report = await syncengine.sync_provider(db, {
                    "id": 1, "key": "binance", "name": "Binance", "kind": "exchange",
                    "baseUrl": "https://api.binance.com", "prefix": "exchange/",
                })
                model_count = db.query(Model).count()
            self.assertTrue(report["ok"])
            self.assertTrue(report["skipped"])
            self.assertEqual(report["created"], 0)
            self.assertEqual(report["updated"], 0)
            self.assertEqual(model_count, 0)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()