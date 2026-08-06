import json
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


class _Broker:
    def place_option_order(self, *a, **k):
        return SimpleNamespace(order_id="DHAN-1")


class _StubAdapter:
    def get_ticker(self, _symbol):
        return {"last_price": 24_500}


def _live_ladder(symbol: str, broker, *, armed: bool = True):
    """A LIVE ladder that has already bought a rung, for save/restore and for
    proving one instrument's actions leave the others alone."""
    from datetime import date as _date

    from engine.fib_touch_ladder import FibTouchConfig, FibTouchLadder, LiveExecutor

    base = datetime(2026, 8, 6, 9, 15)
    rows = [
        # Mother high 24,780, above its own bounce -- a close past the mother
        # ends the campaign, so a fixture that must keep trading contains it.
        (24_660, 24_780, 24_640, 24_642),
        (24_642, 24_644, 24_620, 24_622),
        (24_622, 24_624, 24_600, 24_602),
        (24_602, 24_612, 24_600, 24_610),
        (24_610, 24_620, 24_608, 24_618),
        (24_618, 24_650, 24_615, 24_645),
        (24_645, 24_700, 24_640, 24_695),
        (24_695, 24_698, 24_680, 24_682),
        (24_682, 24_684, 24_670, 24_672),
        (24_672, 24_674, 24_495, 24_510),
    ]
    candles = [
        app_module.IndexCandle(base + timedelta(minutes=i), o, h, low, c) for i, (o, h, low, c) in enumerate(rows)
    ]
    engine = FibTouchLadder(
        FibTouchConfig(
            symbol=symbol,
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
        ),
        premium_lookup=lambda *a: 200.0,
        expiry_source=lambda on: [_date(2026, 8, 11)],
        executor=LiveExecutor(broker, symbol, armed=armed),
    )
    for candle in candles:
        engine.on_candle(candle)
    return engine


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
        for ladders in app_module._fib_boundary_engines.values():
            for runtime in ladders.values():
                if runtime.task and not runtime.task.done():
                    runtime.task.cancel()
        app_module._fib_boundary_engines.clear()

    async def test_status_not_started(self):
        result = await app_module.fib_boundary_paper_status(_DummyRequest())
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["mode"], "paper")
        # A LIST now, always present, so the console renders zero panels rather
        # than special-casing "no campaign".
        self.assertEqual(result["campaigns"], [])

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
            (24_660, 24_665, 24_640, 24_642),  # MOTHER
            (24_642, 24_644, 24_620, 24_622),
            (24_622, 24_624, 24_600, 24_602),  # low 24,600
            (24_602, 24_612, 24_600, 24_610),
            (24_610, 24_620, 24_608, 24_618),  # LOW frozen
            (24_618, 24_650, 24_615, 24_645),
            (24_645, 24_700, 24_640, 24_695),  # high 24,700
            (24_695, 24_698, 24_680, 24_682),
            (24_682, 24_684, 24_670, 24_672),  # HIGH frozen
        ]
        base = datetime(2026, 8, 6, 9, 15)
        candles = [
            app_module.IndexCandle(base + timedelta(minutes=i), o, h, low, c) for i, (o, h, low, c) in enumerate(rows)
        ]
        anchor = find_swing_anchor(candles, candles[0].timestamp, "CE")
        self.assertIsNotNone(anchor)
        assert anchor is not None
        # Both anchors come from the swing AFTER the mother, never its own high.
        self.assertEqual(anchor.high, 24_700.0)
        self.assertEqual(anchor.low, 24_600.0)
        priced = [round(level_price("CE", anchor.high, anchor.low, level), 2) for level in HALVING_LEVELS]
        # span is 100, so the ladder walks down in whole spans from 24,700.
        self.assertEqual(priced, [24_500.0, 24_400.0, 24_300.0, 24_100.0, 23_900.0, 23_500.0, 23_100.0])

    async def test_a_ladder_survives_a_restart_and_comes_back_unarmed(self):
        """The real save/restore path, not the engine's round-trip in isolation."""
        broker = _Broker()
        engine = _live_ladder("NIFTY", broker)
        self.assertTrue(engine.fills, "fixture should have bought a rung")
        self.assertTrue(engine.get_status()["armed"])

        runtime = app_module._CascadeRuntime(engine, None, broker, engine.history[-1].timestamp, running=True)
        app_module._fib_boundary_engines[11] = {"NIFTY": runtime}
        await app_module._save_fib_boundary_open_state(11, force=True)

        # The process "restarts": every in-memory ladder is gone.
        app_module._fib_boundary_engines.clear()
        app_module._fib_boundary_open_state_last_save.clear()

        revived = await app_module._restore_fib_boundary_open_state(11, broker, activate=False)
        self.assertEqual(set(revived), {"NIFTY"})
        status = revived["NIFTY"].engine.get_status()
        self.assertEqual(len(status["fills"]), len(engine.fills))
        self.assertEqual(status["deployed_inr"], engine.deployed_inr)
        self.assertEqual(status["anchor"]["high"], 24_700.0)
        self.assertEqual(status["anchor"]["low"], 24_600.0)
        # THE RULE: a restart is not a person deciding to trade real money.
        self.assertEqual(status["mode"], "live")
        self.assertFalse(status["armed"])

    async def test_a_restart_brings_back_every_ladder_and_arms_none_of_them(self):
        broker = _Broker()
        app_module._fib_boundary_engines[11] = {
            symbol: app_module._CascadeRuntime(
                engine := _live_ladder(symbol, broker), None, broker, engine.history[-1].timestamp, running=True
            )
            for symbol in ("NIFTY", "SENSEX")
        }
        await app_module._save_fib_boundary_open_state(11, force=True)

        app_module._fib_boundary_engines.clear()
        app_module._fib_boundary_open_state_last_save.clear()

        revived = await app_module._restore_fib_boundary_open_state(11, broker, activate=False)
        self.assertEqual(set(revived), {"NIFTY", "SENSEX"})
        for symbol, runtime in revived.items():
            status = runtime.engine.get_status()
            self.assertEqual(status["symbol"], symbol)
            self.assertTrue(status["fills"], symbol)
            self.assertFalse(status["armed"], symbol)

    async def test_a_legacy_single_snapshot_row_still_revives_its_ladder(self):
        """The row written before ladders were per-symbol held ONE campaign at
        the top level. A ladder in flight through the upgrade must not vanish."""
        broker = _Broker()
        engine = _live_ladder("NIFTY", broker)
        await app_module._db_mod.set_app_state(
            app_module._fib_boundary_open_state_key(11),
            json.dumps(
                {
                    "running": True,
                    "last_candle_timestamp": engine.history[-1].timestamp.isoformat(),
                    "engine": engine.to_dict(),
                },
                default=str,
            ),
        )
        app_module._fib_boundary_engines.clear()
        revived = await app_module._restore_fib_boundary_open_state(11, broker, activate=False)
        self.assertEqual(set(revived), {"NIFTY"})
        self.assertEqual(len(revived["NIFTY"].engine.get_status()["fills"]), len(engine.fills))

    async def test_a_snapshot_from_the_retired_engine_is_ignored_not_guessed_at(self):
        await app_module._db_mod.set_app_state(
            app_module._fib_boundary_open_state_key(11),
            json.dumps(
                {
                    "running": True,
                    "last_candle_timestamp": "2026-08-06T09:20:00+05:30",
                    "engine": {"timeframe": "5m", "rungs": []},
                }
            ),
        )
        app_module._fib_boundary_engines.clear()
        self.assertEqual(await app_module._restore_fib_boundary_open_state(11, object(), activate=False), {})

    async def test_one_bad_campaign_does_not_cost_the_user_the_good_one(self):
        broker = _Broker()
        engine = _live_ladder("NIFTY", broker)
        await app_module._db_mod.set_app_state(
            app_module._fib_boundary_open_state_key(11),
            json.dumps(
                {
                    "campaigns": [
                        {"running": True, "last_candle_timestamp": "nonsense", "engine": {"version": 1}},
                        {
                            "running": True,
                            "last_candle_timestamp": engine.history[-1].timestamp.isoformat(),
                            "engine": engine.to_dict(),
                        },
                    ]
                },
                default=str,
            ),
        )
        app_module._fib_boundary_engines.clear()
        revived = await app_module._restore_fib_boundary_open_state(11, broker, activate=False)
        self.assertEqual(set(revived), {"NIFTY"})

    async def test_two_symbols_run_side_by_side_and_are_killed_one_at_a_time(self):
        """The whole point of the change: a second instrument starts, and killing
        it leaves the first one holding its own basket."""
        broker = _Broker()
        app_module._fib_boundary_engines[11] = {
            symbol: app_module._CascadeRuntime(
                engine := _live_ladder(symbol, broker),
                _StubAdapter(),
                broker,
                engine.history[-1].timestamp,
                running=True,
            )
            for symbol in ("NIFTY", "SENSEX")
        }
        status = await app_module.fib_boundary_paper_status(_DummyRequest())
        self.assertEqual([row["symbol"] for row in status["campaigns"]], ["NIFTY", "SENSEX"])

        killed = await app_module.fib_boundary_paper_kill(_DummyRequest(), symbol="SENSEX")
        self.assertEqual(killed["campaign"]["symbol"], "SENSEX")
        ladders = app_module._fib_boundary_engines[11]
        self.assertFalse(ladders["SENSEX"].running)
        self.assertTrue(ladders["NIFTY"].running, "killing one ladder must not touch the other")
        # And the survivor keeps its own fills.
        self.assertTrue(ladders["NIFTY"].engine.get_status()["fills"])

    async def test_arming_one_ladder_does_not_arm_the_other(self):
        broker = _Broker()
        app_module._fib_boundary_engines[11] = {
            symbol: app_module._CascadeRuntime(
                engine := _live_ladder(symbol, broker, armed=False),
                _StubAdapter(),
                broker,
                engine.history[-1].timestamp,
                running=True,
            )
            for symbol in ("NIFTY", "SENSEX")
        }
        result = await app_module.fib_boundary_paper_arm(_DummyRequest(), symbol="NIFTY")
        self.assertEqual(result["status"], "armed")
        ladders = app_module._fib_boundary_engines[11]
        self.assertTrue(ladders["NIFTY"].engine.get_status()["armed"])
        self.assertFalse(ladders["SENSEX"].engine.get_status()["armed"], "arming is per instrument")

    async def test_the_same_instrument_twice_is_still_a_409(self):
        """THE rule the per-symbol keying rests on: a second instrument starts,
        a second mother on the SAME instrument does not."""
        broker = _Broker()
        engine = _live_ladder("NIFTY", broker)
        app_module._fib_boundary_engines[11] = {
            "NIFTY": app_module._CascadeRuntime(
                engine, _StubAdapter(), broker, engine.history[-1].timestamp, running=True
            )
        }
        payload = app_module.FibTouchStartPayload(mother_timestamp=_today_1m_mother().isoformat(), symbol="NIFTY")
        with patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 11}, broker, "user"))):
            with self.assertRaises(app_module.HTTPException) as raised:
                await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("NIFTY CE ladder is already running on this instrument", str(raised.exception.detail))

    async def test_a_different_instrument_is_never_blocked_by_a_running_one(self):
        """The point of the change. A running NIFTY ladder used to hold the only
        slot; SENSEX must now get past the conflict check entirely."""
        broker = _Broker()
        engine = _live_ladder("NIFTY", broker)
        app_module._fib_boundary_engines[11] = {
            "NIFTY": app_module._CascadeRuntime(
                engine, _StubAdapter(), broker, engine.history[-1].timestamp, running=True
            )
        }
        payload = app_module.FibTouchStartPayload(mother_timestamp=_today_1m_mother().isoformat(), symbol="SENSEX")
        with patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 11}, broker, "user"))):
            with self.assertRaises(Exception) as raised:
                await app_module.fib_boundary_paper_start(payload, _DummyRequest())
        # It fails LATER (this stub broker cannot serve candles) -- never at 409.
        self.assertNotEqual(getattr(raised.exception, "status_code", None), 409)

    async def test_kill_and_arm_reject_a_symbol_that_is_not_running(self):
        for route in (app_module.fib_boundary_paper_kill, app_module.fib_boundary_paper_arm):
            with self.assertRaises(app_module.HTTPException) as raised:
                await route(_DummyRequest(), symbol="SENSEX")
            self.assertEqual(raised.exception.status_code, 404)
            self.assertIn("SENSEX", str(raised.exception.detail))

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
