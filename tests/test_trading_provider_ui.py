"""Providers tab trading kill-switch — ctx tolerance, BFF endpoint, pill render.

Offline: the UI api singleton is replaced by in-process fakes and the template
render needs no database.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from ui.render import render
from ui.tabs import providers

TRADING = {"trading": {"kill_switch": True, "exchange_providers": 2}}


def _request(path: str = "/partials/tab/providers", query: bytes = b"") -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    })


class ProvidersTradingContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctx_exposes_the_trading_state(self):
        async def fake_get(path, **_kw):
            return {
                "/api/admin/providers": [],
                "/api/admin/stats": {},
                "/api/admin/meta": {},
                "/api/admin/trading": dict(TRADING),
            }[path]

        with patch.object(providers.api, "get", new=fake_get):
            ctx = await providers._providers_ctx(_request())

        self.assertTrue(ctx["trading"]["kill_switch"])
        self.assertEqual(ctx["trading"]["exchange_providers"], 2)
        self.assertIsNone(ctx["error"])

    async def test_trading_read_failure_does_not_break_the_list(self):
        async def fake_get(path, **_kw):
            if path == "/api/admin/trading":
                raise RuntimeError("trading API down")
            if path == "/api/admin/providers":
                return [{"id": 1, "name": "Groq", "key": "groq", "enabled": True}]
            return {}

        with patch.object(providers.api, "get", new=fake_get):
            ctx = await providers._providers_ctx(_request())

        self.assertIsNone(ctx["trading"])
        self.assertIsNone(ctx["error"])
        self.assertEqual(len(ctx["rows"]), 1)

    async def test_odd_trading_payload_is_ignored(self):
        async def fake_get(path, **_kw):
            if path == "/api/admin/trading":
                return []  # a stub that does not implement the endpoint
            return [] if path == "/api/admin/providers" else {}

        with patch.object(providers.api, "get", new=fake_get):
            ctx = await providers._providers_ctx(_request())

        self.assertIsNone(ctx["trading"])


class TradingKillSwitchEndpointTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(providers.router)
        self.client = TestClient(app)

    def test_engage_posts_true_and_toasts(self):
        post = AsyncMock(return_value={})
        with patch.object(providers.api, "post", new=post):
            res = self.client.post(
                "/ui/providers/trading-kill-switch", data={"kill_switch": "true"})
        self.assertEqual(res.status_code, 200)
        post.assert_awaited_once_with("/api/admin/trading", json={"kill_switch": True})
        self.assertIn("ENGAGED", res.headers.get("HX-Trigger", ""))

    def test_release_posts_false_and_toasts(self):
        post = AsyncMock(return_value={})
        with patch.object(providers.api, "post", new=post):
            res = self.client.post(
                "/ui/providers/trading-kill-switch", data={"kill_switch": "false"})
        self.assertEqual(res.status_code, 200)
        post.assert_awaited_once_with("/api/admin/trading", json={"kill_switch": False})
        self.assertIn("released", res.headers.get("HX-Trigger", ""))

    def test_api_error_is_surfaced_as_an_error_toast(self):
        post = AsyncMock(side_effect=providers.ApiError("trading_settings is locked"))
        with patch.object(providers.api, "post", new=post):
            res = self.client.post(
                "/ui/providers/trading-kill-switch", data={"kill_switch": "true"})
        self.assertEqual(res.status_code, 200)
        trigger = res.headers.get("HX-Trigger", "")
        self.assertIn("trading_settings is locked", trigger)
        self.assertIn('"error"', trigger)


class TradingPillRenderTests(unittest.TestCase):
    def _render(self, trading):
        return render(
            "partials/providers_list.html",
            rows=[], visible=[], visible_total=0, test_ids=[], keys=[],
            q="", expand=None, _root_url="/partials/providers/list",
            error=None, stats=None, meta=None, trading=trading, now_ms=0,
        )

    def test_halted_state_renders_pill_and_release_form(self):
        body = self._render({"kill_switch": True, "exchange_providers": 1})
        self.assertIn("Trading halted", body)
        self.assertIn("All /v1/trading/* and agent trading tools refuse with trading_disabled", body)
        self.assertIn('hx-post="/ui/providers/trading-kill-switch"', body)
        self.assertIn('name="kill_switch" value="false"', body)
        self.assertIn("Release kill-switch", body)
        self.assertNotIn("Trading armed", body)
        self.assertNotIn("hx-confirm", body)

    def test_armed_state_renders_engage_form_and_singular_exchange(self):
        body = self._render({"kill_switch": False, "exchange_providers": 1})
        self.assertIn("Trading armed", body)
        self.assertIn("(1 exchange)", body)
        self.assertIn('name="kill_switch" value="true"', body)
        self.assertIn("Engage kill-switch", body)
        self.assertNotIn("Trading halted", body)

    def test_armed_state_pluralises_exchanges(self):
        body = self._render({"kill_switch": False, "exchange_providers": 2})
        self.assertIn("(2 exchanges)", body)

    def test_missing_trading_state_renders_no_control(self):
        body = self._render(None)
        self.assertNotIn("Trading halted", body)
        self.assertNotIn("Trading armed", body)
        self.assertNotIn("/ui/providers/trading-kill-switch", body)


if __name__ == "__main__":
    unittest.main()
