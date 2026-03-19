import unittest

import pandas as pd

from engine.indicators import compute_dynamic_indicators
from engine.timeframes import resolve_strategy_timeframe


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


if __name__ == "__main__":
    unittest.main()
