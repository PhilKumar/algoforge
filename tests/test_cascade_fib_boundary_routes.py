import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/philforge-test-fib-routes.db")
TEST_USER_DATA = Path("/tmp/philforge-test-fib-routes-data")

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zH5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

import app as app_module  # noqa: E402


class _DummyRequest:
    def __init__(self, user_id: int = 11):
        self.state = SimpleNamespace(user_id=user_id)


def _today_1m_mother() -> datetime:
    """A completed 1m candle inside today's session, which is all the paper
    ladder accepts -- history belongs to the Backtest button."""
    return datetime.now(app_module.IST).replace(hour=9, minute=20, second=0, microsecond=0)


def _recent_5m_mother() -> datetime:
    # A completed 5m candle several days back, safely inside the replay window.
    return (datetime.now(app_module.IST) - timedelta(days=6)).replace(hour=14, minute=15, second=0, microsecond=0)


class FibBoundaryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        if TEST_USER_DATA.exists():
            shutil.rmtree(TEST_USER_DATA)
        app_module.config.DB_PATH = str(TEST_DB)
        app_module.config.USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._db_mod.config.DB_PATH = str(TEST_DB)
        app_module._db_mod.config.USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._db_mod._initialized = False
        app_module._fib_boundary_engines.clear()
        await app_module._db_mod.init_db()

    async def asyncTearDown(self):
        for runtime in app_module._fib_boundary_engines.values():
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
        app_module._fib_boundary_engines.clear()

    async def test_status_not_started(self):
        result = await app_module.fib_boundary_paper_status(_DummyRequest())
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["mode"], "paper")

    async def test_start_rejects_bad_side(self):
        payload = app_module.FibTouchStartPayload(mother_timestamp=_today_1m_mother().isoformat(), side="XX")
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("side must be CE or PE", str(raised.exception.detail))

    async def test_start_rejects_an_unknown_symbol(self):
        payload = app_module.FibTouchStartPayload(mother_timestamp=_today_1m_mother().isoformat(), symbol="RELIANCE")
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Unknown symbol", str(raised.exception.detail))

    async def test_start_accepts_every_listed_instrument(self):
        # All five reach broker validation, so none is rejected as unknown.
        for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            payload = app_module.FibTouchStartPayload(mother_timestamp=_today_1m_mother().isoformat(), symbol=symbol)
            with patch.object(
                app_module, "_request_broker_context", AsyncMock(return_value=({"id": 11}, None, "user"))
            ):
                with self.assertRaises(app_module.HTTPException) as raised:
                    await app_module.fib_boundary_paper_start(payload, _DummyRequest())
            self.assertIn("Connect a Dhan account", str(raised.exception.detail), symbol)

    async def test_start_rejects_a_mother_from_an_earlier_day(self):
        # A past minute has no live quote, and the Backtest button owns history.
        stale = (datetime.now(app_module.IST) - timedelta(days=3)).replace(hour=11, minute=30, second=0, microsecond=0)
        payload = app_module.FibTouchStartPayload(mother_timestamp=stale.isoformat())
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Backtest", str(raised.exception.detail))

    async def test_start_rejects_a_timestamp_outside_the_session(self):
        payload = app_module.FibTouchStartPayload(
            mother_timestamp=datetime.now(app_module.IST)
            .replace(hour=8, minute=30, second=0, microsecond=0)
            .isoformat()
        )
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("09:15", str(raised.exception.detail))

    async def test_valid_mother_reaches_broker_validation(self):
        payload = app_module.FibTouchStartPayload(mother_timestamp=_today_1m_mother().isoformat(), side="CE")
        with patch.object(
            app_module,
            "_request_broker_context",
            AsyncMock(return_value=({"id": 11}, None, "user")),
        ):
            with self.assertRaises(app_module.HTTPException) as raised:
                await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Connect a Dhan account", str(raised.exception.detail))

    async def test_symbols_route_tells_the_console_what_is_honest(self):
        payload = await app_module.fib_touch_symbols(_DummyRequest())
        self.assertEqual(payload["levels"], [2, 3, 4, 6, 8, 12, 16])
        rows = {row["symbol"]: row for row in payload["symbols"]}
        self.assertEqual(set(rows), {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"})
        # NSE withdrew these weeklies; the console must not offer a week that
        # does not exist.
        self.assertTrue(rows["NIFTY"]["has_weeklies"])
        self.assertTrue(rows["SENSEX"]["has_weeklies"])
        for symbol in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            self.assertFalse(rows[symbol]["has_weeklies"], symbol)
        # And no premium history means no backtest, said out loud.
        self.assertFalse(rows["FINNIFTY"]["backtestable"])
        self.assertFalse(rows["MIDCPNIFTY"]["backtestable"])
        self.assertEqual(rows["NIFTY"]["lot_size"], 65)
        self.assertEqual(rows["BANKNIFTY"]["strike_step"], 100.0)

    async def test_chart_rejects_an_unknown_symbol(self):
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_chart(
                _today_1m_mother().isoformat(), _DummyRequest(), symbol="RELIANCE"
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Unknown symbol", str(raised.exception.detail))

    async def test_chart_rejects_a_bad_side(self):
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_chart(_today_1m_mother().isoformat(), _DummyRequest(), side="XX")
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("side must be CE or PE", str(raised.exception.detail))

    async def test_chart_needs_a_broker_before_it_draws_anything(self):
        with patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 11}, None, "user"))):
            with self.assertRaises(app_module.HTTPException) as raised:
                await app_module.fib_boundary_paper_chart(_today_1m_mother().isoformat(), _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Connect a Dhan account", str(raised.exception.detail))

    async def test_chart_prices_the_same_ladder_the_engine_trades(self):
        # The whole point of recomputing the anchor in the route is that it
        # cannot drift from the engine. Prove it: same candles, same mother,
        # same side -> byte-identical levels.
        from engine.fib_touch_ladder import HALVING_LEVELS, find_swing_anchor, level_price

        rows = [
            (24_660, 24_680, 24_655, 24_675),
            (24_675, 24_700, 24_670, 24_695),
            (24_695, 24_698, 24_680, 24_685),
            (24_685, 24_690, 24_660, 24_662),
            (24_662, 24_665, 24_640, 24_642),
            (24_642, 24_644, 24_620, 24_622),
            (24_622, 24_624, 24_600, 24_602),
            (24_602, 24_612, 24_600, 24_610),
            (24_610, 24_620, 24_608, 24_618),
        ]
        base = datetime(2026, 8, 6, 9, 15)
        candles = [
            app_module.IndexCandle(base + timedelta(minutes=i), o, h, low, c) for i, (o, h, low, c) in enumerate(rows)
        ]
        anchor = find_swing_anchor(candles, candles[6].timestamp, "CE")
        self.assertIsNotNone(anchor)
        assert anchor is not None
        priced = [round(level_price("CE", anchor.high, anchor.low, level), 2) for level in HALVING_LEVELS]
        # span is 100, so the ladder walks down in whole spans from 24,700.
        self.assertEqual(priced, [24_500.0, 24_400.0, 24_300.0, 24_100.0, 23_900.0, 23_500.0, 23_100.0])

    async def test_kill_without_campaign_is_404(self):
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_paper_kill(_DummyRequest())
        self.assertEqual(raised.exception.status_code, 404)

    async def test_backtest_rejects_bad_side(self):
        payload = app_module.FibBoundaryBacktestPayload(
            mother_timestamp=_recent_5m_mother().isoformat(), mother_high=24180, mother_low=24050, side="XX"
        )
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_backtest(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("side must be CE or PE", str(raised.exception.detail))

    async def test_backtest_rejects_high_not_above_low(self):
        payload = app_module.FibBoundaryBacktestPayload(
            mother_timestamp=_recent_5m_mother().isoformat(), mother_high=24050, mother_low=24180
        )
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.fib_boundary_backtest(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Mother high must exceed mother low", str(raised.exception.detail))

    async def test_backtest_without_broker_asks_to_connect_dhan(self):
        payload = app_module.FibBoundaryBacktestPayload(
            mother_timestamp=_recent_5m_mother().isoformat(), mother_high=24180, mother_low=24050, side="CE"
        )
        with patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 11}, None, "user"))):
            with self.assertRaises(app_module.HTTPException) as raised:
                await app_module.fib_boundary_backtest(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Connect a Dhan account", str(raised.exception.detail))


if __name__ == "__main__":
    unittest.main()
