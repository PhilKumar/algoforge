"""Folding a campaign chart must not invent, lose or reorder anything."""

import unittest
from datetime import datetime

from engine.fib_space_chart_fold import fold_campaign_chart, foldable_timeframes


def _epoch(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi).timestamp())


def _chart(candles, timeframe="15m"):
    return {
        "status": "ok",
        "timeframe": timeframe,
        "candles": candles,
        "trendlines": [{"id": 1, "a1": {"t": 10, "p": 100.0}, "a2": {"t": 20, "p": 90.0}}],
        "legs": [{"leg_id": 1, "levels": {"0": 100.0}}],
        "entries": [{"t": 15, "p": 95.0}],
    }


def _bars(n, start_minute=15, day=3):
    """`n` consecutive 15m bars from 09:15 on the given day."""
    out = []
    for i in range(n):
        minute = start_minute + 15 * i
        when = _epoch(2026, 8, day, 9 + minute // 60, minute % 60)
        out.append({"t": when, "o": 100 + i, "h": 110 + i, "l": 90 + i, "c": 105 + i, "is_mother": i == 0})
    return out


class FoldableTimeframeTests(unittest.TestCase):
    def test_a_chart_only_folds_upward(self):
        offered = foldable_timeframes("15m")
        self.assertEqual(offered[0], "15m")
        self.assertNotIn("5m", offered)  # inventing finer bars is not folding
        self.assertIn("1h", offered)
        self.assertIn("1d", offered)

    def test_only_whole_multiples_are_offered(self):
        # 4 x 15m makes an hour; nothing divides 15 into 30 unevenly either.
        self.assertIn("30m", foldable_timeframes("15m"))
        # A 30m base cannot make a 4h bar from 8 bars? It can — 240 % 30 == 0.
        self.assertIn("4h", foldable_timeframes("30m"))

    def test_a_daily_chart_has_nothing_coarser_on_offer(self):
        self.assertEqual(foldable_timeframes("1d"), ["1d"])


class FoldTests(unittest.TestCase):
    def test_four_fifteens_make_one_hour(self):
        chart = _chart(_bars(4))
        out = fold_campaign_chart(chart, "1h")
        self.assertEqual(len(out["candles"]), 1)
        bar = out["candles"][0]
        self.assertEqual(bar["o"], 100)  # first open
        self.assertEqual(bar["c"], 108)  # last close
        self.assertEqual(bar["h"], 113)  # highest high
        self.assertEqual(bar["l"], 90)  # lowest low

    def test_the_mother_survives_the_fold(self):
        out = fold_campaign_chart(_chart(_bars(4)), "1h")
        self.assertTrue(out["candles"][0]["is_mother"])

    def test_geometry_is_left_exactly_alone(self):
        """Trendlines and fibs are time/price, so folding must not touch them."""
        chart = _chart(_bars(8))
        out = fold_campaign_chart(chart, "1h")
        self.assertEqual(out["trendlines"], chart["trendlines"])
        self.assertEqual(out["legs"], chart["legs"])
        self.assertEqual(out["entries"], chart["entries"])

    def test_a_fold_never_crosses_a_day(self):
        chart = _chart(_bars(2, day=3) + _bars(2, day=4))
        out = fold_campaign_chart(chart, "1d")
        self.assertEqual(len(out["candles"]), 2)

    def test_the_input_payload_is_not_mutated(self):
        chart = _chart(_bars(4))
        before = list(chart["candles"])
        fold_campaign_chart(chart, "1h")
        self.assertEqual(chart["candles"], before)
        self.assertEqual(chart["timeframe"], "15m")

    def test_asking_for_the_base_is_a_no_op(self):
        chart = _chart(_bars(4))
        self.assertIs(fold_campaign_chart(chart, "15m"), chart)

    def test_folding_down_is_refused(self):
        with self.assertRaises(ValueError):
            fold_campaign_chart(_chart(_bars(4)), "5m")

    def test_bars_stay_in_time_order(self):
        out = fold_campaign_chart(_chart(_bars(12)), "1h")
        stamps = [b["t"] for b in out["candles"]]
        self.assertEqual(stamps, sorted(stamps))


if __name__ == "__main__":
    unittest.main()
