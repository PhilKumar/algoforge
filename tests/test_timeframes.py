import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd

from engine.indicators import compute_dynamic_indicators
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


class DummyBroker:
    pass


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


if __name__ == "__main__":
    unittest.main()
