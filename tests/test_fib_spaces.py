"""A boundary is where TWO fibs' levels converge -- pinned to Phil's charts.

Phil corrected an earlier reading ("zones inside one fib") in the strongest
terms: "I am explicitly telling it about 2 fibs converging levels not 1."  The
two TradingView screenshots he sent on 2026-08-01 are the fixtures; every level
below is read straight off his own labels, and the geometry reproduces them
exactly from level = fib0 - n x (fib0 - fib1).
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.fib_space_geometry import DrawnFib  # noqa: E402
from engine.fib_spaces import find_spaces, tradable_spaces  # noqa: E402


def _fib(fib_id: int, fib0: float, fib1: float) -> DrawnFib:
    at = datetime(2026, 6, 1, 9, 15)
    return DrawnFib(
        fib_id=fib_id,
        trendline_id=fib_id,
        fib0=fib0,
        fib1=fib1,
        touch_index=0,
        touch_timestamp=at,
        drawn_index=1,
        drawn_timestamp=at,
    )


# SHOT 1 -- 26 May to 8 Jun 2026.  Tight fib (range 40.75) and wide (144.55).
SHOT1_SMALL = _fib(1, 24_066.95, 24_026.20)
SHOT1_BIG = _fib(2, 24_002.80, 23_858.25)

# SHOT 2 -- 25 Jun to 3 Jul 2026.  Ranges 40.15 and 114.55.
SHOT2_A = _fib(1, 24_209.45, 24_169.30)
SHOT2_B = _fib(2, 24_120.00, 24_005.45)


class LevelArithmeticTests(unittest.TestCase):
    """The engine's level maths must reproduce Phil's own chart labels."""

    def test_shot_one_levels_match_the_chart(self):
        for level, expected in ((2, 23_985.45), (4, 23_903.95), (8, 23_740.95)):
            self.assertAlmostEqual(SHOT1_SMALL.level_price(level), expected, places=2)
        for level, expected in ((2, 23_713.70), (4, 23_424.60), (8, 22_846.40)):
            self.assertAlmostEqual(SHOT1_BIG.level_price(level), expected, places=2)

    def test_shot_two_levels_match_the_chart(self):
        for level, expected in ((2, 24_129.15), (4, 24_048.85), (8, 23_888.25)):
            self.assertAlmostEqual(SHOT2_A.level_price(level), expected, places=2)
        for level, expected in ((2, 23_890.90), (4, 23_661.80)):
            self.assertAlmostEqual(SHOT2_B.level_price(level), expected, places=2)


class SpaceDetectionTests(unittest.TestCase):
    def test_a_single_fib_makes_no_boundary(self):
        # The whole point of the correction: one fib's own levels are not a
        # boundary, however far apart they sit.
        self.assertEqual(find_spaces([SHOT1_SMALL]), [])

    def test_shot_one_finds_the_two_eight_convergence(self):
        spaces = find_spaces([SHOT1_SMALL, SHOT1_BIG])
        match = [s for s in spaces if round(s.top_price, 2) == 23_740.95]
        self.assertEqual(len(match), 1, spaces)
        space = match[0]
        self.assertAlmostEqual(space.bottom_price, 23_713.70, places=2)
        self.assertAlmostEqual(space.width, 27.25, places=2)
        self.assertAlmostEqual(space.midpoint, 23_727.325, places=2)
        self.assertEqual(space.label, "8-2")  # small fib's L8 over big fib's L2
        self.assertFalse(space.is_tiny)
        # "The first one is above 50% so that can be taken" -- the upper half
        # is the buy zone, the lower half is not.
        self.assertTrue(space.contains_buy(23_735.00))
        self.assertTrue(space.contains_buy(23_727.50))
        self.assertFalse(space.contains_buy(23_720.00))

    def test_shot_two_finds_the_tiny_space_and_buys_on_touch(self):
        spaces = find_spaces([SHOT2_A, SHOT2_B])
        match = [s for s in spaces if round(s.top_price, 2) == 23_890.90]
        self.assertEqual(len(match), 1, spaces)
        space = match[0]
        self.assertAlmostEqual(space.bottom_price, 23_888.25, places=2)
        self.assertAlmostEqual(space.width, 2.65, places=2)
        self.assertEqual(space.label, "2-8")  # B's L2 over A's L8
        # "One is very small.. so you can buy it on the touch of the 2 levels":
        # no usable middle, so the whole space is live.
        self.assertTrue(space.is_tiny)
        self.assertAlmostEqual(space.buy_floor, 23_888.25, places=2)
        self.assertTrue(space.contains_buy(23_888.30))

    def test_every_space_pairs_two_different_fibs(self):
        for fibs in ([SHOT1_SMALL, SHOT1_BIG], [SHOT2_A, SHOT2_B]):
            for space in find_spaces(fibs):
                self.assertNotEqual(space.top_fib_id, space.bottom_fib_id)

    def test_spaces_come_out_deepest_last(self):
        spaces = find_spaces([SHOT1_SMALL, SHOT1_BIG])
        prices = [s.top_price for s in spaces]
        self.assertEqual(prices, sorted(prices, reverse=True))


class TradableSelectionTests(unittest.TestCase):
    def test_shot_one_converges_once_and_that_single_space_trades(self):
        # Phil's 26 May chart crosses exactly once, and he said of it "The
        # first one is above 50% so that can be taken" -- so a lone space is
        # tradable.  This is why the rule is "deepest two", not "skip #1".
        spaces = find_spaces([SHOT1_SMALL, SHOT1_BIG])
        self.assertEqual(len(spaces), 1)
        self.assertEqual(tradable_spaces(spaces), spaces)

    def test_with_three_spaces_the_last_two_trade(self):
        spaces = find_spaces([SHOT2_A, SHOT2_B])
        self.assertEqual(len(spaces), 3)
        picked = tradable_spaces(spaces)
        self.assertEqual(picked, spaces[1:])
        self.assertNotIn(spaces[0], picked)
        # The 2.65-point convergence is one of the two that trade.
        self.assertIn(23_890.90, [round(s.top_price, 2) for s in picked])

    def test_spaces_already_above_the_market_are_not_offered(self):
        spaces = find_spaces([SHOT1_SMALL, SHOT1_BIG])
        picked = tradable_spaces(spaces, below=23_800.0)
        self.assertTrue(all(s.top_price <= 23_800.0 for s in picked))


if __name__ == "__main__":
    unittest.main()
