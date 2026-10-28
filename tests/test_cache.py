"""Opt-in response cache: keying, TTL/LRU, opt-in gates, and the gateway hit path.

Offline. The gateway test uses a tiny FastAPI app that calls the real
`chat_completions` handler with `pipeline_nonstream` stubbed, so the cache
logic under test is the production code.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import cache as nova_cache


CHAT = {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "temperature": 0.2}


def completion(text="hi", tool_calls=None, finish="stop"):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}


class KeyTests(unittest.TestCase):
    def test_same_request_same_key(self):
        self.assertEqual(nova_cache.key_for(dict(CHAT)), nova_cache.key_for(dict(CHAT)))

    def test_irrelevant_fields_are_ignored(self):
        other = {**CHAT, "stream": False, "user": "abc", "metadata": {"x": 1}}
        self.assertEqual(nova_cache.key_for(CHAT), nova_cache.key_for(other))

    def test_semantic_fields_change_the_key(self):
        base = nova_cache.key_for(CHAT)
        for field, value in (("temperature", 0.9), ("max_tokens", 12), ("model", "auto:fast"),
                             ("top_p", 0.5), ("tools", [{"type": "function"}])):
            with self.subTest(field=field):
                self.assertNotEqual(base, nova_cache.key_for({**CHAT, field: value}))

    def test_message_content_and_order_matter(self):
        base = nova_cache.key_for(CHAT)
        edited = {**CHAT, "messages": [{"role": "user", "content": "hello!"}]}
        swapped = {**CHAT, "messages": [{"role": "system", "content": "hello"}]}
        self.assertNotEqual(base, nova_cache.key_for(edited))
        self.assertNotEqual(base, nova_cache.key_for(swapped))

    def test_content_parts_and_developer_role_normalize(self):
        parts = {**CHAT, "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]}
        developer = {**CHAT, "messages": [{"role": "developer", "content": "hello"}]}
        self.assertEqual(nova_cache.key_for(CHAT), nova_cache.key_for(parts))
        self.assertEqual(nova_cache.key_for(developer),
                         nova_cache.key_for({**CHAT, "messages": [{"role": "system", "content": "hello"}]}))


class StoreTests(unittest.TestCase):
    def test_set_get_roundtrip_returns_a_copy(self):
        store = nova_cache.ResponseCache(ttl_s=60, max_entries=4)
        store.put("k", {"v": 1})
        first = store.get("k")
        self.assertEqual(first, {"v": 1})
        first["v"] = 99
        self.assertEqual(store.get("k"), {"v": 1})

    def test_ttl_expiry(self):
        store = nova_cache.ResponseCache(ttl_s=10, max_entries=4)
        store.put("k", {"v": 1}, now=1000)
        self.assertIsNotNone(store.get("k", now=1005))
        self.assertIsNone(store.get("k", now=1011))

    def test_lru_eviction(self):
        store = nova_cache.ResponseCache(ttl_s=60, max_entries=2)
        store.put("a", {"v": 1}, now=1)
        store.put("b", {"v": 2}, now=1)
        store.get("a", now=2)          # a is now the most recently used
        store.put("c", {"v": 3}, now=2)
        self.assertIsNotNone(store.get("a", now=3))
        self.assertIsNone(store.get("b", now=3))
        self.assertEqual(store.stats()["evictions"], 1)

    def test_stats_count_hits_and_misses(self):
        store = nova_cache.ResponseCache(ttl_s=60, max_entries=2)
        store.put("k", {"v": 1})
        store.get("k")
        store.get("missing")
        stats = store.stats()
        self.assertEqual((stats["hits"], stats["misses"], stats["entries"]), (1, 1, 1))

    def test_clear(self):
        store = nova_cache.ResponseCache()
        store.put("k", {"v": 1})
        self.assertEqual(store.clear(), 1)
        self.assertEqual(store.stats()["entries"], 0)


class GateTests(unittest.TestCase):
    def test_tool_call_completions_are_never_stored(self):
        self.assertFalse(nova_cache.cacheable(completion(tool_calls=[{"id": "c"}])))
        self.assertFalse(nova_cache.cacheable(completion(finish="tool_calls")))
        self.assertFalse(nova_cache.cacheable(completion(), status=500))
        self.assertTrue(nova_cache.cacheable(completion()))

    def test_disabled_by_default(self):
        self.assertFalse(nova_cache.settings(None)["enabled"])

    def test_system_config_can_enable_it(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from nova.models import Base
        from nova.kv import set_config_json

        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            self.assertFalse(nova_cache.is_enabled(db))
            set_config_json(db, nova_cache.CFG_CACHE, {"enabled": True})
            self.assertTrue(nova_cache.is_enabled(db))
        finally:
            db.close()


class GatewayCacheTests(unittest.TestCase):
    """End-to-end: a hit is served from cache, with `_nova.cached` and via=cache."""

    def setUp(self):
        from fastapi import FastAPI, Depends
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from nova import auth as nova_auth
        from nova.database import get_db
        from nova.models import Base, RequestLog
        from routers import gateway

        self.gateway = gateway
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        nova_auth.reset_state()
        nova_cache.reset()

        self.calls = []

        async def fake_pipeline(db, requested, messages, temperature, original_body=None,
                                forward_headers=None, precomputed=None):
            self.calls.append(requested)
            return ({"content": "cached answer", "upstream_model": "m", "provider_name": "P",
                     "provider_id": 1, "stage": 1, "via": "upstream",
                     "usage": {"prompt_tokens": 3, "completion_tokens": 2}}, 1,
                    {"strategy": "priority", "candidates": [], "steps": [], "verdict": {},
                     "direct": True})

        self.pipeline = patch.object(gateway, "pipeline_nonstream", fake_pipeline)
        self.pipeline.start()
        self.lockouts = patch.object(gateway, "persist_lockouts", lambda db, force=False: None)
        self.lockouts.start()
        self.cache_flag = patch.object(nova_cache, "RESPONSE_CACHE", True)
        self.cache_flag.start()
        # The handler opens its own SessionLocal() for logging/usage; point it
        # at the test database so RequestLog assertions see what it wrote.
        self.session_patch = patch("nova.database.SessionLocal", self.Session)
        self.session_patch.start()

        app = FastAPI()
        app.post("/v1/chat/completions")(gateway.chat_completions)

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _db
        self.client = TestClient(app)

    def tearDown(self):
        self.pipeline.stop()
        self.lockouts.stop()
        self.cache_flag.stop()
        self.session_patch.stop()
        nova_cache.reset()

    def test_second_identical_request_is_a_cache_hit(self):
        first = self.client.post("/v1/chat/completions", json=CHAT)
        second = self.client.post("/v1/chat/completions", json=CHAT)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["_nova"]["cached"])
        self.assertTrue(second.json()["_nova"]["cached"])
        self.assertEqual(second.json()["choices"][0]["message"]["content"], "cached answer")
        self.assertEqual(len(self.calls), 1)  # the upstream ran exactly once

    def test_hit_writes_a_requestlog_row_with_via_cache(self):
        from sqlalchemy import select
        from nova.models import RequestLog

        self.client.post("/v1/chat/completions", json=CHAT)
        self.client.post("/v1/chat/completions", json=CHAT)
        with self.Session() as db:
            vias = [r.via for r in db.scalars(select(RequestLog)).all()]
        self.assertIn("cache", vias)

    def test_no_cache_header_bypasses_read_and_write(self):
        self.client.post("/v1/chat/completions", json=CHAT)
        fresh = self.client.post("/v1/chat/completions", json=CHAT,
                                 headers={"x-nova-no-cache": "1"})
        self.assertFalse(fresh.json()["_nova"]["cached"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(nova_cache.stats()["entries"], 1)

    def test_tool_call_answers_are_not_cached(self):
        asyncio.get_event_loop_policy()
        from nova.cache import CACHE

        body = completion(tool_calls=[{"id": "c"}])
        self.assertFalse(nova_cache.cacheable(body))
        CACHE.clear()
        self.assertEqual(len(CACHE._data), 0)


if __name__ == "__main__":
    unittest.main()
