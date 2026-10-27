"""Offline protocol regression tests; no live upstream keys or network required."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from nova import bootstrap, discovery
from nova.provider_transport import (
    anthropic_to_openai, native_anthropic_body, openai_to_anthropic,
)
from routers import gateway
from routers.admin_providers import PRESETS, validate_base_url
from routers.anthropic_adapter import anthropic_messages_to_openai


def row(kind="anthropic", base="https://api.anthropic.com"):
    provider = SimpleNamespace(kind=kind, baseUrl=base, id=7, name="Example", enabled=True)
    return SimpleNamespace(provider=provider, modelId="claude-example", exposedId="example/claude-example")


class ProviderProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_presets_and_validation(self):
        presets = {preset["key"]: preset for preset in PRESETS}
        self.assertEqual(presets["xai"]["base_url"], "https://api.x.ai/v1")
        self.assertEqual(presets["xai"]["kind"], "openai")
        self.assertEqual(presets["cloudflare-worker"]["kind"], "openai")
        self.assertEqual(presets["custom-gateway"]["kind"], "openai")
        self.assertIsNone(validate_base_url("https://example.workers.dev/v1", "openai"))
        self.assertIsNone(validate_base_url("http://localhost:11434/v1", "openai"))
        for url in ("", "ftp://host/v1", "https://user:pass@example.org/v1", "https://example.org/v1?token=secret",
                    "https://example.org/v1/chat/completions"):
            self.assertIsNotNone(validate_base_url(url, "openai"), url)

    def test_anthropic_tool_roundtrip(self):
        messages = [{"role": "system", "content": "Act safely"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "lookup", "arguments": '{"q":"hello"}'}}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "found"}]
        body = openai_to_anthropic("claude-example", messages, {"max_tokens": 123,
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}}}, stream=True)
        self.assertEqual(body["system"], "Act safely")
        self.assertEqual(body["messages"][0]["content"][0]["input"], {"q": "hello"})
        self.assertEqual(body["messages"][1]["role"], "user")
        self.assertEqual(body["messages"][1]["content"][0]["type"], "tool_result")
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "lookup"})
        self.assertTrue(body["stream"])
        normalized = anthropic_to_openai({"content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "hello"}}],
                                           "stop_reason": "tool_use", "usage": {"input_tokens": 8, "output_tokens": 3}})
        self.assertEqual(normalized["finish_reason"], "tool_calls")
        self.assertEqual(normalized["tool_calls"][0]["function"]["arguments"], '{"q": "hello"}')

    def test_native_client_history(self):
        payload = {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                 {"type": "thinking", "thinking": "private"}]}], "max_tokens": 200}
        native = native_anthropic_body(payload, "claude-example")
        self.assertEqual(native["messages"][0]["content"], payload["messages"][0]["content"][:1])

    def test_parallel_tool_results_share_one_user_turn(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "one"},
            {"role": "tool", "tool_call_id": "call_2", "content": "two"},
        ]
        body = openai_to_anthropic("claude-example", messages, {})
        self.assertEqual([m["role"] for m in body["messages"]], ["assistant", "user"])
        results = body["messages"][1]["content"]
        self.assertEqual([b["tool_use_id"] for b in results], ["call_1", "call_2"])

    def test_anthropic_tool_result_pairs_with_tool_use(self):
        messages, system = anthropic_messages_to_openai({
            "system": "Be brief.",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
                ]},
                {"role": "user", "content": [
                    {"type": "text", "text": "Here is the result."},
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                ]},
            ],
        })
        self.assertEqual(system, "Be brief.")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "toolu_1")
        self.assertEqual(messages[3]["role"], "user")
        self.assertEqual(messages[3]["content"], "Here is the result.")

    async def test_non_json_2xx_is_a_failed_stage_not_an_exception(self):
        async def handler(req):
            return httpx.Response(200, text="<html>degraded upstream</html>",
                                  headers={"content-type": "text/html"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(gateway, "NONSTREAM_CLIENT", client), patch("nova.database.SessionLocal") as session:
                session.return_value.__enter__.return_value.scalars.return_value.first.return_value = SimpleNamespace(apiKey="test-token", cooldownUntil=None)
                result = await gateway.attempt_upstream(
                    row("openai", "https://example.test/v1"),
                    [{"role": "user", "content": "hello"}], None)
            self.assertIsNone(result)
        finally:
            await client.aclose()

    def test_schema_sync_ddl_is_additive(self):
        from sqlalchemy import Boolean, Column

        sql = bootstrap._add_column_sql("ModelRoute", Column("auto", Boolean, default=True))
        self.assertIn("ALTER TABLE", sql)
        self.assertIn("ADD COLUMN", sql)
        self.assertIn("DEFAULT TRUE", sql)
        self.assertNotIn("NOT NULL", sql)

    async def test_native_nonstream_transport(self):
        async def handler(req):
            self.assertEqual(str(req.url), "https://api.anthropic.com/v1/messages")
            self.assertEqual(req.headers["x-api-key"], "test-token")
            self.assertNotIn("authorization", req.headers)
            self.assertEqual(req.headers["anthropic-version"], "2023-06-01")
            self.assertEqual(json.loads(req.content)["messages"][0]["content"][0]["text"], "hello")
            return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                                             "usage": {"input_tokens": 2, "output_tokens": 1}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(gateway, "NONSTREAM_CLIENT", client), patch("nova.database.SessionLocal") as session:
                session.return_value.__enter__.return_value.scalars.return_value.first.return_value = SimpleNamespace(apiKey="test-token", cooldownUntil=None)
                result = await gateway.attempt_upstream(row(), [{"role": "user", "content": "hello"}], None)
            self.assertEqual(result["content"], "hi")
            self.assertEqual(result["usage"]["prompt_tokens"], 2)
        finally:
            await client.aclose()

    async def test_worker_openai_transport_and_anthropic_stream(self):
        async def handler(req):
            if "workers.dev" in str(req.url):
                self.assertEqual(str(req.url), "https://example.workers.dev/v1/chat/completions")
                self.assertEqual(req.headers["authorization"], "Bearer test-token")
                return httpx.Response(200, json={"choices": [{"message": {"content": "worker works"}}]})
            self.assertEqual(str(req.url), "https://api.anthropic.com/v1/messages")
            self.assertTrue(json.loads(req.content)["stream"])
            return httpx.Response(200, text=(
                'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(gateway, "UPSTREAM_CLIENT", client), patch.object(gateway, "NONSTREAM_CLIENT", client), patch.object(gateway, "select_upstream_key_sync", return_value={"apiKey": "test-token"}), patch("nova.database.SessionLocal") as session:
                session.return_value.__enter__.return_value.scalars.return_value.first.return_value = SimpleNamespace(apiKey="test-token", cooldownUntil=None)
                worker = await gateway.attempt_upstream(row("openai", "https://example.workers.dev/v1"), [{"role": "user", "content": "hello"}], None)
                self.assertEqual(worker["content"], "worker works")
                iterator, _, _ = await gateway.attempt_upstream_stream(row(), [{"role": "user", "content": "hello"}], None)
                pieces = [piece async for piece in iterator]
            self.assertEqual(pieces[0]["text"], "hello")
            self.assertEqual(pieces[-1]["finish_reason"], "stop")
        finally:
            await client.aclose()

    async def test_native_stream_tool_events(self):
        def handler(req):
            return httpx.Response(200, text=(
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"lookup"}}\n\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":\\"hi\\"}"}}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(gateway, "UPSTREAM_CLIENT", client), patch.object(gateway, "select_upstream_key_sync", return_value={"apiKey": "test-token"}):
                iterator, _, _ = await gateway.attempt_upstream_stream(row(), [{"role": "user", "content": "lookup"}], None)
                events = [event async for event in iterator]
            self.assertEqual(events[0]["tool_calls"][0]["function"]["name"], "lookup")
            self.assertEqual(events[1]["tool_calls"][0]["function"]["arguments"], '{"q":"hi"}')
            self.assertEqual(events[-1]["finish_reason"], "tool_calls")
        finally:
            await client.aclose()

    async def test_discovery_pricing_and_native_endpoint(self):
        self.assertFalse(discovery._from_openai_compatible({"id": "grok-1"}, "xai", 1, "xAI")["is_free"])
        self.assertTrue(discovery._from_openai_compatible({"id": "free-1", "pricing": {"prompt": "0", "completion": "0"}}, "custom", 1, "Custom")["is_free"])
        async def fake_fetch(url, headers=None, log=None, log_url=None):
            self.assertEqual(url, "https://api.anthropic.com/v1/models?limit=100")
            self.assertEqual(headers["x-api-key"], "test-token")
            return {"data": [{"id": "claude-example", "display_name": "Claude Example"}]}
        with patch.object(discovery, "_fetch_json", fake_fetch):
            result = await discovery._discover_anthropic("https://api.anthropic.com/v1", "test-token", 1, "Anthropic")
        self.assertFalse(result[0]["is_free"])

    async def test_gemini_key_not_exposed_on_catalogue_failure(self):
        async def handler(req):
            self.assertIn("key=secret-value", str(req.url))
            return httpx.Response(403, json={"error": "forbidden"})

        real_client = httpx.AsyncClient
        def mock_client(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(discovery.httpx, "AsyncClient", mock_client):
            with self.assertRaisesRegex(ValueError, "upstream returned HTTP 403") as exc:
                await discovery._discover_gemini("https://example.test/v1beta", "secret-value", 1, "Gemini")
        self.assertNotIn("secret-value", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
