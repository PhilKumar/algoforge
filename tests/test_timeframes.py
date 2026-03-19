import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd

from engine.indicators import compute_dynamic_indicators, cpr, cpr_timeframe, yesterday_candle
from engine.live import LiveEngine
from engine.paper_trading import PaperTradingEngine
from engine.timeframes import drop_incomplete_candle, next_entry_ready_at, resolve_strategy_timeframe


def _make_ohlcv(start: str, closes: list[float], freq: str = "1min") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq=freq)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=index,
    )


class TimeframeRegressionTests(unittest.TestCase):
    def test_resolve_mixed_timeframe_uses_lowest_execution_frame(self):
        spec = resolve_strategy_timeframe(["EMA_5_3m", "SMA_20_5m"])
        self.assertEqual(spec.requested, 3)
        self.assertEqual(spec.fetch, 1)
        self.assertTrue(spec.mixed)
        self.assertEqual(spec.all_frames, (3, 5))

    def test_mixed_timeframe_alignment_uses_last_closed_higher_frame(self):
        raw = _make_ohlcv("2026-03-18 09:15", list(range(100, 110)))
        result = compute_dynamic_indicators(
            raw,
            ["SMA_1_3m", "SMA_1_5m"],
            default_timeframe_minutes=3,
            source_timeframe_minutes=1,
        )

        self.assertEqual(list(result.index.strftime("%H:%M")), ["09:15", "09:18", "09:21"])
        self.assertTrue(pd.isna(result.loc[pd.Timestamp("2026-03-18 09:15"), "SMA_1_5m"]))
        self.assertEqual(result.loc[pd.Timestamp("2026-03-18 09:18"), "SMA_1_5m"], 104)
        self.assertEqual(result.loc[pd.Timestamp("2026-03-18 09:21"), "SMA_1_5m"], 104)

    def test_drop_incomplete_candle_removes_forming_bar(self):
        raw = _make_ohlcv("2026-03-19 09:10", [100, 101], freq="5min")
        trimmed = drop_incomplete_candle(raw, 5, pd.Timestamp("2026-03-19 09:16").to_pydatetime())
        self.assertEqual(list(trimmed.index.strftime("%H:%M")), ["09:10"])


class CprRegressionTests(unittest.TestCase):
    def test_daily_cpr_normalizes_bc_below_tc(self):
        df = pd.DataFrame(
            {
                "open": [111.0, 112.0],
                "high": [120.0, 121.0],
                "low": [100.0, 101.0],
                "close": [105.0, 110.0],
            },
            index=pd.to_datetime(["2030-01-02", "2030-01-03"]),
        )

        result = cpr(df)
        row = result.loc[pd.Timestamp("2030-01-03")]

        self.assertLessEqual(row["bc"], row["tc"])
        self.assertAlmostEqual(float(row["bc"]), 106.6666666667, places=4)
        self.assertAlmostEqual(float(row["tc"]), 110.0, places=4)

    def test_higher_timeframe_cpr_normalizes_bc_below_tc(self):
        df = pd.DataFrame(
            {
                "open": [119.0, 116.0, 111.0, 107.0, 110.0, 111.0, 112.0, 113.0],
                "high": [120.0, 118.0, 116.0, 112.0, 114.0, 115.0, 116.0, 117.0],
                "low": [110.0, 108.0, 104.0, 100.0, 109.0, 110.0, 111.0, 112.0],
                "close": [118.0, 114.0, 109.0, 105.0, 111.0, 112.0, 113.0, 114.0],
            },
            index=pd.date_range("2030-01-02 08:00", periods=8, freq="1h"),
        )

        result = cpr_timeframe(df, timeframe="4H")
        row = result.loc[pd.Timestamp("2030-01-02 12:00")]

        self.assertLessEqual(row["bc"], row["tc"])
        self.assertAlmostEqual(float(row["bc"]), 106.6666666667, places=4)
        self.assertAlmostEqual(float(row["tc"]), 110.0, places=4)

    def test_intraday_cpr_does_not_leak_same_day_levels_into_last_historical_session(self):
        df = _make_ohlcv("2025-03-03 09:15", [100, 101, 102, 103], freq="5min")

        result = cpr(df)

        self.assertTrue(pd.isna(result.iloc[-1]["bc"]))
        self.assertTrue(pd.isna(result.iloc[-1]["tc"]))

    def test_higher_timeframe_cpr_does_not_leak_same_bar_levels_into_last_historical_bar(self):
        df = pd.DataFrame(
            {
                "open": [119.0, 116.0, 111.0, 107.0],
                "high": [120.0, 118.0, 116.0, 112.0],
                "low": [110.0, 108.0, 104.0, 100.0],
                "close": [118.0, 114.0, 109.0, 105.0],
            },
            index=pd.date_range("2025-03-03 08:00", periods=4, freq="1h"),
        )

        result = cpr_timeframe(df, timeframe="4H")

        self.assertTrue(pd.isna(result.iloc[-1]["bc"]))
        self.assertTrue(pd.isna(result.iloc[-1]["tc"]))


class YesterdayRegressionTests(unittest.TestCase):
    def test_yesterday_candle_does_not_leak_same_day_values_into_last_historical_session(self):
        df = _make_ohlcv("2025-03-03 09:15", [100, 101, 102, 103], freq="5min")

        result = yesterday_candle(df)

        self.assertTrue(pd.isna(result.iloc[-1]["yesterday_high"]))
        self.assertTrue(pd.isna(result.iloc[-1]["yesterday_low"]))
        self.assertTrue(pd.isna(result.iloc[-1]["yesterday_close"]))
        self.assertTrue(pd.isna(result.iloc[-1]["yesterday_open"]))


class DummyBroker:
    def __init__(
        self,
        funds: dict | None = None,
        option_ltp: float = 100.0,
        place_order_results: list[dict] | None = None,
        verify_results: list[dict] | None = None,
    ):
        self.funds = funds or {"availabelBalance": 0.0}
        self.option_ltp = option_ltp
        self.place_order_results = list(place_order_results or [])
        self.verify_results = list(verify_results or [])
        self.cancelled_orders = []
        self.placed_orders = []

    async def async_get_funds(self):
        return self.funds

    async def async_get_option_ltp(self, *args, **kwargs):
        return self.option_ltp

    def get_option_ltp(self, *args, **kwargs):
        return self.option_ltp

    async def async_place_option_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        if self.place_order_results:
            return self.place_order_results.pop(0)
        return {"orderId": f"ORD{len(self.placed_orders)}"}

    async def async_verify_order_fill(self, order_id: str, max_wait_sec: int = 15):
        if self.verify_results:
            return self.verify_results.pop(0)
        return {
            "order_id": order_id,
            "status": "FILLED",
            "filled_qty": 0,
            "avg_price": self.option_ltp,
            "message": "Order filled successfully",
        }

    async def async_cancel_order(self, order_id: str):
        self.cancelled_orders.append(order_id)
        return {"orderId": order_id, "status": "CANCELLED"}


class DummyFeed:
    def __init__(self, snapshot: pd.DataFrame):
        self.snapshot = snapshot

    def get_candle_snapshot(self, instrument_id: str, timeframe: int, include_current: bool = False) -> pd.DataFrame:
        return self.snapshot.copy()


class LivePendingEntryTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_pending_order_waits_until_next_candle_open_plus_one_second(self):
        engine = LiveEngine(dhan=DummyBroker(), run_id="timing-test")
        engine.strategy = {"timeframe_minutes": 5, "indicators": [], "max_trades_per_day": 1}
        signal_ts = pd.Timestamp("2026-03-19 09:15").to_pydatetime()
        engine._pending_order = {
            "signal_candle_time": signal_ts,
            "created_at": pd.Timestamp("2026-03-19 09:20:00").to_pydatetime(),
            "ready_at": next_entry_ready_at(signal_ts, 5),
            "row": pd.Series({"open": 100.0}),
            "attempts": 0,
            "retry_at": None,
        }
        engine._flush_pending_order = AsyncMock()

        with patch("engine.live._now_ist", return_value=pd.Timestamp("2026-03-19 09:20:00").to_pydatetime()):
            await engine._try_flush_pending_order()
        engine._flush_pending_order.assert_not_awaited()

        with patch("engine.live._now_ist", return_value=pd.Timestamp("2026-03-19 09:20:01").to_pydatetime()):
            await engine._try_flush_pending_order()
        engine._flush_pending_order.assert_awaited_once()


class PaperPendingEntryTimingTests(unittest.TestCase):
    def test_paper_pending_entry_arms_for_next_candle_open_plus_one_second(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="timing-test")
        engine.strategy = {"timeframe_minutes": 5, "indicators": []}
        signal_ts = pd.Timestamp("2026-03-19 09:15").to_pydatetime()
        latest_row = pd.Series({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5})

        engine._arm_pending_entry(signal_ts, latest_row)

        self.assertFalse(engine._pending_entry_is_ready(pd.Timestamp("2026-03-19 09:20:00").to_pydatetime()))
        self.assertTrue(engine._pending_entry_is_ready(pd.Timestamp("2026-03-19 09:20:01").to_pydatetime()))


class LiveTouchExitTests(unittest.TestCase):
    def test_live_touch_exit_uses_forming_execution_candle(self):
        engine = LiveEngine(dhan=DummyBroker(), run_id="touch-live")
        engine.strategy = {"instrument": "26000", "timeframe_minutes": 3, "indicators": []}
        engine.exit_conditions = [
            {"left": "current_high", "operator": "touches", "right": "number", "right_number_value": 104}
        ]
        engine.current_time = pd.Timestamp("2026-03-19 09:22:30").to_pydatetime()
        engine.current_spot = 102.0
        engine._ws_mode = True
        engine._feed = DummyFeed(
            pd.DataFrame(
                {
                    "open": [101.0, 102.0],
                    "high": [105.0, 103.0],
                    "low": [100.0, 101.0],
                    "close": [103.0, 102.0],
                    "volume": [100, 120],
                },
                index=pd.to_datetime(["2026-03-19 09:21:00", "2026-03-19 09:22:00"]),
            )
        )
        row = pd.Series(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 90},
            name=pd.Timestamp("2026-03-19 09:18:00"),
        )
        pos = {"transaction_type": "BUY", "entry_premium": 100.0, "peak_premium": 100.0, "lots": 1, "lot_size": 1}

        reason = engine._check_exit_conditions(pos, row, 100.0)

        self.assertEqual(reason, "TOUCH_EXIT")


class PaperTouchExitTests(unittest.TestCase):
    def test_paper_touch_exit_uses_latest_raw_snapshot_in_rest_mode(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="touch-paper")
        engine.strategy = {"instrument": "26000", "timeframe_minutes": 3, "indicators": []}
        engine.exit_conditions = [
            {"left": "current_high", "operator": "touches", "right": "number", "right_number_value": 104}
        ]
        engine.current_time = pd.Timestamp("2026-03-19 09:22:30").to_pydatetime()
        engine.current_spot = 102.0
        engine._latest_raw_candles = pd.DataFrame(
            {
                "open": [101.0, 102.0],
                "high": [105.0, 103.0],
                "low": [100.0, 101.0],
                "close": [103.0, 102.0],
                "volume": [100, 120],
            },
            index=pd.to_datetime(["2026-03-19 09:21:00", "2026-03-19 09:22:00"]),
        )
        row = pd.Series(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 90},
            name=pd.Timestamp("2026-03-19 09:18:00"),
        )
        position = {"transaction_type": "BUY", "entry_premium": 100.0, "peak_premium": 100.0, "lots": 1, "lot_size": 1}

        reason = engine._check_exit_conditions(position, row, 100.0)

        self.assertEqual(reason, "TOUCH_EXIT")


class PortfolioStrategyExitTests(unittest.TestCase):
    def test_live_strategy_exit_uses_combined_open_position_pnl(self):
        engine = LiveEngine(dhan=DummyBroker(), run_id="portfolio-live")
        engine.strat_sl_val = 90.0
        engine.positions = [
            {
                "status": "open",
                "transaction_type": "BUY",
                "entry_premium": 100.0,
                "current_premium": 50.0,
                "lots": 1,
                "lot_size": 1,
                "quantity": 1,
            },
            {
                "status": "open",
                "transaction_type": "BUY",
                "entry_premium": 100.0,
                "current_premium": 50.0,
                "lots": 1,
                "lot_size": 1,
                "quantity": 1,
            },
        ]

        self.assertEqual(engine._check_strategy_exit(), "STRATEGY_SL")

    def test_live_strategy_thresholds_use_combined_entry_notional(self):
        engine = LiveEngine(dhan=DummyBroker(), run_id="portfolio-live")
        engine._sl_pct = 10.0
        engine._tp_pct = 20.0
        engine._set_strategy_thresholds(
            [
                {"entry_premium": 100.0, "lots": 1, "lot_size": 50, "quantity": 50},
                {"entry_premium": 120.0, "lots": 1, "lot_size": 50, "quantity": 50},
            ]
        )

        self.assertEqual(engine.trade_entry_prem, 11000.0)
        self.assertEqual(engine.strat_sl_val, 1100.0)
        self.assertEqual(engine.strat_tp_val, 2200.0)

    def test_paper_strategy_exit_uses_combined_open_position_pnl(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="portfolio-paper-exit")
        engine.strat_tp_val = 90.0
        engine.positions = [
            {
                "status": "open",
                "transaction_type": "BUY",
                "entry_premium": 100.0,
                "current_premium": 145.0,
                "lots": 1,
                "lot_size": 1,
            },
            {
                "status": "open",
                "transaction_type": "BUY",
                "entry_premium": 100.0,
                "current_premium": 145.0,
                "lots": 1,
                "lot_size": 1,
            },
        ]

        self.assertEqual(engine._check_strategy_exit(), "STRATEGY_TP")

    def test_paper_strategy_close_uses_actual_exit_premium(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="portfolio-paper-close")
        engine._history_file = "/tmp/paper_history_portfolio-paper.json"
        engine.current_time = pd.Timestamp("2026-03-19 09:25").to_pydatetime()
        position = {
            "status": "open",
            "transaction_type": "BUY",
            "entry_premium": 100.0,
            "lots": 1,
            "lot_size": 1,
            "leg_num": 1,
        }
        engine.positions = [position]

        with patch.object(engine, "_save_trade_history"):
            engine._close_position(position, "STRATEGY_TP", 110.0)

        self.assertEqual(engine.closed_trades[-1]["pnl"], 10.0)


class CapitalGatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_capital_check_blocks_unaffordable_long_entry(self):
        engine = LiveEngine(dhan=DummyBroker({"availabelBalance": 1000.0}), run_id="capital-live")
        engine.strategy = {"instrument": "26000"}
        engine._enforce_capital = True
        engine._capital_buffer_pct = 0.0
        engine._sell_option_margin_per_lot = 100000.0

        allowed = await engine._can_enter_trade(
            [{"transaction_type": "BUY", "entry_premium": 30.0, "lots": 1, "quantity": 50, "lot_size": 50}]
        )

        self.assertFalse(allowed)
        self.assertEqual(engine.capital_rejections, 1)
        self.assertFalse(engine.last_capital_check["passed"])

    async def test_live_capital_check_uses_margin_for_short_option(self):
        engine = LiveEngine(dhan=DummyBroker({"availabelBalance": 90000.0}), run_id="capital-live")
        engine.strategy = {"instrument": "26000"}
        engine._enforce_capital = True
        engine._capital_buffer_pct = 0.0
        engine._sell_option_margin_per_lot = 100000.0

        allowed = await engine._can_enter_trade(
            [{"transaction_type": "SELL", "entry_premium": 10.0, "lots": 1, "quantity": 50, "lot_size": 50}]
        )

        self.assertFalse(allowed)
        self.assertEqual(engine.last_capital_check["required"], 100000.0)

    async def test_paper_capital_check_blocks_unaffordable_portfolio(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="capital-paper")
        engine.initial_capital = 1000.0
        engine._enforce_capital = True
        engine._capital_buffer_pct = 0.0
        engine._sell_option_margin_per_lot = 100000.0

        allowed = engine._can_enter_trade(
            [{"transaction_type": "BUY", "entry_premium": 30.0, "lots": 1, "lot_size": 50}]
        )

        self.assertFalse(allowed)
        self.assertEqual(engine.capital_rejections, 1)
        self.assertFalse(engine.last_capital_check["passed"])


class LiveOrderVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_entry_uses_verified_broker_fill_price(self):
        broker = DummyBroker(
            option_ltp=100.0,
            place_order_results=[{"orderId": "ENTRY1"}],
            verify_results=[{"status": "FILLED", "filled_qty": 50, "avg_price": 101.25}],
        )
        engine = LiveEngine(dhan=broker, run_id="verify-entry")
        engine.strategy = {
            "run_name": "Verify Entry",
            "instrument": "26000",
            "legs": [{"transaction_type": "BUY", "option_type": "CE", "strike_type": "atm", "lots": 1}],
        }
        engine.deploy_config = {"product_type": "MIS", "entry_order": "MARKET"}
        engine.current_spot = 22000.0
        engine.current_time = pd.Timestamp("2026-03-19 09:20:01").to_pydatetime()
        engine.session_date = engine.current_time.date()

        with (
            patch.object(engine, "_can_enter_trade", AsyncMock(return_value=True)),
            patch("engine.live.ScripMaster.resolve_expiry", return_value="2026-03-26"),
            patch("engine.live.ScripMaster.get_lot_size", return_value=50),
        ):
            await engine._enter_trade(pd.Series({"open": 22000.0}))

        self.assertTrue(engine.in_trade)
        self.assertEqual(len(engine.positions), 1)
        self.assertAlmostEqual(engine.positions[0]["entry_premium"], 101.25)
        self.assertTrue(engine.positions[0]["entry_fill_verified"])
        self.assertEqual(engine.last_order_verification["status"], "FILLED")

    async def test_live_entry_timeout_does_not_open_position(self):
        broker = DummyBroker(
            option_ltp=100.0,
            place_order_results=[{"orderId": "ENTRY1"}],
            verify_results=[{"status": "TIMEOUT", "filled_qty": 0, "avg_price": 0.0, "message": "pending"}],
        )
        engine = LiveEngine(dhan=broker, run_id="verify-entry-timeout")
        engine.strategy = {
            "run_name": "Verify Entry Timeout",
            "instrument": "26000",
            "legs": [{"transaction_type": "BUY", "option_type": "CE", "strike_type": "atm", "lots": 1}],
        }
        engine.deploy_config = {"product_type": "MIS", "entry_order": "MARKET"}
        engine.current_spot = 22000.0
        engine.current_time = pd.Timestamp("2026-03-19 09:20:01").to_pydatetime()
        engine.session_date = engine.current_time.date()

        with (
            patch.object(engine, "_can_enter_trade", AsyncMock(return_value=True)),
            patch("engine.live.ScripMaster.resolve_expiry", return_value="2026-03-26"),
            patch("engine.live.ScripMaster.get_lot_size", return_value=50),
        ):
            await engine._enter_trade(pd.Series({"open": 22000.0}))

        self.assertFalse(engine.in_trade)
        self.assertEqual(len(engine.positions), 0)
        self.assertEqual(engine.order_verification_failures, 1)
        self.assertIn("ENTRY1", broker.cancelled_orders)

    async def test_live_exit_uses_verified_broker_fill_price(self):
        broker = DummyBroker(
            place_order_results=[{"orderId": "EXIT1"}],
            verify_results=[{"status": "FILLED", "filled_qty": 50, "avg_price": 118.5}],
        )
        engine = LiveEngine(dhan=broker, run_id="verify-exit")
        engine.deploy_config = {"product_type": "MIS", "exit_order": "MARKET"}
        engine.current_time = pd.Timestamp("2026-03-19 09:25:00").to_pydatetime()
        position = {
            "leg_num": 1,
            "status": "open",
            "transaction_type": "BUY",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22000,
            "expiry": "2026-03-26",
            "entry_premium": 100.0,
            "current_premium": 120.0,
            "quantity": 50,
            "lots": 1,
            "lot_size": 50,
            "trading_symbol": "NIFTY 22000CE 2026-03-26",
        }
        engine.positions = [position]
        engine.in_trade = True

        await engine._exit_position(position, "EXIT_SIGNAL", 120.0)

        self.assertFalse(engine.in_trade)
        self.assertEqual(len(engine.positions), 0)
        self.assertAlmostEqual(engine.closed_trades[-1]["exit_premium"], 118.5)
        self.assertEqual(engine.last_order_verification["status"], "FILLED")

    async def test_live_exit_timeout_marks_position_for_retry(self):
        broker = DummyBroker(
            place_order_results=[{"orderId": "EXIT1"}],
            verify_results=[{"status": "TIMEOUT", "filled_qty": 0, "avg_price": 0.0, "message": "pending"}],
        )
        engine = LiveEngine(dhan=broker, run_id="verify-exit-timeout")
        engine.deploy_config = {"product_type": "MIS", "exit_order": "MARKET"}
        engine.current_time = pd.Timestamp("2026-03-19 09:25:00").to_pydatetime()
        position = {
            "leg_num": 1,
            "status": "open",
            "transaction_type": "BUY",
            "underlying": "NIFTY",
            "option_type": "CE",
            "strike": 22000,
            "expiry": "2026-03-26",
            "entry_premium": 100.0,
            "current_premium": 120.0,
            "quantity": 50,
            "lots": 1,
            "lot_size": 50,
            "trading_symbol": "NIFTY 22000CE 2026-03-26",
        }
        engine.positions = [position]
        engine.in_trade = True

        await engine._exit_position(position, "EXIT_SIGNAL", 120.0)

        self.assertTrue(engine.in_trade)
        self.assertEqual(len(engine.positions), 1)
        self.assertEqual(engine.positions[0]["_force_exit_reason"], "EXIT_SIGNAL")
        self.assertIn("EXIT1", broker.cancelled_orders)


class PaperExecutionRealismTests(unittest.TestCase):
    def test_paper_execution_costs_make_entry_and_exit_worse_for_longs(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="paper-realism")
        engine.configure(
            {
                "run_name": "Paper Realism",
                "instrument": "26000",
                "execution_profile": "manual",
                "spread_bps": 0.0,
                "entry_slippage_bps": 100.0,
                "exit_slippage_bps": 100.0,
            },
            [],
            [],
        )

        self.assertAlmostEqual(engine._apply_execution_costs(100.0, "BUY", "entry"), 101.0)
        self.assertAlmostEqual(engine._apply_execution_costs(110.0, "BUY", "exit"), 108.9)

    def test_paper_close_uses_adjusted_exit_fill_price(self):
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            engine = PaperTradingEngine(dhan=DummyBroker(), run_id="paper-realism-close")
        engine.configure(
            {
                "run_name": "Paper Realism Close",
                "instrument": "26000",
                "execution_profile": "manual",
                "spread_bps": 0.0,
                "entry_slippage_bps": 0.0,
                "exit_slippage_bps": 100.0,
            },
            [],
            [],
        )
        engine.current_time = pd.Timestamp("2026-03-19 09:25").to_pydatetime()
        position = {
            "status": "open",
            "transaction_type": "BUY",
            "entry_premium": 100.0,
            "quantity": 1,
            "lots": 1,
            "lot_size": 1,
            "leg_num": 1,
        }
        engine.positions = [position]

        with patch.object(engine, "_save_trade_history"):
            engine._close_position(position, "TARGET", 110.0)

        self.assertAlmostEqual(engine.closed_trades[-1]["exit_quote_premium"], 110.0)
        self.assertAlmostEqual(engine.closed_trades[-1]["exit_premium"], 108.9)
        self.assertAlmostEqual(engine.closed_trades[-1]["pnl"], 8.9)


if __name__ == "__main__":
    unittest.main()
