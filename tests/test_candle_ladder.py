import unittest
from datetime import datetime, timedelta

from engine.candle_ladder import (
    LadderCandle,
    LadderError,
    TwoRedLadder,
    ladder_from,
    order_events,
)

START = datetime(2026, 5, 19, 9, 15)


def bar(timeframe: str, offset: int, o: float, h: float, low: float, c: float) -> LadderCandle:
    """One bar, `offset` bars after the session open on that chart."""
    minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[timeframe]
    return LadderCandle(timeframe, START + timedelta(minutes=minutes * offset), o, h, low, c)


def a_ladder(mother: LadderCandle, stages, *, premium=50.0, lots=(1, 2, 3, 4), require_new_low=True):
    return TwoRedLadder(
        mother,
        stages=stages,
        strike_for=lambda _ts, price: (int(price // 50 * 50), "CE"),
        premium_lookup=lambda _ts, _strike, _side: premium,
        lot_size=65,
        lots=lots,
        require_new_low=require_new_low,
    )


class LadderShapeTests(unittest.TestCase):
    def test_a_one_minute_start_climbs_all_the_way_to_one_hour(self):
        self.assertEqual(ladder_from("1m", 4), ("1m", "5m", "15m", "1h"))

    def test_a_later_start_runs_out_of_chart_rather_than_inventing_one(self):
        # 15m has only 1h above it. Four lots do not conjure a 4h bar.
        self.assertEqual(ladder_from("15m", 4), ("15m", "1h"))
        self.assertEqual(ladder_from("1h", 4), ("1h",))

    def test_an_unknown_timeframe_is_refused(self):
        with self.assertRaises(LadderError):
            ladder_from("3m", 2)

    def test_events_are_ordered_by_when_each_bar_closed_finest_first(self):
        # Both close at 09:20. The 1m bar is the newer information.
        one_minute = bar("1m", 5, 100, 101, 99, 100)  # opens 09:20?  no: 09:20 close
        hour = LadderCandle("1h", START, 100, 101, 99, 100)  # closes 10:15
        ordered = order_events([hour, one_minute])
        self.assertEqual(ordered[0].timeframe, "1m")


class StopPlacementTests(unittest.TestCase):
    """Phil's rule: the stop sits on the FIRST of the two reds, not the second."""

    def _two_reds(self, ladder):
        # Opening close of 105 comes from the first bar; then 102, then 99.
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))  # red 1, closes 102
        ladder.on_candle(bar("1m", 3, 102, 102.5, 98, 99))  # red 2, closes 99 -> arm

    def test_the_stop_is_the_higher_of_the_two_red_closes(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        self._two_reds(ladder)
        self.assertEqual(ladder.stages[0].stop, 102.0)

    def test_a_bounce_to_the_second_red_close_does_not_buy(self):
        # Reaching 99 is not enough; the older engine would have filled here.
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        self._two_reds(ladder)
        ladder.on_candle(bar("1m", 4, 99, 101.5, 98.5, 101))
        self.assertEqual(ladder.fills, [])

    def test_a_recovery_to_the_first_red_close_buys(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        self._two_reds(ladder)
        ladder.on_candle(bar("1m", 4, 99, 103, 98.5, 102.5))
        self.assertEqual(len(ladder.fills), 1)
        self.assertEqual(ladder.fills[0].index_price, 102.0)
        self.assertEqual(ladder.fills[0].lots, 1)
        self.assertEqual(ladder.fills[0].quantity, 65)

    def test_a_third_red_trails_the_stop_down_one_red(self):
        # The rule stays "one red back from the newest", so a third red moves
        # the stop from red 1's close to red 2's close.
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        self._two_reds(ladder)
        ladder.on_candle(bar("1m", 4, 99, 99.5, 95, 96))  # red 3, closes 96
        self.assertEqual(ladder.stages[0].stop, 99.0)


class EscalationTests(unittest.TestCase):
    def test_a_fill_climbs_to_the_next_timeframe(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m", "5m"))
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("1m", 3, 102, 102.5, 98, 99))
        ladder.on_candle(bar("1m", 4, 99, 103, 98.5, 102.5))
        self.assertEqual(len(ladder.fills), 1)
        # Rung 1 is done; further 1m candles are ignored by the watch.
        self.assertEqual(ladder.active, 1)
        self.assertEqual(ladder.stages[1].timeframe, "5m")

    def test_the_marked_low_is_the_lowest_seen_when_the_rung_filled(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m", "5m"))
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("1m", 3, 102, 102.5, 98, 99))
        ladder.on_candle(bar("1m", 4, 99, 103, 97.25, 102.5))
        self.assertEqual(ladder.fills[0].marked_low, 97.25)

    def test_the_second_rung_waits_for_a_genuinely_new_low(self):
        # Without this gate the 5m rung fires on the same downswing that filled
        # the 1m rung, which is one trade bought twice rather than a ladder.
        mother = bar("5m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("5m", "15m"))
        ladder.on_candle(bar("5m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("5m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("5m", 3, 102, 102.5, 96, 99))
        ladder.on_candle(bar("5m", 4, 99, 103, 98.5, 102.5))  # rung 1 fills, low 96
        # Every high below 104 here, or the basket simply hits its target and
        # leaves before the ladder has any chance to climb.
        ladder.on_candle(bar("15m", 1, 102, 103, 99, 100))  # sets the 15m prior close
        ladder.on_candle(bar("15m", 2, 100, 101, 98, 99))  # red, but 98 > the marked 96
        ladder.on_candle(bar("15m", 3, 99, 100, 97, 98))  # red again, still above 96
        self.assertIsNone(ladder.stages[1].stop)

    def test_a_new_low_lets_the_second_rung_arm(self):
        mother = bar("5m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("5m", "15m"))
        ladder.on_candle(bar("5m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("5m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("5m", 3, 102, 102.5, 96, 99))
        ladder.on_candle(bar("5m", 4, 99, 103, 98.5, 102.5))
        ladder.on_candle(bar("15m", 1, 102, 103, 99, 100))  # sets the 15m prior close
        ladder.on_candle(bar("15m", 2, 100, 101, 94, 95))  # red, and a new low
        ladder.on_candle(bar("15m", 3, 95, 96, 90, 91))  # red 2 -> arms one red back
        self.assertEqual(ladder.stages[1].stop, 95.0)

    def test_the_gate_can_be_switched_off(self):
        mother = bar("5m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("5m", "15m"), require_new_low=False)
        ladder.on_candle(bar("5m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("5m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("5m", 3, 102, 102.5, 96, 99))
        ladder.on_candle(bar("5m", 4, 99, 103, 98.5, 102.5))
        ladder.on_candle(bar("15m", 1, 102, 103, 99, 100))
        ladder.on_candle(bar("15m", 2, 100, 101, 98, 99))  # red 1, no new low needed
        ladder.on_candle(bar("15m", 3, 99, 100, 97, 98))  # red 2 -> arms at red 1's close
        self.assertEqual(ladder.stages[1].stop, 99.0)

    def test_each_rung_is_bigger_than_the_last(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m", "5m"))
        self.assertEqual([stage.lots for stage in ladder.stages], [1, 2])


class ExitTests(unittest.TestCase):
    def _one_fill(self, premium=50.0):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",), premium=premium)
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("1m", 3, 102, 102.5, 98, 99))
        ladder.on_candle(bar("1m", 4, 99, 103, 98.5, 102.5))
        return ladder

    def test_the_target_is_a_quarter_of_the_way_back_to_the_mother_high(self):
        ladder = self._one_fill()
        # entry 102, mother high 110 -> 102 + 0.25 * 8 = 104
        self.assertEqual(ladder.target_index, 104.0)

    def test_reaching_the_target_closes_and_books_the_trade(self):
        ladder = self._one_fill()
        ladder.on_candle(bar("1m", 5, 102.5, 105, 102, 104.5))
        self.assertEqual(ladder.status, "CLOSED")
        self.assertEqual(ladder.exit_reason, "target")
        # Flat premium in and out, so the only movement is costs.
        self.assertLess(ladder.net_pnl, 0)
        self.assertEqual(ladder.gross_pnl, 0.0)

    def test_expiry_closes_an_unfinished_trade(self):
        ladder = self._one_fill()
        ladder.close_at_expiry(bar("1m", 9, 100, 100.5, 99, 100), 100.0)
        self.assertEqual(ladder.status, "EXPIRED")
        self.assertEqual(ladder.exit_reason, "expiry")

    def test_a_mother_that_never_set_up_expires_without_a_trade(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        ladder.close_at_expiry(bar("1m", 9, 100, 100.5, 99, 100), 100.0)
        self.assertEqual(ladder.status, "EXPIRED")
        self.assertEqual(ladder.fills, [])
        self.assertIsNone(ladder.net_pnl)

    def test_an_unpriced_leg_leaves_the_money_blank_rather_than_free(self):
        ladder = self._one_fill(premium=None)
        ladder.on_candle(bar("1m", 5, 102.5, 105, 102, 104.5))
        self.assertEqual(ladder.status, "CLOSED")
        self.assertIsNone(ladder.net_pnl)
        self.assertEqual(ladder.exit_reason, "target")


if __name__ == "__main__":
    unittest.main()
