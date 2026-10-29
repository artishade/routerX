"""Per-ClientKey trading scopes (Phase 1) — admin wire field, defaults, edits.

Offline: a throwaway in-memory SQLite database (StaticPool) and the admin
endpoints called directly with a request-scoped session. The wire contract is
snake_case (``trading_scopes``); the DB column is camelCase (``tradingScopes``).
Empty means NO trading access, so the default must never be a wildcard.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova.models import ClientKey
from routers.admin_client_keys import (
    create_client_key,
    to_client_key,
    update_client_key,
)


def make_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    ClientKey.__table__.create(engine)
    return engine


class ClientKeyScopeTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _create(self, body: dict) -> dict:
        response = create_client_key(body=body, db=self.db)
        self.assertEqual(response.status_code, 200, response.body)
        return json.loads(response.body)

    def _scopes(self, key_id: int) -> str:
        row = self.db.get(ClientKey, key_id)
        self.assertIsNotNone(row)
        self.db.refresh(row)
        return row.tradingScopes

    # ------------------------------------------------------------ serializer
    def test_to_client_key_includes_snake_case_scopes(self):
        key = ClientKey(name="serialized", token="nova-sk-serialized")
        self.db.add(key)
        self.db.commit()

        body = to_client_key(key)

        self.assertIn("trading_scopes", body)
        self.assertEqual(body["trading_scopes"], "")
        self.assertNotIn("tradingScopes", body)

    def test_to_client_key_guards_a_null_scope(self):
        key = ClientKey(name="unflushed", token="nova-sk-unflushed")
        # The Python-side column default is only applied on INSERT.
        self.assertIsNone(key.tradingScopes)
        self.assertEqual(to_client_key(key)["trading_scopes"], "")

    # --------------------------------------------------------------- create
    def test_create_defaults_to_no_trading_access(self):
        body = self._create({"name": "a"})

        self.assertEqual(body["trading_scopes"], "")
        self.assertEqual(self._scopes(body["id"]), "")

    def test_create_persists_and_serializes_scopes_exactly(self):
        raw = "trading:read, trading:quote"
        body = self._create({"name": "b", "trading_scopes": raw})

        self.assertEqual(body["trading_scopes"], raw)
        self.assertEqual(self._scopes(body["id"]), raw)

    def test_create_strips_surrounding_whitespace(self):
        body = self._create({"name": "c", "trading_scopes": "  trading:positions  "})

        self.assertEqual(body["trading_scopes"], "trading:positions")
        self.assertEqual(self._scopes(body["id"]), "trading:positions")

    def test_create_accepts_globs_and_unknown_scopes_verbatim(self):
        for raw in ("*", "trading:*", "trading:future.thing"):
            with self.subTest(raw=raw):
                body = self._create({"name": "g", "trading_scopes": raw})
                self.assertEqual(body["trading_scopes"], raw)
                self.assertEqual(self._scopes(body["id"]), raw)

    # --------------------------------------------------------------- update
    def test_update_sets_then_clears_scopes(self):
        created = self._create({"name": "d"})
        key_id = created["id"]

        response = update_client_key(
            id=str(key_id), body={"trading_scopes": "trading:read"}, db=self.db
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._scopes(key_id), "trading:read")

        response = update_client_key(
            id=str(key_id), body={"trading_scopes": ""}, db=self.db
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._scopes(key_id), "")

        # The clear must not disturb the other fields.
        row = self.db.get(ClientKey, key_id)
        self.db.refresh(row)
        self.assertEqual(row.name, "d")
        self.assertEqual(row.allowedModels, "*")

    def test_update_drops_only_the_sent_scopes(self):
        created = self._create(
            {"name": "e", "trading_scopes": "trading:read", "allowed_models": "auto"}
        )
        update_client_key(
            id=str(created["id"]),
            body={"allowed_models": "auto,chat"},
            db=self.db,
        )
        self.assertEqual(self._scopes(created["id"]), "trading:read")

        row = self.db.get(ClientKey, created["id"])
        self.db.refresh(row)
        self.assertEqual(row.allowedModels, "auto,chat")

    # ---------------------------------------------------------- non-strings
    def test_non_string_scopes_are_ignored_on_create(self):
        body = self._create({"name": "f", "trading_scopes": ["trading:read"]})

        self.assertEqual(body["trading_scopes"], "")
        self.assertEqual(self._scopes(body["id"]), "")

    def test_non_string_scopes_are_ignored_on_patch(self):
        created = self._create({"name": "h", "trading_scopes": "trading:read"})
        key_id = created["id"]

        for value in (42, None, ["trading:quote"], {"scope": "trading:quote"}):
            with self.subTest(value=value):
                response = update_client_key(
                    id=str(key_id), body={"trading_scopes": value}, db=self.db
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self._scopes(key_id), "trading:read")


if __name__ == "__main__":
    unittest.main()