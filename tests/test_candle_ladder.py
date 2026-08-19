import unittest
from datetime import datetime, timedelta

from engine.candle_ladder import (
    LadderCandle,
    LadderError,
    TwoRedLadder,
    closed_at,
    ladder_from,
    order_events,
)

START = datetime(2026, 5, 19, 9, 15)


def bar(timeframe: str, offset: int, o: float, h: float, low: float, c: float) -> LadderCandle:
    """One bar, `offset` bars after the session open on that chart."""
    minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[timeframe]
    return LadderCandle(timeframe, START + timedelta(minutes=minutes * offset), o, h, low, c)


def a_ladder(mother: LadderCandle, stages, *, premium=50.0, lots=(1, 2, 3, 4), require_new_low=True, **kw):
    return TwoRedLadder(
        mother,
        stages=stages,
        strike_for=lambda _ts, price: (int(price // 50 * 50), "CE"),
        premium_lookup=lambda _ts, _strike, _side: premium,
        lot_size=65,
        lots=lots,
        require_new_low=require_new_low,
        **kw,
    )


class LadderShapeTests(unittest.TestCase):
    def test_a_one_minute_start_climbs_all_the_way_to_one_hour(self):
        self.assertEqual(ladder_from("1m", 4), ("1m", "5m", "15m", "1h"))

    def test_the_chain_runs_1m_to_1w_and_stops_there(self):
        # Phil, 2026-08-19: three layers from wherever it starts, and a 1H
        # mother climbs to the daily and the weekly.
        self.assertEqual(ladder_from("15m", 3), ("15m", "1h", "1d"))
        self.assertEqual(ladder_from("1h", 3), ("1h", "1d", "1w"))
        # Nothing above the weekly: a depth of four does not conjure a month.
        self.assertEqual(ladder_from("1h", 4), ("1h", "1d", "1w"))
        self.assertEqual(ladder_from("1w", 3), ("1w",))

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

    def test_greens_between_the_two_reds_change_nothing(self):
        """Phil, 2026-08-19: "Green is not the matter here. Any number of green
        candles can be between the 2 red candles. The thing is the price of the
        current candle has to be below the previous red candle close."
        """
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))  # red 1, closes 102
        ladder.on_candle(bar("1m", 3, 102, 103.5, 101.5, 103))  # green
        ladder.on_candle(bar("1m", 4, 103, 104, 102.5, 103.5))  # green
        self.assertEqual(len(ladder.stages[0].reds), 1)
        self.assertIsNone(ladder.stages[0].stop)
        ladder.on_candle(bar("1m", 5, 103.5, 103.5, 100, 101))  # red, below 102 -> arms at 102
        self.assertEqual(ladder.stages[0].stop, 102.0)

    def test_a_red_that_does_not_close_below_the_previous_red_is_ignored(self):
        # Not a step down, so it neither arms nor resets: the sequence waits.
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))  # red 1, closes 102
        ladder.on_candle(bar("1m", 3, 104, 104.5, 102.5, 103))  # red, but closes ABOVE 102
        self.assertEqual([row.close for row in ladder.stages[0].reds], [102.0])
        self.assertIsNone(ladder.stages[0].stop)
        ladder.on_candle(bar("1m", 4, 103, 103.2, 100, 101))  # red, below 102 -> arms at 102
        self.assertEqual(ladder.stages[0].stop, 102.0)

    def test_the_stop_is_always_above_the_market_when_it_arms(self):
        """The invariant the sequence buys us: reds[-2].close > reds[-1].close,
        so a buy-stop can never be placed BELOW the bar that armed it -- the
        10-Aug-2026 phantom fill (a stop at 24,573.55 with NIFTY at 24,591).
        """
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("1m", 3, 106, 106.5, 103, 104))  # a HIGHER red: ignored
        arming = bar("1m", 4, 104, 104.2, 100, 101)
        ladder.on_candle(arming)
        self.assertEqual(ladder.stages[0].stop, 102.0)
        self.assertGreater(ladder.stages[0].stop, arming.close)
        self.assertEqual(ladder.fills, [])  # the arming bar cannot take its own stop

    def test_a_resting_stop_survives_a_green_that_does_not_reach_it(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        self._two_reds(ladder)  # armed at 102
        ladder.on_candle(bar("1m", 4, 99, 100.5, 98.5, 100))  # green, high below the stop
        self.assertEqual(ladder.stages[0].stop, 102.0)  # the order is on the book
        ladder.on_candle(bar("1m", 5, 100, 102.5, 99.5, 102))  # reaches it
        self.assertEqual(len(ladder.fills), 1)
        self.assertEqual(ladder.fills[0].index_price, 102.0)

    def test_a_lower_red_after_a_green_trails_the_resting_stop_down(self):
        mother = bar("1m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("1m",))
        self._two_reds(ladder)  # armed at 102 (reds 102, 99)
        ladder.on_candle(bar("1m", 4, 99, 100.5, 98.5, 100))  # green, high below the stop
        self.assertEqual(ladder.stages[0].stop, 102.0)  # the order is on the book
        ladder.on_candle(bar("1m", 5, 100, 100.2, 96, 97))  # red, below 99 -> one red back is 99
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
        ladder.on_candle(bar("15m", 1, 102, 103, 99, 100))  # red 1, no new low needed
        ladder.on_candle(bar("15m", 2, 100, 101, 98, 99))  # red 2, lower -> arms at red 1's close
        self.assertEqual(ladder.stages[1].stop, 100.0)

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

    def test_the_exit_is_stamped_when_its_bar_closed(self):
        ladder = self._one_fill()
        ladder.on_candle(bar("1m", 5, 102.5, 105, 102, 104.5))
        # The bar opens at 09:20 and closes at 09:21; the exit is the close.
        self.assertEqual(ladder.exit_timestamp, START + timedelta(minutes=6))

    def test_a_slow_bar_spanning_the_buy_cannot_reach_the_target(self):
        """The bar that CONTAINS the buy carries price from before it.

        Found 2026-08-19 on a real losing trade: NIFTY made 23,390 at 09:15,
        the rung filled at 23,277 at 10:45, and the DAY bar -- whose high was
        that 09:15 print -- cleared the target and sold the basket at the
        day's close of 23,070, booking -Rs 6,598 and calling it "target".
        A target has to be made by price the basket was alive for, so a bar
        that started before the last fill is ignored; the NEXT one counts.
        """
        mother = bar("15m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("15m", "1h"))
        ladder.on_candle(bar("15m", 4, 105, 106, 104, 105))  # 10:15
        ladder.on_candle(bar("15m", 5, 105, 105.5, 101, 102))  # 10:30, red 1
        ladder.on_candle(bar("15m", 6, 102, 102.5, 98, 99))  # 10:45, red 2 -> arm
        ladder.on_candle(bar("15m", 7, 99, 103, 98.5, 102.5))  # 11:00, fills
        self.assertTrue(ladder.fills, "the 15m rung should have filled")
        fill = ladder.fills[0]
        # This hourly bar OPENS 10:15 -- before the 11:00 fill -- so its 105
        # high is part history and must not close the trade.
        stale_hour = LadderCandle("1h", START + timedelta(minutes=60), 100, 105, 98, 104.5)
        self.assertLess(stale_hour.timestamp, fill.timestamp)
        ladder.on_candle(stale_hour)
        self.assertIsNone(ladder.exit_reason, "a bar older than the buy closed the trade")

        # The next hourly bar is wholly after the fill, so it does close it --
        # and the exit is stamped at that bar's CLOSE, never its open (Phil,
        # 2026-08-14: a saved run read "bought 12:45, closed 12:15").
        fresh_hour = LadderCandle("1h", START + timedelta(minutes=120), 102, 105, 101, 104.5)
        ladder.on_candle(fresh_hour)
        self.assertEqual(ladder.exit_reason, "target")
        self.assertEqual(ladder.exit_timeframe, "1h")
        self.assertGreater(ladder.exit_timestamp, fill.timestamp)
        self.assertEqual(ladder.exit_timestamp, START + timedelta(minutes=180))


class TimeStopTests(unittest.TestCase):
    """A time stop is a rule about a position, not about a setup."""

    def test_the_clock_starts_at_the_first_buy_not_at_the_mother(self):
        mother = bar("15m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("15m",), hold_days=1)
        # Days pass with nothing bought -- the time stop must stay quiet.
        for day in range(1, 4):
            late = LadderCandle("15m", START + timedelta(days=day, hours=6), 104, 105, 103, 104)
            ladder.on_candle(late)
        self.assertIsNone(ladder.exit_reason, "an unbought setup was time-stopped")
        self.assertFalse(ladder.fills)

    def test_it_sells_a_day_after_the_buy(self):
        mother = bar("15m", 0, 100, 110, 99, 105)
        ladder = a_ladder(mother, ("15m",), hold_days=1)
        ladder.on_candle(bar("15m", 5, 105, 105.5, 101, 102))  # red 1
        ladder.on_candle(bar("15m", 6, 102, 102.5, 98, 99))  # red 2 -> arm at 102
        ladder.on_candle(bar("15m", 7, 99, 102.6, 98.5, 100))  # fills
        self.assertTrue(ladder.fills, "the rung should have filled")
        next_day = LadderCandle("15m", START + timedelta(days=1, hours=6), 99, 99.5, 98, 98.5)
        ladder.on_candle(next_day)
        self.assertEqual(ladder.exit_reason, "time_stop")


class PricingMomentTests(unittest.TestCase):
    """Every premium is read at the bar's CLOSE -- the moment the engine acts.

    That is when a paper campaign sees the bar and buys, so a backtest priced
    the same way agrees with it; and it is the only minute a 15m or 1H rung
    still has a live quote for. A kill is the one exception: it happens now.
    """

    def _ladder_recording_minutes(self, stages=("15m",), **kw):
        asked: list = []

        def premium(when, _strike, _side):
            asked.append(when)
            return 50.0

        mother = bar("15m", 0, 100, 110, 99, 105)
        ladder = TwoRedLadder(
            mother,
            stages=stages,
            strike_for=lambda _ts, price: (int(price // 50 * 50), "CE"),
            premium_lookup=premium,
            lot_size=65,
            **kw,
        )
        return ladder, asked

    def _fill_the_first_rung(self, ladder):
        ladder.on_candle(bar("15m", 4, 105, 106, 104, 105))  # 10:15
        ladder.on_candle(bar("15m", 5, 105, 105.5, 101, 102))  # 10:30, red 1
        ladder.on_candle(bar("15m", 6, 102, 102.5, 98, 99))  # 10:45, red 2 -> arm at 102
        ladder.on_candle(bar("15m", 7, 99, 103, 98.5, 102.5))  # 11:00, fills

    def test_a_fill_is_priced_at_the_bar_close_not_its_open(self):
        ladder, asked = self._ladder_recording_minutes()
        self._fill_the_first_rung(ladder)
        # The 11:00 bar closes at 11:15; the fill keeps its open for display.
        self.assertEqual(asked, [START + timedelta(minutes=120)])
        self.assertEqual(ladder.fills[0].timestamp, START + timedelta(minutes=105))
        self.assertEqual(ladder.fills[0].priced_at, START + timedelta(minutes=120))

    def test_a_target_exit_is_priced_when_its_bar_closed(self):
        ladder, asked = self._ladder_recording_minutes()
        self._fill_the_first_rung(ladder)
        ladder.on_candle(bar("15m", 8, 102.5, 105, 102, 104.5))  # 11:15 bar, closes 11:30
        self.assertEqual(ladder.exit_reason, "target")
        self.assertEqual(asked[-1], START + timedelta(minutes=135))
        self.assertEqual(ladder.exit_priced_at, ladder.exit_timestamp)

    def test_a_kill_is_priced_at_the_moment_of_the_kill(self):
        ladder, asked = self._ladder_recording_minutes()
        self._fill_the_first_rung(ladder)
        now = START + timedelta(minutes=123)
        ladder.kill(LadderCandle("15m", now, 101, 101, 101, 101), 101.0)
        self.assertEqual(ladder.status, "KILLED")
        self.assertEqual(asked[-1], now)

    def test_the_last_bar_of_the_day_closes_at_the_session_end(self):
        stub = LadderCandle("1h", START.replace(hour=15, minute=15), 100, 101, 99, 100)
        self.assertEqual(closed_at(stub), START.replace(hour=15, minute=30))
        last_minute = LadderCandle("1m", START.replace(hour=15, minute=29), 100, 101, 99, 100)
        self.assertEqual(closed_at(last_minute), START.replace(hour=15, minute=30))
        # An ordinary bar is untouched, and so is a daily bar (session-measured).
        self.assertEqual(closed_at(bar("1h", 1, 100, 101, 99, 100)), START + timedelta(minutes=120))
        self.assertEqual(closed_at(LadderCandle("1d", START, 1, 2, 0, 1)), START + timedelta(minutes=375))


class SessionEndTests(unittest.TestCase):
    """The expiry day, and the optional flat-by-3:15 rule, end the ladder."""

    def _ladder(self, **kw):
        mother = bar("1m", 0, 100, 110, 99, 105)
        return a_ladder(mother, ("1m",), **kw)

    def _one_fill(self, ladder):
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("1m", 3, 102, 102.5, 98, 99))
        ladder.on_candle(bar("1m", 4, 99, 103, 98.5, 102.5))
        self.assertEqual(len(ladder.fills), 1)

    def test_a_ladder_with_no_expiry_holds_through_the_close(self):
        ladder = self._ladder()
        self._one_fill(ladder)
        ladder.on_candle(LadderCandle("1m", START.replace(hour=15, minute=14), 100, 100.5, 99, 100))
        self.assertIsNone(ladder.exit_timestamp)
        self.assertEqual(ladder.status, "OPEN")

    def test_the_expiry_day_sells_the_basket_on_the_first_bar_closing_at_or_after_three_fifteen(self):
        ladder = self._ladder(expiry=START.date())
        self._one_fill(ladder)
        ladder.on_candle(LadderCandle("1m", START.replace(hour=15, minute=13), 100, 100.5, 99, 100))
        self.assertIsNone(ladder.exit_timestamp)  # closes 15:14, too early
        last = LadderCandle("1m", START.replace(hour=15, minute=14), 100, 100.5, 99, 100.25)
        ladder.on_candle(last)
        self.assertEqual(ladder.status, "EXPIRED")
        self.assertEqual(ladder.exit_reason, "expiry")
        self.assertEqual(ladder.exit_index_price, 100.25)  # sold at that bar's close
        self.assertEqual(ladder.exit_timestamp, START.replace(hour=15, minute=15))

    def test_a_setup_that_never_bought_expires_at_the_close_of_the_expiry_day(self):
        ladder = self._ladder(expiry=START.date())
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(LadderCandle("1m", START.replace(hour=15, minute=14), 100, 100.5, 99, 100))
        self.assertEqual(ladder.status, "EXPIRED")
        self.assertEqual(ladder.fills, [])
        self.assertEqual(ladder.events[-1]["event"], "session_ended_without_a_trade")

    def test_before_the_expiry_day_the_close_changes_nothing(self):
        ladder = self._ladder(expiry=START.date() + timedelta(days=7))
        self._one_fill(ladder)
        ladder.on_candle(LadderCandle("1m", START.replace(hour=15, minute=14), 100, 100.5, 99, 100))
        self.assertIsNone(ladder.exit_timestamp)

    def test_intraday_close_sells_at_three_fifteen_and_ends_the_campaign(self):
        ladder = self._ladder(intraday_close=True, expiry=START.date() + timedelta(days=30))
        self._one_fill(ladder)
        ladder.on_candle(LadderCandle("1m", START.replace(hour=15, minute=14), 100, 100.5, 99, 100.75))
        self.assertEqual(ladder.status, "CLOSED")
        self.assertEqual(ladder.exit_reason, "intraday_close")
        self.assertEqual(ladder.exit_index_price, 100.75)

    def test_intraday_close_does_not_let_the_three_fifteen_bar_open_a_position(self):
        ladder = self._ladder(intraday_close=True)
        ladder.on_candle(bar("1m", 1, 105, 106, 104, 105))
        ladder.on_candle(bar("1m", 2, 105, 105.5, 101, 102))
        ladder.on_candle(bar("1m", 3, 102, 102.5, 98, 99))  # armed at 102
        # The 15:14 bar rises through the stop AND is the day's last chance:
        # it must not buy something the next bar has to sell.
        ladder.on_candle(LadderCandle("1m", START.replace(hour=15, minute=14), 99, 103, 98.5, 102.5))
        self.assertEqual(ladder.fills, [])
        self.assertEqual(ladder.status, "CLOSED")


if __name__ == "__main__":
    unittest.main()
