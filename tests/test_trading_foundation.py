"""Phase-0 trading foundation: schema, append-only audit, kill-switch, admin API.

Offline: a throwaway in-memory SQLite database (StaticPool) and FastAPI's
TestClient. No upstream/exchange calls exist in Phase 0.
"""
import json
import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import trading, trading_audit, trading_settings
from nova.database import get_db
from nova.models import (
    ALL_TABLES,
    Base,
    Provider,
    RiskPolicy,
    SystemConfig,
    TradeAuditLog,
    TradeFill,
    TradeOrder,
)
from routers import admin_trading


def make_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()

    def test_tables_are_created(self):
        names = set(inspect(self.engine).get_table_names())
        for table in ("TradeOrder", "TradeFill", "RiskPolicy", "TradeAuditLog"):
            self.assertIn(table, names)

    def test_all_tables_registry_contains_trading_models(self):
        for model in (TradeOrder, TradeFill, RiskPolicy, TradeAuditLog):
            self.assertIn(model, ALL_TABLES)

    def test_trade_order_columns_and_indexes(self):
        columns = {c["name"] for c in inspect(self.engine).get_columns("TradeOrder")}
        self.assertTrue({
            "id", "publicId", "clientOrderId", "exchange", "providerId", "symbol",
            "side", "orderType", "quantity", "notional", "limitPrice", "status",
            "mode", "reason", "clientKeyId", "metadata", "createdAt", "updatedAt",
        } <= columns)
        indexes = {i["name"] for i in inspect(self.engine).get_indexes("TradeOrder")}
        self.assertIn("TradeOrder_clientOrderId_idx", indexes)
        self.assertIn("TradeOrder_createdAt_idx", indexes)

    def test_audit_columns_and_indexes(self):
        columns = {c["name"] for c in inspect(self.engine).get_columns("TradeAuditLog")}
        self.assertTrue({
            "id", "ts", "event", "action", "status", "code", "actor",
            "clientKeyId", "providerId", "providerKey", "exchange", "symbol",
            "orderPublicId", "message", "payload", "mode",
        } <= columns)
        indexes = {i["name"] for i in inspect(self.engine).get_indexes("TradeAuditLog")}
        for name in ("TradeAuditLog_ts_idx", "TradeAuditLog_event_idx",
                     "TradeAuditLog_orderPublicId_idx", "TradeAuditLog_clientKeyId_idx",
                     "TradeAuditLog_exchange_idx"):
            self.assertIn(name, indexes)

    def test_risk_policy_keeps_metadata_column_name(self):
        columns = {c["name"] for c in inspect(self.engine).get_columns("RiskPolicy")}
        self.assertIn("metadata", columns)
        self.assertIn("allowedSymbols", columns)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def _seed(self, **overrides):
        with self.Session() as db:
            row = trading_audit.record(db, event=overrides.pop("event", "intent"), **overrides)
            db.commit()
            return row.id

    def test_record_inserts_and_serializes_snake_case(self):
        payload = {"qty": 0.5, "nested": {"ok": True, "tags": ["a", "b"]}}
        with self.Session() as db:
            row = trading_audit.record(
                db,
                event="submission",
                action="trade.submit",
                status="accepted",
                code="ok",
                actor="gateway",
                client_key_id=7,
                provider_id=3,
                provider_key="binance",
                exchange="binance",
                symbol="BTC/USDT",
                order_public_id="ord_1",
                message="submitted",
                payload=payload,
                mode="sandbox",
            )
            self.assertIsNotNone(row.id)
            db.commit()

        with self.Session() as db:
            stored = db.scalar(select(TradeAuditLog))
            out = trading_audit.serialize_audit(stored)
            self.assertEqual(out["event"], "submission")
            self.assertEqual(out["action"], "trade.submit")
            self.assertEqual(out["actor"], "gateway")
            self.assertEqual(out["client_key_id"], 7)
            self.assertEqual(out["provider_id"], 3)
            self.assertEqual(out["provider_key"], "binance")
            self.assertEqual(out["exchange"], "binance")
            self.assertEqual(out["symbol"], "BTC/USDT")
            self.assertEqual(out["order_public_id"], "ord_1")
            self.assertEqual(out["message"], "submitted")
            self.assertEqual(out["mode"], "sandbox")
            self.assertEqual(out["payload"], payload)
            self.assertIsInstance(out["ts"], int)
            self.assertGreater(out["ts"], 0)
            self.assertNotIn("clientKeyId", out)

    def test_record_requires_an_event(self):
        with self.Session() as db:
            with self.assertRaises(ValueError):
                trading_audit.record(db, event="   ")
            db.rollback()

    def test_orm_update_is_refused(self):
        row_id = self._seed(action="before")
        with self.Session() as db:
            row = db.get(TradeAuditLog, row_id)
            row.action = "tampered"
            with self.assertRaises(trading_audit.TradeAuditImmutable):
                db.commit()

    def test_orm_delete_is_refused(self):
        row_id = self._seed()
        with self.Session() as db:
            row = db.get(TradeAuditLog, row_id)
            db.delete(row)
            with self.assertRaises(trading_audit.TradeAuditImmutable):
                db.commit()

    def test_payload_round_trips_as_json(self):
        payload = {"kill_switch": True, "counts": [1, 2, 3], "note": None}
        row_id = self._seed(event="kill_switch", payload=payload)
        with self.Session() as db:
            stored = db.get(TradeAuditLog, row_id)
            self.assertEqual(json.loads(stored.payload), payload)
            self.assertEqual(trading_audit.serialize_audit(stored)["payload"], payload)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def test_load_defaults_false_without_a_row(self):
        with self.Session() as db:
            self.assertEqual(trading_settings.load(db), {"kill_switch": False})
            self.assertFalse(trading_settings.kill_switch_engaged(db))

    def test_malformed_config_means_false(self):
        with self.Session() as db:
            db.add(SystemConfig(key=trading_settings.CFG_TRADING, value="{not json"))
            db.commit()
        with self.Session() as db:
            self.assertFalse(trading_settings.load(db)["kill_switch"])

    def test_save_persists_and_appends_kill_switch_audit(self):
        with self.Session() as db:
            self.assertEqual(trading_settings.save(db, kill_switch=True), {"kill_switch": True})
        with self.Session() as db:
            self.assertTrue(trading_settings.kill_switch_engaged(db))
            stored = db.get(SystemConfig, trading_settings.CFG_TRADING)
            self.assertEqual(json.loads(stored.value), {"kill_switch": True})
            rows = db.scalars(select(TradeAuditLog).order_by(TradeAuditLog.id)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].event, "kill_switch")
            self.assertEqual(rows[0].action, "trading.kill_switch")
            self.assertEqual(rows[0].actor, "admin")
            self.assertEqual(rows[0].status, "engaged")
            self.assertEqual(json.loads(rows[0].payload), {"kill_switch": True})

    def test_save_release_audits_released_status(self):
        with self.Session() as db:
            trading_settings.save(db, kill_switch=True)
            trading_settings.save(db, kill_switch=False)
        with self.Session() as db:
            self.assertFalse(trading_settings.kill_switch_engaged(db))
            rows = db.scalars(select(TradeAuditLog).order_by(TradeAuditLog.id)).all()
            self.assertEqual([r.status for r in rows], ["engaged", "released"])


class TradingGateTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def test_ensure_trading_enabled_blocks_when_engaged(self):
        with self.Session() as db:
            trading_settings.save(db, kill_switch=True)
            with self.assertRaises(trading.TradingError) as ctx:
                trading.ensure_trading_enabled(db)
        err = ctx.exception
        self.assertEqual(err.status, 503)
        self.assertEqual(err.code, "trading_disabled")
        self.assertEqual(err.message, "Trading is disabled by the operator's kill-switch.")
        self.assertEqual(err.to_payload(), {
            "error": {
                "message": "Trading is disabled by the operator's kill-switch.",
                "type": "trading_error",
                "code": "trading_disabled",
            }
        })

    def test_ensure_trading_enabled_passes_when_released(self):
        with self.Session() as db:
            self.assertIsNone(trading.ensure_trading_enabled(db))
            trading_settings.save(db, kill_switch=True)
            trading_settings.save(db, kill_switch=False)
            self.assertIsNone(trading.ensure_trading_enabled(db))

    def test_error_payload_carries_retry_and_extra(self):
        err = trading.TradingError(429, "rate_limited", "slow down",
                                   retry_after=5, extra={"policy": "max-notional"})
        payload = err.to_payload()
        self.assertEqual(payload["retry_after"], 5)
        self.assertEqual(payload["policy"], "max-notional")
        self.assertEqual(payload["error"]["code"], "rate_limited")


class AdminTradingApi:
    def __init__(self):
        self.engine = make_engine()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = FastAPI()
        self.app.include_router(admin_trading.router, prefix="/api/admin/trading")

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = _db
        self.client = TestClient(self.app)

    def add_provider(self, key, kind="exchange", enabled=True):
        with self.Session() as db:
            db.add(Provider(key=key, name=key.title(), kind=kind, enabled=enabled))
            db.commit()


class AdminTradingTests(unittest.TestCase):
    def setUp(self):
        self.api = AdminTradingApi()

    def test_get_reports_state_and_enabled_exchange_providers(self):
        self.api.add_provider("binance", enabled=True)
        self.api.add_provider("kraken", enabled=False)
        self.api.add_provider("openai", kind="openai", enabled=True)
        response = self.api.client.get("/api/admin/trading")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "trading": {"kill_switch": False, "exchange_providers": 1},
        })

    def test_post_true_persists_and_audits(self):
        response = self.api.client.post("/api/admin/trading", json={"kill_switch": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["trading"]["kill_switch"])
        with self.api.Session() as db:
            self.assertTrue(trading_settings.load(db)["kill_switch"])
            rows = db.scalars(select(TradeAuditLog)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].event, "kill_switch")
            self.assertEqual(rows[0].status, "engaged")

    def test_post_false_releases(self):
        self.api.client.post("/api/admin/trading", json={"kill_switch": True})
        response = self.api.client.post("/api/admin/trading", json={"kill_switch": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["trading"]["kill_switch"])
        with self.api.Session() as db:
            self.assertFalse(trading_settings.load(db)["kill_switch"])
            rows = db.scalars(select(TradeAuditLog).order_by(TradeAuditLog.id)).all()
            self.assertEqual([r.status for r in rows], ["engaged", "released"])

    def test_post_rejects_non_boolean(self):
        for payload in ({"kill_switch": "true"}, {"kill_switch": 1}, {"kill_switch": None}, {}):
            response = self.api.client.post("/api/admin/trading", json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": "kill_switch must be a boolean"})
        with self.api.Session() as db:
            self.assertEqual(db.scalars(select(TradeAuditLog)).all(), [])

    def test_post_tolerates_invalid_json_body(self):
        response = self.api.client.post(
            "/api/admin/trading", content=b"not-json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "kill_switch must be a boolean"})

    def test_audit_endpoint_returns_newest_first(self):
        with self.api.Session() as db:
            trading_audit.record(db, event="intent", message="first", payload={"n": 1})
            db.commit()
        self.api.client.post("/api/admin/trading", json={"kill_switch": True})
        response = self.api.client.get("/api/admin/trading/audit")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["audit"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "kill_switch")
        self.assertEqual(rows[0]["status"], "engaged")
        self.assertEqual(rows[1]["event"], "intent")
        self.assertEqual(rows[1]["payload"], {"n": 1})
        self.assertEqual(rows[1]["message"], "first")
        for row in rows:
            self.assertIn("client_key_id", row)
            self.assertNotIn("clientKeyId", row)

    def test_audit_limit_is_clamped(self):
        for n in range(3):
            self.api.client.post("/api/admin/trading", json={"kill_switch": bool(n % 2)})
        small = self.api.client.get("/api/admin/trading/audit?limit=0")
        self.assertEqual(small.status_code, 200)
        self.assertEqual(len(small.json()["audit"]), 1)
        large = self.api.client.get("/api/admin/trading/audit?limit=999")
        self.assertEqual(large.status_code, 200)
        self.assertEqual(len(large.json()["audit"]), 3)


if __name__ == "__main__":
    unittest.main()