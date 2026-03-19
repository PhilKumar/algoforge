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
    pass


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
        engine = PaperTradingEngine(dhan=DummyBroker(), run_id="portfolio-paper")
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
        engine = PaperTradingEngine(dhan=DummyBroker(), run_id="portfolio-paper")
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

        engine._close_position(position, "STRATEGY_TP", 110.0)

        self.assertEqual(engine.closed_trades[-1]["pnl"], 10.0)


if __name__ == "__main__":
    unittest.main()
