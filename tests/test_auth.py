"""Client-key auth + quota enforcement on the gateway (nova/auth.py).

Offline: a throwaway in-memory SQLite database, FastAPI's TestClient, and no
upstream calls at all (every case is decided before a provider is touched).
"""
import sys
import unittest
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import auth as nova_auth
from nova.database import get_db
from nova.models import Base, ClientKey, RequestLog, SystemConfig


class AuthApi:
    """A tiny app that mirrors how the real routes use the dependency."""

    def __init__(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = FastAPI()

        @self.app.exception_handler(nova_auth.AuthError)
        async def _auth_error(request: Request, exc: nova_auth.AuthError):
            from fastapi.responses import JSONResponse

            return JSONResponse(exc.to_payload(), status_code=exc.status, headers=exc.headers())

        @self.app.get("/v1/ping")
        def ping(principal: object = Depends(nova_auth.gateway_auth)):
            return {"key": principal.name if principal else None,
                    "anonymous": getattr(principal, "anonymous", False)}

        @self.app.post("/v1/echo")
        async def echo(request: Request, principal: object = Depends(nova_auth.gateway_auth)):
            body = await request.json()
            model = str(body.get("model") or "")
            try:
                nova_auth.enforce(getattr(principal, "key", None), model)
            except nova_auth.AuthError as err:
                from fastapi.responses import JSONResponse

                return JSONResponse(err.to_payload(), status_code=err.status, headers=err.headers())
            key = getattr(principal, "key", None)
            with self.Session() as db:
                nova_auth.update_usage(db, key.id if key else None, 10, 5)
            return {"ok": True, "model": model}

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = _db
        self.client = TestClient(self.app)

    def add_key(self, token="nova-sk-test", **overrides):
        with self.Session() as db:
            key = ClientKey(name=overrides.pop("name", "tester"), token=token, **overrides)
            db.add(key)
            db.commit()
            return key.id

    def get_key(self, key_id):
        with self.Session() as db:
            return db.get(ClientKey, key_id)


class AnonymousTests(unittest.TestCase):
    def setUp(self):
        nova_auth.reset_state()
        self.api = AuthApi()

    def test_no_client_keys_means_anonymous_is_allowed(self):
        response = self.api.client.get("/v1/ping")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["anonymous"])

    def test_unknown_token_is_a_typed_401(self):
        response = self.api.client.get("/v1/ping", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_INVALID_KEY)

    def test_disabled_token_is_refused(self):
        self.api.add_key("nova-sk-off", enabled=False)
        response = self.api.client.get("/v1/ping", headers={"x-api-key": "nova-sk-off"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_INVALID_KEY)

    def test_existing_keys_close_anonymous_access(self):
        self.api.add_key()
        response = self.api.client.get("/v1/ping")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_MISSING_KEY)

    def test_bearer_and_x_api_key_both_resolve(self):
        key_id = self.api.add_key("nova-sk-abc")
        via_bearer = self.api.client.get("/v1/ping", headers={"Authorization": "Bearer nova-sk-abc"})
        via_header = self.api.client.get("/v1/ping", headers={"x-api-key": "nova-sk-abc"})
        self.assertEqual(via_bearer.status_code, 200)
        self.assertEqual(via_header.status_code, 200)
        self.assertEqual(via_bearer.json()["key"], "tester")
        self.assertIsNotNone(key_id)

    def test_require_api_key_env_denies_anonymous(self):
        original = nova_auth.REQUIRE_API_KEY
        nova_auth.REQUIRE_API_KEY = True
        try:
            response = self.api.client.get("/v1/ping")
        finally:
            nova_auth.REQUIRE_API_KEY = original
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], nova_auth.CODE_MISSING_KEY)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        nova_auth.reset_state()
        self.api = AuthApi()

    def test_allowed_models_globs(self):
        self.api.add_key("nova-sk-models", allowedModels="gpt-*, claude-3")
        ok = self.api.client.post("/v1/echo", json={"model": "gpt-4o"},
                                  headers={"Authorization": "Bearer nova-sk-models"})
        forbidden = self.api.client.post("/v1/echo", json={"model": "claude-opus"},
                                         headers={"Authorization": "Bearer nova-sk-models"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], nova_auth.CODE_MODEL_FORBIDDEN)

    def test_star_allows_everything(self):
        self.api.add_key("nova-sk-star", allowedModels="*")
        response = self.api.client.post("/v1/echo", json={"model": "anything"},
                                        headers={"Authorization": "Bearer nova-sk-star"})
        self.assertEqual(response.status_code, 200)

    def test_rpm_limit_returns_429_with_retry_after(self):
        self.api.add_key("nova-sk-rpm", rpmLimit=2)
        headers = {"Authorization": "Bearer nova-sk-rpm"}
        self.assertEqual(self.api.client.get("/v1/ping", headers=headers).status_code, 200)
        self.assertEqual(self.api.client.get("/v1/ping", headers=headers).status_code, 200)
        third = self.api.client.get("/v1/ping", headers=headers)
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.json()["error"]["code"], nova_auth.CODE_RPM_EXCEEDED)
        self.assertIn("retry-after", {k.lower() for k in third.headers})
        self.assertGreaterEqual(int(third.headers["retry-after"]), 1)

    def test_rpm_zero_is_unlimited(self):
        self.api.add_key("nova-sk-free", rpmLimit=0)
        headers = {"Authorization": "Bearer nova-sk-free"}
        for _ in range(5):
            self.assertEqual(self.api.client.get("/v1/ping", headers=headers).status_code, 200)

    def test_daily_token_budget(self):
        key_id = self.api.add_key("nova-sk-tpd", rpmLimit=0, tpdLimit=20)
        headers = {"Authorization": "Bearer nova-sk-tpd"}
        self.assertEqual(self.api.client.post("/v1/echo", json={"model": "m"}, headers=headers).status_code, 200)
        # update_usage booked 10 in + 5 out on that request: budget is 20.
        key = self.api.get_key(key_id)
        self.assertEqual((key.tokensIn, key.tokensOut), (10, 5))
        key.tokensIn, key.tokensOut = 15, 10
        with self.api.Session() as db:
            db.merge(key)
            db.commit()
        blocked = self.api.client.post("/v1/echo", json={"model": "m"}, headers=headers)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], nova_auth.CODE_TPD_EXCEEDED)

    def test_usage_is_attributed_to_the_key_in_one_place(self):
        key_id = self.api.add_key("nova-sk-count", rpmLimit=0)
        headers = {"Authorization": "Bearer nova-sk-count"}
        self.api.client.post("/v1/echo", json={"model": "m"}, headers=headers)
        self.api.client.post("/v1/echo", json={"model": "m"}, headers=headers)
        key = self.api.get_key(key_id)
        self.assertEqual(key.reqCount, 2)
        self.assertEqual(key.tokensIn, 20)
        self.assertEqual(key.tokensOut, 10)
        self.assertIsNotNone(key.lastUsedAt)

    def test_repeated_bad_tokens_are_throttled(self):
        original = nova_auth.AUTH_FAILURES
        nova_auth.AUTH_FAILURES = nova_auth.routing.ClientLockouts(
            max_attempts=2, duration_ms=60_000, window_ms=60_000)
        try:
            self.api.client.get("/v1/ping", headers={"Authorization": "Bearer bad-1"})
            self.api.client.get("/v1/ping", headers={"Authorization": "Bearer bad-2"})
            locked = self.api.client.get("/v1/ping", headers={"Authorization": "Bearer bad-3"})
        finally:
            nova_auth.AUTH_FAILURES = original
        self.assertEqual(locked.status_code, 429)


class AllowedModelsParsingTests(unittest.TestCase):
    def test_comma_and_space_lists(self):
        self.assertEqual(nova_auth.parse_allowed_models("a, b  c,d"), ["a", "b", "c", "d"])
        self.assertEqual(nova_auth.parse_allowed_models(""), [])
        self.assertEqual(nova_auth.parse_allowed_models(None), [])

    def test_glob_semantics(self):
        self.assertTrue(nova_auth.model_allowed([], "x"))
        self.assertTrue(nova_auth.model_allowed(["gpt-*"], "gpt-4o"))
        self.assertFalse(nova_auth.model_allowed(["gpt-*"], "claude"))
        self.assertTrue(nova_auth.model_allowed(["openrouter/*"], "openrouter/llama"))
        self.assertTrue(nova_auth.model_allowed(["auto"], "auto"))


if __name__ == "__main__":
    unittest.main()
