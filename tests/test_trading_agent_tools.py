"""Phase 1 read-only trading tools for the RouterX agent.

Run: python -m unittest discover -s tests -p "test_trading_agent_tools.py" -v

Offline by design: dispatch goes through patched nova.trading reads, so no
exchange, network or credentials are involved.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import agent, trading
from nova.agent import AgentPlan, parse_plan
from routers import agent_api

QUOTE = {
    "exchange": "binance",
    "symbol": "BTC/USDT",
    "base_asset": "BTC",
    "quote_asset": "USDT",
    "bid": 64000.5,
    "ask": 64001.5,
    "last": 64001.0,
    "ts": "2026-01-01T00:00:00Z",
    "source": "binance",
}

POSITIONS = {
    "exchange": "binance",
    "as_of": "2026-01-01T00:00:00Z",
    "positions": [
        {
            "symbol": "ETH/USDT",
            "side": "long",
            "quantity": 1.5,
            "entry_price": 3000,
            "mark_price": 3100,
            "unrealized_pnl": 150,
            "leverage": 3,
        }
    ],
}

BALANCES = {
    "exchange": "binance",
    "as_of": "2026-01-01T00:00:00Z",
    "balances": [{"asset": "USDT", "free": 100, "locked": 5, "total": 105}],
}


class TradingCatalogueTests(unittest.TestCase):
    def test_actions_are_registered(self):
        for action in ("trading_quote", "trading_positions", "trading_balances"):
            self.assertIn(action, agent.ALLOWED_ACTIONS)
            self.assertTrue(agent.TOOLS_DOC.get(action))

    def test_planner_accepts_a_trading_quote_plan(self):
        plan = parse_plan('{"action":"trading_quote","title":"q","query_or_url_or_command":"binance::BTC/USDT","reason":""}')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.action, "trading_quote")
        self.assertEqual(plan.query, "binance::BTC/USDT")

    def test_api_tool_catalogue_lists_the_trading_tools(self):
        ids = {tool["id"] for tool in agent_api.TOOLS}
        for action in ("trading_quote", "trading_positions", "trading_balances"):
            self.assertIn(action, ids)
        for tool in agent_api.TOOLS:
            self.assertTrue({"id", "name", "description", "icon"} <= set(tool))


class TradingSessionCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._real_local = agent.SessionLocal
        agent.SessionLocal = self.session

    def tearDown(self):
        agent.SessionLocal = self._real_local
        self.engine.dispose()

    def plan(self, action, query):
        return AgentPlan(action=action, title="t", query=query, reason="")


class DispatchTests(TradingSessionCase):
    async def test_quote_dispatch_formats_the_service_result(self):
        mock = AsyncMock(return_value=QUOTE)
        with patch.object(trading, "read_quote", mock):
            out = await agent.execute_tool(self.plan("trading_quote", "binance::BTC/USDT"))
        self.assertIn("BTC/USDT", out)
        self.assertIn("binance", out)
        self.assertIn("64000.5", out)
        self.assertIn("as of", out)
        mock.assert_awaited_once()
        _, kwargs = mock.await_args
        self.assertEqual(kwargs, {"symbol": "BTC/USDT", "exchange": "binance", "actor": "agent"})

    async def test_positions_dispatch_formats_the_service_result(self):
        mock = AsyncMock(return_value=POSITIONS)
        with patch.object(trading, "read_positions", mock):
            out = await agent.execute_tool(self.plan("trading_positions", "binance"))
        self.assertIn("ETH/USDT", out)
        self.assertIn("long", out)
        self.assertIn("as of", out)
        mock.assert_awaited_once()
        _, kwargs = mock.await_args
        self.assertEqual(kwargs, {"exchange": "binance", "actor": "agent"})

    async def test_balances_dispatch_formats_the_service_result(self):
        mock = AsyncMock(return_value=BALANCES)
        with patch.object(trading, "read_balances", mock):
            out = await agent.execute_tool(self.plan("trading_balances", ""))
        self.assertIn("USDT", out)
        self.assertIn("105", out)
        self.assertIn("as of", out)
        mock.assert_awaited_once()
        _, kwargs = mock.await_args
        self.assertEqual(kwargs, {"exchange": None, "actor": "agent"})

    async def test_kill_switch_refusal_is_reported_not_raised(self):
        refusal = trading.TradingError(
            503, "trading_disabled", "Trading is disabled by the operator's kill-switch."
        )
        with patch.object(trading, "read_quote", AsyncMock(side_effect=refusal)):
            out = await agent.execute_tool(self.plan("trading_quote", "binance::BTC/USDT"))
        self.assertIsInstance(out, str)
        self.assertIn("trading_disabled", out)
        self.assertIn("kill-switch", out)
        self.assertIn("refused", out)

    async def test_empty_quote_argument_never_calls_the_service(self):
        mock = AsyncMock(return_value=QUOTE)
        with patch.object(trading, "read_quote", mock):
            out = await agent.execute_tool(self.plan("trading_quote", "   "))
        self.assertIn("trading_quote error", out)
        self.assertIn("exchange::symbol", out)
        mock.assert_not_awaited()

    async def test_passing_the_exchange_prefix_without_a_symbol_is_an_error(self):
        mock = AsyncMock(return_value=QUOTE)
        with patch.object(trading, "read_quote", mock):
            out = await agent.tool_trading_quote("binance::")
        self.assertIn("trading_quote error", out)
        mock.assert_not_awaited()


class DirectToolTests(TradingSessionCase):
    async def test_quote_tool_accepts_exchange_and_symbol(self):
        mock = AsyncMock(return_value=QUOTE)
        with patch.object(trading, "read_quote", mock):
            out = await agent.tool_trading_quote("binance::BTC/USDT")
        self.assertIn("BTC/USDT on binance", out)
        self.assertIn("bid: 64000.5", out)
        self.assertIn("ask: 64001.5", out)
        self.assertIn("last: 64001", out)
        self.assertIn("as of 2026-01-01T00:00:00Z", out)
        self.assertEqual(mock.await_args.kwargs,
                         {"symbol": "BTC/USDT", "exchange": "binance", "actor": "agent"})

    async def test_quote_tool_accepts_a_bare_symbol(self):
        mock = AsyncMock(return_value=QUOTE)
        with patch.object(trading, "read_quote", mock):
            await agent.tool_trading_quote("  BTC/USDT  ")
        self.assertEqual(mock.await_args.kwargs,
                         {"symbol": "BTC/USDT", "exchange": None, "actor": "agent"})

    async def test_positions_tool_lists_every_field(self):
        mock = AsyncMock(return_value=POSITIONS)
        with patch.object(trading, "read_positions", mock):
            out = await agent.tool_trading_positions("binance")
        for needle in ("ETH/USDT", "long", "quantity 1.5", "entry_price 3000",
                       "mark_price 3100", "unrealized_pnl 150", "leverage 3",
                       "as of 2026-01-01T00:00:00Z"):
            self.assertIn(needle, out)
        self.assertEqual(mock.await_args.kwargs, {"exchange": "binance", "actor": "agent"})

    async def test_positions_tool_uses_the_first_half_of_an_exchange_prefix(self):
        mock = AsyncMock(return_value=POSITIONS)
        with patch.object(trading, "read_positions", mock):
            await agent.tool_trading_positions("binance::BTC/USDT")
        self.assertEqual(mock.await_args.kwargs, {"exchange": "binance", "actor": "agent"})

    async def test_balances_tool_lists_free_locked_and_total(self):
        mock = AsyncMock(return_value=BALANCES)
        with patch.object(trading, "read_balances", mock):
            out = await agent.tool_trading_balances("binance")
        for needle in ("USDT", "free 100", "locked 5", "total 105",
                       "as of 2026-01-01T00:00:00Z"):
            self.assertIn(needle, out)
        self.assertEqual(mock.await_args.kwargs, {"exchange": "binance", "actor": "agent"})

    async def test_empty_results_are_stated_honestly(self):
        with patch.object(trading, "read_positions",
                          AsyncMock(return_value={"exchange": "binance", "as_of": "t", "positions": []})), \
                patch.object(trading, "read_balances",
                             AsyncMock(return_value={"exchange": "binance", "as_of": "t", "balances": []})):
            self.assertIn("(no open positions)", await agent.tool_trading_positions(""))
            self.assertIn("(no non-zero balances)", await agent.tool_trading_balances(""))

    async def test_an_unexpected_error_is_a_string_not_an_exception(self):
        with patch.object(trading, "read_quote", AsyncMock(side_effect=RuntimeError("boom"))):
            out = await agent.tool_trading_quote("binance::BTC/USDT")
        self.assertIn("trading_quote failed: RuntimeError: boom", out)

    async def test_refusals_are_reported_for_positions_and_balances(self):
        refusal = trading.TradingError(503, "trading_disabled", "Trading is disabled by the operator's kill-switch.")
        with patch.object(trading, "read_positions", AsyncMock(side_effect=refusal)):
            pos = await agent.tool_trading_positions("binance")
        with patch.object(trading, "read_balances", AsyncMock(side_effect=refusal)):
            bal = await agent.tool_trading_balances("binance")
        self.assertIn("trading_positions refused: trading_disabled", pos)
        self.assertIn("trading_balances refused: trading_disabled", bal)


class HelperTests(unittest.TestCase):
    def test_split_handles_both_forms(self):
        self.assertEqual(agent._trading_split("binance::BTC/USDT"), ("binance", "BTC/USDT"))
        self.assertEqual(agent._trading_split(" BTC/USDT "), (None, "BTC/USDT"))
        self.assertEqual(agent._trading_split("::BTC/USDT"), (None, "BTC/USDT"))
        self.assertEqual(agent._trading_split("binance::"), ("binance", ""))
        self.assertEqual(agent._trading_split(""), (None, ""))

    def test_amount_formatting(self):
        self.assertEqual(agent._fmt_amount(None), "n/a")
        self.assertEqual(agent._fmt_amount(1.5), "1.5")
        self.assertEqual(agent._fmt_amount(3000), "3000")
        self.assertEqual(agent._fmt_amount("105"), "105")
        self.assertEqual(agent._fmt_amount("nope"), "nope")


if __name__ == "__main__":
    unittest.main()
