"""Named routing profiles (`auto:<name>`) and sticky sessions.

Pure resolver + storage round-trip + the gateway wiring: the profile's chain is
honoured, its strategy replaces the default, and the response `model` still
equals the requested id (identity spoofing invariant).
"""
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import profiles, routing, session as nova_session
from nova.models import Base, Model, ModelRoute, Provider


class ResolverTests(unittest.TestCase):
    STORED = {"fast": {"fallbacks": ["groq/a", "groq/b"], "strategy": "latency", "note": "quick"}}

    def test_normal_ids_have_no_profile(self):
        self.assertEqual(profiles.resolve_profile("gpt-4o", self.STORED), ("", None))
        self.assertEqual(profiles.resolve_profile("auto", self.STORED), ("", None))

    def test_known_profile_returns_its_spec(self):
        name, spec = profiles.resolve_profile("auto:fast", self.STORED)
        self.assertEqual(name, "fast")
        self.assertEqual(spec["fallbacks"], ["groq/a", "groq/b"])
        self.assertEqual(spec["strategy"], "latency")
        self.assertTrue(spec["auto_standins"])

    def test_unknown_profile_is_reported_not_guessed(self):
        self.assertEqual(profiles.resolve_profile("auto:nope", self.STORED), ("nope", None))

    def test_parse_and_ids(self):
        self.assertEqual(profiles.parse_profile_id("auto:x"), "x")
        self.assertEqual(profiles.parse_profile_id("auto"), "")
        self.assertIsNone(profiles.parse_profile_id("gpt-4o"))
        self.assertEqual(profiles.profile_ids(self.STORED), ["auto:fast"])

    def test_spec_normalization(self):
        spec = profiles.normalize_spec({"fallbacks": "a, b ,a", "strategy": "telepathy",
                                        "auto_standins": False, "note": 5})
        self.assertEqual(spec["fallbacks"], ["a", "b"])
        self.assertEqual(spec["strategy"], "")      # unknown strategy dropped
        self.assertFalse(spec["auto_standins"])
        self.assertEqual(spec["note"], "5")
        self.assertTrue(profiles.normalize_spec({})["auto_standins"])


class GatewayProfileTests(unittest.TestCase):
    def setUp(self):
        from routers import gateway

        self.gateway = gateway
        self.saved_registry = routing.LOCKOUTS
        routing.LOCKOUTS = routing.LockoutRegistry()
        nova_session.reset()
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()

        primary = Provider(key="primary", name="Primary", kind="openai",
                           baseUrl="https://primary.test", prefix="primary/", priority=10)
        fast = Provider(key="fast", name="Fast", kind="openai",
                        baseUrl="https://fast.test", prefix="fast/", priority=20)
        self.db.add_all([primary, fast])
        self.db.flush()
        self.db.add_all([
            Model(providerId=primary.id, modelId="m", exposedId="primary/m", status="healthy"),
            Model(providerId=fast.id, modelId="a", exposedId="fast/a", status="healthy", latencyMs=999),
            Model(providerId=fast.id, modelId="b", exposedId="fast/b", status="healthy", latencyMs=999),
        ])
        self.db.add(ModelRoute(publicId="primary/m", fallbacks='["fast/b"]', auto=False))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        nova_session.reset()
        routing.LOCKOUTS = self.saved_registry

    def test_saving_profiles_round_trips(self):
        profiles.save_profiles(self.db, {"fast": {"fallbacks": ["fast/a"], "strategy": "latency"}})
        stored = profiles.load_profiles(self.db)
        self.assertEqual(stored["fast"]["fallbacks"], ["fast/a"])
        self.assertEqual(stored["fast"]["strategy"], "latency")

    def test_auto_profile_uses_its_own_chain_first(self):
        profiles.save_profiles(self.db, {"mine": {"fallbacks": ["fast/a"], "strategy": "priority"}})
        rows, plan = self.gateway.route_request(self.db, "auto:mine")
        self.assertEqual(rows[0].exposedId, "fast/a")
        self.assertEqual(plan["profile"]["name"], "mine")
        self.assertTrue(plan["direct"])  # the profile id itself is the request

    def test_profile_strategy_overrides_the_default(self):
        profiles.save_profiles(self.db, {"lat": {"fallbacks": ["fast/b", "fast/a"], "strategy": "latency"}})
        self.db.query(Model).filter_by(exposedId="fast/a").one().latencyMs = 5
        self.db.commit()
        rows, plan = self.gateway.route_request(self.db, "auto:lat")
        self.assertEqual(plan["strategy"], "latency")
        self.assertEqual(rows[0].exposedId, "fast/a")

    def test_unknown_profile_is_a_typed_404(self):
        with self.assertRaises(self.gateway.HTTPError) as ctx:
            self.gateway.route_request(self.db, "auto:missing")
        self.assertEqual(ctx.exception.status, 404)

    def test_auto_standins_can_be_switched_off_per_profile(self):
        profiles.save_profiles(self.db, {"tight": {"fallbacks": ["fast/a"], "auto_standins": False}})
        rows, _plan = self.gateway.route_request(self.db, "auto:tight")
        self.assertEqual([r.exposedId for r in rows], ["fast/a"])


class StickySessionTests(unittest.TestCase):
    def setUp(self):
        nova_session.reset()

    def tearDown(self):
        nova_session.reset()

    def test_session_id_from_header_or_metadata(self):
        self.assertEqual(nova_session.session_id_from({"x-nova-session": " s1 "}, None), "s1")
        self.assertEqual(nova_session.session_id_from({}, {"metadata": {"session_id": "s2"}}), "s2")
        self.assertEqual(nova_session.session_id_from({}, {}), "")

    def test_pin_and_expiry(self):
        nova_session.SESSIONS.ttl_s = 1
        nova_session.pin("s", "fast/a", "fast")
        self.assertIsNotNone(nova_session.get_pin("s"))
        self.assertIsNone(nova_session.SESSIONS.get("s", now=__import__("time").time() + 2))

    def test_handoff_reports_from_to_reason_and_moves_the_pin(self):
        nova_session.pin("s", "fast/a")
        note = nova_session.handoff("s", "fast/a", "primary/m", "pinned model failed")
        self.assertEqual(note, {"from": "fast/a", "to": "primary/m", "reason": "pinned model failed"})
        self.assertEqual(nova_session.get_pin("s").model, "primary/m")

    def test_reorder_keeps_every_candidate(self):
        class R:
            def __init__(self, exposedId):
                self.exposedId = exposedId

        rows = [R("a"), R("b"), R("c")]
        reordered = nova_session.reorder(rows, "c")
        self.assertEqual([r.exposedId for r in reordered], ["c", "a", "b"])
        self.assertEqual(nova_session.reorder(rows, "missing"), rows)


if __name__ == "__main__":
    unittest.main()
