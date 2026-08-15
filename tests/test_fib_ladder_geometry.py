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


class ConfigurationTests(unittest.TestCase):
    def test_the_level_set_is_the_caller_s(self):
        g = LadderGeometry((1, 2, 4, 8))
        self.assertEqual(g.levels, (1, 2, 4, 8))

    def test_an_empty_level_set_is_refused(self):
        with self.assertRaises(ValueError):
            LadderGeometry(())


if __name__ == "__main__":
    unittest.main()
