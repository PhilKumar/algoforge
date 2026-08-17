"""The seam between the ladder and the adjudicated trendline+fib geometry.

Phil merged Fib Boundary and Fib Space on 2026-08-15: one geometry, one engine,
and a switch for what it buys. This module carries the two things the ladder
cannot get from SpaceGeometry on its own -- bar numbering with the session gap
rule, and a stack of fibs whose levels all rest at once.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.fib_ladder_geometry import LadderGeometry  # noqa: E402
from engine.fib_space_geometry import DrawnFib  # noqa: E402


class _Candle:
    """A plain candle, the shape the ladder is fed."""

    def __init__(self, timestamp, open_, high, low, close):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close


LEVELS = (2, 3, 4, 6, 8, 12, 16)


class BarConversionTests(unittest.TestCase):
    """Slopes are measured in BAR INDEX, and a session's first bar carries
    yesterday's close -- without either, the lines this engine draws are not
    the lines Phil drew."""

    def test_bars_are_numbered_from_zero(self):
        g = LadderGeometry(LEVELS)
        base = datetime(2026, 2, 26, 9, 15)
        for i in range(4):
            g.on_bar(_Candle(base + timedelta(minutes=15 * i), 100, 101, 99, 100), is_mother=(i == 0))
        self.assertEqual([b.index for b in g._bars], [0, 1, 2, 3])

    def test_only_a_session_opener_carries_yesterdays_close(self):
        g = LadderGeometry(LEVELS)
        day1 = datetime(2026, 2, 26, 15, 0)
        day2 = datetime(2026, 2, 27, 9, 15)
        g.on_bar(_Candle(day1, 100, 101, 99, 100.5), is_mother=True)
        g.on_bar(_Candle(day1 + timedelta(minutes=15), 100.5, 101, 99, 100), is_mother=False)
        g.on_bar(_Candle(day2, 97, 98, 96, 97.5), is_mother=False)
        self.assertIsNone(g._bars[0].session_prev_close, "the very first bar has no yesterday")
        self.assertIsNone(g._bars[1].session_prev_close, "same session, nothing carried")
        self.assertEqual(g._bars[2].session_prev_close, 100.0, "new day carries the prior close")

    def test_a_gap_down_that_closed_above_its_open_is_still_red(self):
        """The whole reason session_prev_close exists. Yesterday closed 100,
        today opened 97 and closed 97.5 -- green on its own open, red against
        the close the move actually began at, which is what the eye reads."""
        g = LadderGeometry(LEVELS)
        day1 = datetime(2026, 2, 26, 15, 0)
        g.on_bar(_Candle(day1, 100, 101, 99, 100.0), is_mother=True)
        g.on_bar(_Candle(datetime(2026, 2, 27, 9, 15), 97, 98, 96, 97.5), is_mother=False)
        opener = g._bars[1]
        self.assertTrue(opener.close > opener.open, "green on its own open")
        self.assertTrue(opener.is_red, "and red where it matters")

    def test_nothing_is_drawn_before_the_mother_is_seen(self):
        g = LadderGeometry(LEVELS)
        base = datetime(2026, 2, 26, 9, 15)
        for i in range(3):
            g.on_bar(_Candle(base + timedelta(minutes=15 * i), 100, 101, 99, 100), is_mother=False)
        self.assertEqual(g.fibs, [])
        self.assertEqual(g.trendlines, [])
        self.assertIsNone(g.mother_high)


class StackedLevelTests(unittest.TestCase):
    """Phil, 2026-08-15: a new fib ADDS its levels and the old ones keep
    resting. So a rung is (fib, level) -- two fibs both have an L4 and they are
    different prices and different money."""

    def _geometry_with(self, *fibs):
        g = LadderGeometry(LEVELS)
        g.on_bar(_Candle(datetime(2026, 2, 26, 9, 15), 100, 101, 99, 100), is_mother=True)
        g._geometry.fibs.extend(fibs)
        return g

    def _fib(self, fib_id, fib0, fib1, when):
        return DrawnFib(
            fib_id=fib_id,
            trendline_id=1,
            fib0=fib0,
            fib1=fib1,
            touch_index=0,
            touch_timestamp=when,
            drawn_index=1,
            drawn_timestamp=when,
        )

    def test_two_fibs_give_two_full_ladders(self):
        when = datetime(2026, 2, 27, 9, 15)
        g = self._geometry_with(self._fib(1, 25527.15, 25400.95, when), self._fib(2, 25000.0, 24900.0, when))
        levels = g.all_levels()
        self.assertEqual(len(levels), 14, "7 levels on each of 2 fibs")
        self.assertEqual(len({row.key for row in levels}), 14, "every rung is uniquely named")
        self.assertIn("F1L4", {row.key for row in levels})
        self.assertIn("F2L4", {row.key for row in levels})

    def test_each_level_is_n_spans_below_its_own_fib(self):
        when = datetime(2026, 2, 27, 9, 15)
        g = self._geometry_with(self._fib(1, 25527.15, 25400.95, when))
        by_key = {row.key: row.price for row in g.all_levels()}
        span = 25527.15 - 25400.95
        for level in LEVELS:
            self.assertAlmostEqual(by_key[f"F1L{level}"], 25527.15 - level * span, places=6)
        # The two ends Phil can check by eye on a chart.
        self.assertAlmostEqual(by_key["F1L2"], 25274.75, places=2)
        self.assertAlmostEqual(by_key["F1L16"], 23507.95, places=2)

    def test_levels_come_back_deepest_last(self):
        """A walk down the list is a walk down the chart, which is the order
        money is committed in."""
        when = datetime(2026, 2, 27, 9, 15)
        g = self._geometry_with(self._fib(1, 25527.15, 25400.95, when), self._fib(2, 25000.0, 24900.0, when))
        prices = [row.price for row in g.all_levels()]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_a_level_remembers_when_its_fib_was_drawn(self):
        """Nothing may trade a level before its fib existed -- buying earlier
        reads the future, which is what the old anchor's confirmed_at guarded."""
        when = datetime(2026, 2, 27, 11, 30)
        g = self._geometry_with(self._fib(1, 25527.15, 25400.95, when))
        self.assertTrue(all(row.drawn_at == when for row in g.all_levels()))

    def test_no_fibs_means_no_levels(self):
        g = LadderGeometry(LEVELS)
        g.on_bar(_Candle(datetime(2026, 2, 26, 9, 15), 100, 101, 99, 100), is_mother=True)
        self.assertEqual(g.all_levels(), [])


class StructureTests(unittest.TestCase):
    """What the CHART is handed. It used to redraw the geometry for itself out
    of the retired single-swing finder, which since the merge is not what the
    ladder trades -- a stacked ladder drawn as one fib is a chart that quietly
    shows the wrong prices."""

    def _drawn(self):
        """A fall, a bounce, a trendline, and a second fib once the first
        structure's low breaks. Same shape the engine's own tests use."""
        g = LadderGeometry(LEVELS)
        rows = [
            (24_660, 24_780, 24_640, 24_642),
            (24_642, 24_644, 24_620, 24_622),
            (24_622, 24_624, 24_600, 24_602),
            (24_602, 24_612, 24_600, 24_610),
            (24_610, 24_620, 24_608, 24_618),
            (24_618, 24_650, 24_615, 24_645),
            (24_645, 24_700, 24_640, 24_695),
            (24_695, 24_698, 24_680, 24_682),
            (24_682, 24_684, 24_670, 24_672),
            (24_672, 24_674, 24_560, 24_570),
            (24_570, 24_640, 24_565, 24_635),
            (24_635, 24_690, 24_630, 24_640),
            (24_640, 24_642, 24_505, 24_510),
        ]
        base = datetime(2026, 8, 6, 9, 15)
        for i, (o, h, low, c) in enumerate(rows):
            g.on_bar(_Candle(base + timedelta(minutes=15 * i), o, h, low, c), is_mother=(i == 0))
        return g

    def test_every_drawn_fib_comes_back_with_its_own_ladder(self):
        s = self._drawn().structures()
        self.assertEqual([fib["fib_id"] for fib in s["fibs"]], [1, 2])
        self.assertEqual([fib["fib0"] for fib in s["fibs"]], [24_698.0, 24_690.0])
        self.assertEqual([fib["fib1"] for fib in s["fibs"]], [24_600.0, 24_560.0])
        for fib in s["fibs"]:
            self.assertEqual([row["level"] for row in fib["levels"]], list(LEVELS))
            self.assertAlmostEqual(fib["levels"][0]["price"], fib["fib0"] - 2 * fib["span"], places=2)

    def test_a_trendline_carries_both_ends_as_timestamps(self):
        """`Trendline` holds anchor1 as a BAR INDEX only -- the renderer needs a
        clock at both ends, and only the object that numbered the bars can
        resolve one."""
        s = self._drawn().structures()
        self.assertTrue(s["trendlines"])
        line = s["trendlines"][0]
        self.assertIsInstance(line["a1"]["t"], str)
        self.assertIsInstance(line["a2"]["t"], str)
        self.assertLess(line["a1"]["t"], line["a2"]["t"], "the line runs forwards in time")

    def test_exactly_one_trendline_is_the_standing_one(self):
        s = self._drawn().structures()
        self.assertEqual(sum(1 for line in s["trendlines"] if line["active"]), 1)

    def test_nothing_drawn_means_nothing_to_draw(self):
        g = LadderGeometry(LEVELS)
        self.assertEqual(g.structures(), {"fibs": [], "trendlines": []})
        g.on_bar(_Candle(datetime(2026, 2, 26, 9, 15), 100, 101, 99, 100), is_mother=True)
        self.assertEqual(g.structures(), {"fibs": [], "trendlines": []})


class ConfigurationTests(unittest.TestCase):
    def test_the_level_set_is_the_caller_s(self):
        g = LadderGeometry((1, 2, 4, 8))
        self.assertEqual(g.levels, (1, 2, 4, 8))

    def test_an_empty_level_set_is_refused(self):
        with self.assertRaises(ValueError):
            LadderGeometry(())


# NIFTY 5m, 14-Aug-2026, 14:00 mother -- the real bars, from Upstox.
# (open, high, low, close), one row per 5m candle from the mother on.
_AUG14_5M = [
    (24395.00, 24405.20, 24393.00, 24395.55),  # 14:00 MOTHER
    (24395.90, 24398.80, 24387.35, 24389.95),  # 14:05
    (24390.50, 24392.05, 24382.50, 24383.55),  # 14:10
    (24383.20, 24389.20, 24378.65, 24379.95),  # 14:15
    (24378.70, 24381.00, 24372.80, 24376.90),  # 14:20
    (24377.30, 24379.20, 24369.65, 24371.60),  # 14:25
    (24372.60, 24375.00, 24367.55, 24371.60),  # 14:30
    (24371.95, 24373.50, 24366.40, 24369.35),  # 14:35
    (24369.35, 24373.85, 24368.40, 24369.45),  # 14:40  low locks on this close
    (24370.00, 24374.15, 24364.75, 24366.80),  # 14:45  ultimate low 24,364.75
    (24365.65, 24383.40, 24365.65, 24382.40),  # 14:50
    (24380.90, 24384.40, 24373.40, 24374.60),  # 14:55  the swing high the TL touches
    (24374.25, 24382.25, 24370.70, 24375.45),  # 15:00
    (24375.75, 24377.45, 24368.25, 24371.05),  # 15:05
    (24371.15, 24375.20, 24354.45, 24354.45),  # 15:10  decisive break of the low
]


class InstrumentScaledSwingGateTests(unittest.TestCase):
    """The chop gate is measured in the instrument's own candles, not in a
    fraction of price.

    Phil's 14-Aug-2026 chart: trendline from the mother, touching the 14:55
    swing high; the low broke decisively at 15:10. Every part of the rule
    fired -- and the fib was thrown away, because the swing measured 19.65
    points and the crypto-sized MIN_FIB_RANGE_PCT demanded 24.4 (0.1% of
    24,384). A swing bigger than a whole 5m bar is not chop on a 5m chart.
    Phil, 2026-08-17: "Market has fallen so much and still we had not drawn
    a fib means it is ridiculous."
    """

    def _replay(self):
        g = LadderGeometry(LEVELS)
        base = datetime(2026, 8, 14, 14, 0)
        for i, (o, h, lo, c) in enumerate(_AUG14_5M):
            g.on_bar(_Candle(base + timedelta(minutes=5 * i), o, h, lo, c), is_mother=(i == 0))
        return g

    def test_the_trendline_touches_the_swing_high(self):
        g = self._replay()
        self.assertEqual(len(g.trendlines), 1)
        self.assertEqual(g.trendlines[0].anchor2_timestamp, datetime(2026, 8, 14, 14, 55))

    def test_the_fib_is_drawn_on_the_break_of_the_low(self):
        g = self._replay()
        self.assertEqual(len(g.fibs), 1, "the swing is a whole candle tall; it is a structure, not chop")
        fib = g.fibs[0]
        self.assertEqual(fib.drawn_timestamp, datetime(2026, 8, 14, 15, 10))
        self.assertAlmostEqual(fib.fib0, 24384.40, places=2)
        self.assertAlmostEqual(fib.fib1, 24364.75, places=2)

    def test_the_swing_is_smaller_than_the_old_price_gate(self):
        """Documents WHY this test exists: under 0.1%-of-price it fails."""
        fib = self._replay().fibs[0]
        self.assertLess(fib.span, fib.fib0 * 0.001)

    def test_a_swing_smaller_than_a_candle_is_still_chop(self):
        """The gate did not vanish -- it scaled. A wobble narrower than one
        candle on eight-point candles draws nothing that size."""
        g = LadderGeometry(LEVELS)
        base = datetime(2026, 8, 14, 14, 0)
        rows = [(100.0, 108.0, 100.0, 101.0)]  # mother, an 8-point bar
        px = 100.0
        for i in range(1, 14):
            px -= 0.4
            rows.append((px + 4, px + 8, px, px + 3.5 if i % 4 else px + 0.5))
        for i, (o, h, lo, c) in enumerate(rows):
            g.on_bar(_Candle(base + timedelta(minutes=5 * i), o, h, lo, c), is_mother=(i == 0))
        for fib in g.fibs:
            self.assertGreaterEqual(fib.span, 8.0 * 0.99, "a fib narrower than one candle slipped through")


if __name__ == "__main__":
    unittest.main()
