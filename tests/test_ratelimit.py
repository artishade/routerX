"""Per-(provider, model, key) usage windows, caps and key cooldowns.

Offline: pure helpers, then the `UsageWindow` table over an in-memory SQLite
database. The last class checks that a capped pair is dropped from the plan
before any upstream is contacted.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import ratelimit
from nova.models import Base, ProviderKey, utcnow


def db_session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


class BucketTests(unittest.TestCase):
    def test_minute_and_day_buckets(self):
        now = 1_700_000_123
        self.assertEqual(ratelimit.bucket(now, "minute"), now // 60)
        self.assertEqual(ratelimit.bucket(now, "day"), now // 86_400)
        self.assertEqual(ratelimit.bucket(now, "minute"), ratelimit.bucket(now + 59 - now % 60, "minute"))

    def test_datetime_input_is_naive_utc(self):
        dt = datetime(2026, 1, 1, 12, 0, 0)
        epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
        self.assertEqual(ratelimit.bucket(dt, "day"), epoch // 86_400)

    def test_bucket_boundaries(self):
        self.assertEqual(ratelimit.bucket(119, "minute"), 1)
        self.assertEqual(ratelimit.bucket(120, "minute"), 2)
        self.assertEqual(ratelimit.bucket(86_399, "day"), 0)
        self.assertEqual(ratelimit.bucket(86_400, "day"), 1)


class LimitTests(unittest.TestCase):
    def test_zero_is_unlimited_by_default(self):
        self.assertTrue(all(v == 0 for v in ratelimit.limits_from({}, env={}).values()))
        self.assertFalse(ratelimit.is_exhausted({"requests": 1_000_000, "tokensIn": 1}, {}))

    def test_env_defaults_and_provider_overrides(self):
        env = {"rpm": 10, "tpm": 0, "rpd": 0, "tpd": 0}
        self.assertEqual(ratelimit.limits_from({}, env=env)["rpm"], 10)
        merged = ratelimit.limits_from({"rpm": 3}, env=env)
        self.assertEqual(merged["rpm"], 3)
        self.assertEqual(merged["tpm"], 0)  # an override never invents a budget

    def test_normalize_limits_ignores_bad_values(self):
        self.assertEqual(ratelimit.normalize_limits({"rpm": "7", "tpm": -2, "rpd": True, "x": 1}),
                         {"rpm": 7, "tpm": 0, "rpd": 0, "tpd": 0})

    def test_is_exhausted_per_limit(self):
        usage = {"requests": 5, "tokensIn": 100, "tokensOut": 50}
        self.assertTrue(ratelimit.is_exhausted(usage, {"rpm": 5}))
        self.assertTrue(ratelimit.is_exhausted(usage, {"tpd": 150}))
        self.assertFalse(ratelimit.is_exhausted(usage, {"rpm": 6, "tpd": 151}))
        self.assertFalse(ratelimit.is_exhausted({}, {"rpm": 1}))

    def test_merge_records_accepts_dicts_and_rows(self):
        merged = ratelimit.merge_records([
            {"windowKind": "minute", "requests": 2, "tokensIn": 3, "tokensOut": 4},
            {"window_kind": "day", "requests": 10, "tokens_in": 1, "tokens_out": 1},
            {"windowKind": "week", "requests": 99},
        ])
        self.assertEqual(merged["minute"]["requests"], 2)
        self.assertEqual(merged["minute"]["tokensIn"] + merged["minute"]["tokensOut"], 7)
        self.assertEqual(merged["day"]["requests"], 10)
        self.assertNotIn("week", merged)


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        ratelimit.reset_prune_clock()
        self.db = db_session()

    def tearDown(self):
        self.db.close()

    def test_record_usage_upserts_both_windows(self):
        ratelimit.record_usage(self.db, 1, "m", 7, 100, 20)
        ratelimit.record_usage(self.db, 1, "m", 7, 50, 5)
        rows = self.db.query(ratelimit.UsageWindow).all()
        self.assertEqual(len(rows), 2)  # one minute row, one day row
        by_kind = {r.windowKind: r for r in rows}
        self.assertEqual(by_kind["minute"].requests, 2)
        self.assertEqual(by_kind["minute"].tokensIn, 150)
        self.assertEqual(by_kind["day"].tokensOut, 25)

    def test_capped_is_empty_without_limits(self):
        ratelimit.record_usage(self.db, 1, "m", 7, 10, 10)
        self.assertEqual(ratelimit.capped(self.db, {1: "p"}, limits={}), set())

    def test_capped_honours_the_provider_override(self):
        ratelimit.record_usage(self.db, 1, "m", 7, 1, 1)
        ratelimit.record_usage(self.db, 1, "m", 7, 1, 1)
        caps = ratelimit.capped(self.db, {1: "p"}, limits={"p": {"rpm": 2}})
        self.assertEqual(caps, {(1, "m")})
        self.assertEqual(ratelimit.capped(self.db, {1: "p"}, limits={"p": {"rpm": 3}}), set())

    def test_capped_from_env_defaults(self):
        original = (ratelimit.DEFAULT_RPD,)
        ratelimit.DEFAULT_RPD = 1
        try:
            ratelimit.record_usage(self.db, 2, "other", 0)
            # Without a knob, `capped` returns early; with an explicit knob the
            # env default still applies to a provider with no override.
            caps = ratelimit.capped(self.db, {2: "q"}, limits={"*": {}})
        finally:
            ratelimit.DEFAULT_RPD = original[0]
        self.assertIn((2, "other"), caps)

    def test_limits_round_trip_through_system_config(self):
        # SystemConfig table is part of Base.metadata: save/load through kv.
        saved = ratelimit.save_limits(self.db, {"groq": {"rpm": 30, "bogus": 1}})
        self.assertEqual(saved["groq"]["rpm"], 30)
        self.assertEqual(ratelimit.load_limits(self.db)["groq"]["tpd"], 0)

    def test_prune_removes_old_windows_only(self):
        now = utcnow()
        old = now - timedelta(days=3)
        ratelimit.record_usage(self.db, 1, "m", 0, 1, 1, now=old.timestamp())
        ratelimit.record_usage(self.db, 1, "m", 0, 1, 1)
        removed = ratelimit.prune_usage(self.db)
        self.assertGreaterEqual(removed, 2)
        self.assertEqual(self.db.query(ratelimit.UsageWindow).count(), 2)  # today's pair

    def test_usage_snapshot_shape(self):
        ratelimit.record_usage(self.db, 1, "m", 0, 10, 5)
        snap = ratelimit.usage_snapshot(self.db)
        self.assertEqual(snap[0]["model"], "m")
        self.assertEqual(snap[0]["minute"]["tokens"], 15)
        self.assertIn("day", snap[0])


class RouteSkipTests(unittest.TestCase):
    """A capped pair must be skipped by `route_request`, never dispatched."""

    def setUp(self):
        self.db = db_session()
        self.saved = ratelimit.CFG_RATE_LIMITS
        from nova.kv import set_config_json
        set_config_json(self.db, ratelimit.CFG_RATE_LIMITS, {"primary": {"rpm": 1}})

    def tearDown(self):
        self.db.close()

    def _seed(self):
        from nova.models import Model, ModelRoute, Provider

        primary = Provider(key="primary", name="Primary", kind="openai",
                           baseUrl="https://primary.test", prefix="primary/", priority=10)
        backup = Provider(key="backup", name="Backup", kind="openai",
                          baseUrl="https://backup.test", prefix="backup/", priority=20)
        self.db.add_all([primary, backup])
        self.db.flush()
        self.db.add_all([
            Model(providerId=primary.id, modelId="fast", exposedId="primary/fast"),
            Model(providerId=backup.id, modelId="slow", exposedId="backup/slow"),
        ])
        self.db.add(ModelRoute(publicId="primary/fast", fallbacks='["backup/slow"]', auto=False))
        self.db.commit()
        return primary

    def test_capped_direct_row_falls_through_to_the_next_stage(self):
        from routers import gateway

        primary = self._seed()
        ratelimit.record_usage(self.db, primary.id, "fast", 0)
        rows, _plan = gateway.route_request(self.db, "primary/fast")
        self.assertEqual([r.exposedId for r in rows], ["backup/slow"])


class CooldownTests(unittest.TestCase):
    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(ratelimit.parse_retry_after("120"), 120.0)
        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        from email.utils import format_datetime

        parsed = ratelimit.parse_retry_after(format_datetime(future))
        self.assertIsNotNone(parsed)
        self.assertTrue(80 <= parsed <= 92)
        self.assertIsNone(ratelimit.parse_retry_after("not-a-date"))

    def test_defaults_by_status_and_backoff(self):
        self.assertEqual(ratelimit.cooldown_seconds(400), 0)
        self.assertGreater(ratelimit.cooldown_seconds(429), 0)
        self.assertGreater(ratelimit.cooldown_seconds(500), 0)
        self.assertGreater(ratelimit.cooldown_seconds(401), 0)
        self.assertGreater(ratelimit.cooldown_seconds(429, err_count=3),
                           ratelimit.cooldown_seconds(429, err_count=0))

    def test_retry_after_wins_and_is_capped(self):
        self.assertEqual(ratelimit.cooldown_seconds(429, "5"), 5.0)
        self.assertLessEqual(ratelimit.cooldown_seconds(429, "999999"), ratelimit.KEY_COOLDOWN_MAX_S)

    def test_set_and_clear_cooldown_persist(self):
        db = db_session()
        try:
            key = ProviderKey(providerId=1, apiKey="secret-token", label="k")
            db.add(key)
            db.commit()
            ratelimit.set_cooldown(db, key.id, 30, "HTTP 429")
            db.refresh(key)
            self.assertTrue(ratelimit.is_cooling(key))
            self.assertEqual(key.errCount, 1)
            self.assertEqual(key.lastError, "HTTP 429")
            ratelimit.clear_cooldown(db, key.id)
            db.refresh(key)
            self.assertFalse(ratelimit.is_cooling(key))
            self.assertEqual(key.reqCount, 1)
            self.assertIsNotNone(key.lastUsedAt)
        finally:
            db.close()

    def test_pick_key_skips_cooling_keys_until_all_are_cooling(self):
        now = utcnow()
        ready = ProviderKey(id=1, providerId=1, apiKey="a", weight=9)
        cooling_soon = ProviderKey(id=2, providerId=1, apiKey="b", weight=5, cooldownUntil=now + timedelta(seconds=30))
        cooling_late = ProviderKey(id=3, providerId=1, apiKey="c", weight=1, cooldownUntil=now + timedelta(minutes=5))

        picked, fallback = ratelimit.pick_key([ready, cooling_soon, cooling_late], now)
        self.assertEqual((picked.id, fallback), (1, False))

        picked, fallback = ratelimit.pick_key([cooling_late, cooling_soon], now)
        self.assertEqual((picked.id, fallback), (2, True))
        self.assertEqual(ratelimit.pick_key([], now), (None, False))


if __name__ == "__main__":
    unittest.main()
