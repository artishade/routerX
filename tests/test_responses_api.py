"""POST /v1/responses - the OpenAI Responses API adapter.

Offline: translation units, then the endpoint over a TestClient with the
non-stream pipeline stubbed, and the SSE event sequence for stream=true.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import auth as nova_auth
from nova.database import get_db
from nova.models import Base
from routers import openai_responses


class TranslationTests(unittest.TestCase):
    def test_string_input_becomes_one_user_message(self):
        messages, system = openai_responses.input_to_messages({"input": "hi", "instructions": "be terse"})
        self.assertEqual(messages[0], {"role": "system", "content": "be terse"})
        self.assertEqual(messages[1], {"role": "user", "content": "hi"})
        self.assertEqual(system, "be terse")

    def test_message_array_with_content_parts(self):
        payload = {"input": [
            {"role": "user", "content": [{"type": "input_text", "text": "one"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "two"}]},
            {"type": "function_call", "name": "x"},
        ]}
        messages, _ = openai_responses.input_to_messages(payload)
        self.assertEqual(messages, [{"role": "user", "content": "one"},
                                    {"role": "assistant", "content": "two"}])

    def test_chat_body_maps_output_tokens(self):
        body = openai_responses.chat_body(
            {"model": "auto", "max_output_tokens": 64, "temperature": 0.1,
             "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]},
            [{"role": "user", "content": "hi"}])
        self.assertEqual(body["max_tokens"], 64)
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["tools"][0]["function"]["name"], "f")

    def test_response_object_shape(self):
        obj = openai_responses.response_object("auto", "hello", 3, 2, {"provider": "P"})
        self.assertEqual(obj["object"], "response")
        self.assertEqual(obj["status"], "completed")
        self.assertEqual(obj["model"], "auto")
        self.assertEqual(obj["output"][0]["type"], "message")
        self.assertEqual(obj["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(obj["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(obj["usage"], {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
        self.assertEqual(obj["_nova"], {"provider": "P"})

    def test_unpack_stream_reads_positions_and_key_id(self):
        from routers.gateway import UpstreamStream

        def relay():
            yield {"text": "x"}

        opened = UpstreamStream(relay(), "P", "m", 7)
        stream, provider_name, upstream_model, key_id = openai_responses._unpack_stream(opened)
        self.assertIs(stream, opened.iterator)
        self.assertEqual((provider_name, upstream_model, key_id), ("P", "m", 7))
        # Breaking out of iteration must yield the stream itself, not the
        # provider name -- the mis-indexing this helper exists to prevent.
        self.assertIs(next(iter(opened)), opened.iterator)

    def test_unpack_stream_accepts_a_plain_tuple(self):
        first, second, third = object(), object(), object()
        self.assertEqual(openai_responses._unpack_stream((first, "P", "m")), (first, "P", "m", None))
        self.assertEqual(openai_responses._unpack_stream((first, "P", "m", 9)), (first, "P", "m", 9))
        self.assertEqual(openai_responses._unpack_stream([second, "P", "m"]), (second, "P", "m", None))
        with self.assertRaises(TypeError):
            openai_responses._unpack_stream((third, "P"))


class ResponsesEndpointTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        nova_auth.reset_state()

        async def fake_pipeline(db, requested, messages, temperature, original_body=None,
                                forward_headers=None, precomputed=None):
            return ({"content": "hello there", "upstream_model": "m", "provider_name": "P",
                     "provider_id": 1, "stage": 1, "via": "upstream",
                     "usage": {"prompt_tokens": 4, "completion_tokens": 2}}, 1,
                    {"strategy": "priority", "candidates": [], "steps": [], "verdict": {},
                     "direct": True})

        self.patch = patch("routers.gateway.pipeline_nonstream", fake_pipeline)
        self.patch.start()
        self.lockout_patch = patch("routers.gateway.persist_lockouts", lambda db, force=False: None)
        self.lockout_patch.start()
        self.session_patch = patch("nova.database.SessionLocal", self.Session)
        self.session_patch.start()

        app = FastAPI()
        app.post("/v1/responses")(openai_responses.create_response)

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _db
        self.client = TestClient(app)

    def tearDown(self):
        self.patch.stop()
        self.lockout_patch.stop()
        self.session_patch.stop()

    def test_non_streaming_response(self):
        response = self.client.post("/v1/responses", json={"model": "auto", "input": "hi"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "auto")
        self.assertEqual(body["output"][0]["content"][0]["text"], "hello there")
        self.assertEqual(body["usage"]["input_tokens"], 4)
        self.assertEqual(body["_nova"]["provider"], "P")

    def test_missing_model_or_input_is_a_400(self):
        self.assertEqual(self.client.post("/v1/responses", json={"input": "hi"}).status_code, 400)
        self.assertEqual(self.client.post("/v1/responses", json={"model": "auto"}).status_code, 400)

    def test_routing_failure_is_typed(self):
        from routers import gateway

        async def failing(db, *a, **kw):
            raise gateway.HTTPError(503, "all stages failed")

        with patch("routers.gateway.pipeline_nonstream", failing):
            response = self.client.post("/v1/responses", json={"model": "auto", "input": "hi"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], 503)

    def _stream_through_upstream(self, fake_stream, fake_engine=None):
        """Run one streamed turn; returns the SSE lines and the `served` identity."""
        from types import SimpleNamespace
        from nova import routing as nova_routing
        from routers import gateway

        provider = SimpleNamespace(id=1, kind="openai", key="primary", name="P")
        row = SimpleNamespace(exposedId="primary/m", modelId="m", provider=provider)
        plan = {"strategy": "priority", "candidates": [], "steps": [], "verdict": {}, "direct": True}
        served: dict = {}

        def capture(db, requested, result, messages=None, tokens_in=0, tokens_out=0):
            served.update(result)

        logged: list[dict] = []

        def capture_log(db, **kwargs):
            logged.append(kwargs)

        patches = [
            patch("routers.gateway.attempt_upstream_stream", fake_stream),
            patch.object(nova_routing.LOCKOUTS, "is_locked", lambda *a, **k: False),
            patch("routers.gateway.route_request", lambda db, model, body=None: ([row], plan)),
            patch("routers.gateway.record_served", capture),
            patch("routers.gateway.write_log", capture_log),
            patch("routers.gateway.note_key_success", lambda key_id: None),
        ]
        if fake_engine is not None:
            patches.append(patch("routers.gateway.nova_engine.chat_stream", fake_engine))
        for p in patches:
            p.start()
        try:
            with self.client.stream("POST", "/v1/responses",
                                    json={"model": "auto", "input": "hi", "stream": True}) as res:
                lines = list(res.iter_lines())
        finally:
            for p in reversed(patches):
                p.stop()
        self.served = served
        self.logged = logged
        return lines

    def test_streaming_event_sequence(self):
        from routers.gateway import UpstreamStream

        async def fake_stream(row, messages, temperature, original_body=None, forward_headers=None,
                              block=None, key_info=None):
            async def relay():
                yield {"text": "hel", "finish_reason": None, "tool_calls": None}
                yield {"text": "lo", "finish_reason": "stop", "tool_calls": None}

            # Exactly what attempt_upstream_stream returns: a 3-tuple that
            # carries the real model + key id as attributes.
            return UpstreamStream(relay(), "P", "m", 7)

        lines = self._stream_through_upstream(fake_stream)
        events = [line for line in lines if line.startswith("event: ")]
        self.assertEqual(events[0], "event: response.created")
        self.assertIn("event: response.output_text.delta", events)
        self.assertEqual(events[-1], "event: response.completed")
        deltas = [json.loads(line[len("data: "):]) for line in lines
                  if line.startswith("data: ") and "output_text.delta" in line]
        self.assertEqual("".join(d["delta"] for d in deltas), "hello")
        completed = json.loads([line for line in lines if line.startswith("data: ")][-1][len("data: "):])
        self.assertEqual(completed["response"]["output"][0]["content"][0]["text"], "hello")
        # `_nova` and the request log must name the real upstream model, never
        # the key id.
        self.assertEqual(completed["response"]["_nova"]["upstream_model"], "m")
        self.assertEqual(completed["response"]["_nova"]["provider"], "P")
        self.assertEqual(self.logged[-1]["upstream_model"], "m")
        self.assertEqual(self.served["key_id"], 7)

    def test_streaming_plain_tuple_still_serves(self):
        async def fake_stream(row, messages, temperature, original_body=None, forward_headers=None,
                              block=None, key_info=None):
            async def relay():
                yield {"text": "ok", "finish_reason": "stop", "tool_calls": None}

            return relay(), "P", "m"

        lines = self._stream_through_upstream(fake_stream)
        events = [line for line in lines if line.startswith("event: ")]
        self.assertEqual(events[-1], "event: response.completed")
        self.assertEqual(self.served["key_id"], None)

    def test_streaming_failure_reports_per_stage_reasons(self):
        from routers import gateway

        async def fake_stream(row, messages, temperature, original_body=None, forward_headers=None,
                              block=None, key_info=None):
            raise gateway.UpstreamFailure("upstream returned HTTP 503", status=503,
                                          provider_id=1, key_id=7)

        async def dead_engine(*args, **kwargs):
            raise gateway.nova_engine.EngineUnavailable("engine down")

        lines = self._stream_through_upstream(fake_stream, fake_engine=dead_engine)
        events = [line for line in lines if line.startswith("event: ")]
        self.assertEqual(events[-1], "event: response.failed")
        failed = json.loads([line for line in lines if line.startswith("data: ")][-1][len("data: "):])
        self.assertEqual(failed["error"]["code"], 502)
        self.assertTrue(failed["error"]["message"].startswith("All routing stages failed"))
        self.assertEqual(failed["error"]["stages"], ["stage 1: upstream returned HTTP 503",
                                                     "engine: engine down"])


if __name__ == "__main__":
    unittest.main()
