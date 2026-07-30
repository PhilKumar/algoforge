"""Tests for the pieces a multi-month cascade backtest depends on.

These cover the contract calendar, automatic mother detection, and the replay
ordering and rule-interpretation flags added to `OneHourCascade`.
"""

import unittest
from datetime import date, datetime, timedelta

from engine.cascade_calendar import CalendarError, ContractCalendar, ContractRule
from engine.cascade_mothers import atr_series, find_mother_candles
from engine.cascade_options import (
    Candle,
    CascadeConfig,
    Contract,
    NiftyContractResolver,
    OneHourCascade,
    OptionCandle,
    _close_times,
)


def t(hour: int, minute: int = 15, day: int = 20) -> datetime:
    return datetime(2026, 7, day, hour, minute)


def flat_option(timestamp, _contract: Contract):
    return OptionCandle(timestamp, 100, 102, 98, 101)


class ContractCalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = ContractCalendar(
            [
                ContractRule(date(2025, 1, 1), expiry_weekday=3, lot_size=25),  # Thursday
                ContractRule(date(2025, 9, 1), expiry_weekday=1, lot_size=65),  # Tuesday
            ]
        )

    def test_rule_switches_at_the_effective_date(self):
        self.assertEqual(self.calendar.rule_for(date(2025, 8, 31)).expiry_weekday, 3)
        self.assertEqual(self.calendar.rule_for(date(2025, 8, 31)).lot_size, 25)
        self.assertEqual(self.calendar.rule_for(date(2025, 9, 1)).expiry_weekday, 1)
        self.assertEqual(self.calendar.rule_for(date(2025, 9, 1)).lot_size, 65)

    def test_refuses_to_backdate_todays_rules(self):
        """Guessing an expiry weekday reprices every trade before the gap."""
        with self.assertRaises(CalendarError) as caught:
            self.calendar.rule_for(date(2024, 6, 1))
        self.assertIn("--calendar", str(caught.exception))

    def test_holiday_tuesday_shifts_expiry_to_the_previous_session(self):
        # A full weekday session calendar for two weeks, minus Tue 9 Sep.
        sessions = {
            day for offset in range(21) for day in [date(2025, 9, 1) + timedelta(days=offset)] if day.weekday() < 5
        } - {date(2025, 9, 9)}
        expiries = self.calendar.weekly_expiries(date(2025, 9, 1), date(2025, 9, 18), sessions)
        self.assertIn(date(2025, 9, 2), expiries)  # normal Tuesday
        self.assertIn(date(2025, 9, 8), expiries)  # holiday Tuesday -> Monday
        self.assertNotIn(date(2025, 9, 9), expiries)

    def test_expiries_beyond_observed_sessions_use_the_raw_weekday(self):
        sessions = {date(2025, 9, 1), date(2025, 9, 2)}
        expiries = self.calendar.weekly_expiries(date(2025, 9, 1), date(2025, 9, 16), sessions)
        self.assertIn(date(2025, 9, 16), expiries)


class MotherScannerTests(unittest.TestCase):
    @staticmethod
    def _series(highs):
        """Candles with a fixed 40-point body so ATR is predictable."""
        return [
            Candle(t(9) + timedelta(minutes=15 * position), high - 40, high, high - 40, high - 20)
            for position, high in enumerate(highs)
        ]

    # Leading flat bars let ATR warm up; the scanner skips pivots until it has.
    LEAD = [100] * 8
    PEAK = LEAD + [100, 110, 120, 200, 120, 110, 100] + [100] * 10
    DOUBLE_TOP = LEAD + [100, 110, 200, 200, 120, 110, 100] + [100] * 10

    def test_finds_the_pivot_high_and_confirms_it_late(self):
        candles = self._series(self.PEAK)
        found = find_mother_candles(candles, left_bars=2, right_bars=2, atr_period=5, min_range_atr=0.0)
        self.assertTrue(found)
        pivot = found[0]
        self.assertEqual(pivot.high, 200)
        self.assertEqual(pivot.index, 11)
        # A live system could not know index 11 was a pivot until index 13.
        self.assertEqual(pivot.confirmed_index, 13)
        self.assertEqual(pivot.confirmed_at, candles[13].timestamp)
        self.assertGreater(pivot.confirmed_at, pivot.timestamp)

    def test_atr_filter_rejects_a_noise_level_swing(self):
        candles = self._series(self.PEAK)
        # Every candle spans 40 points, so a 10x-ATR floor rejects all of them.
        found = find_mother_candles(candles, left_bars=2, right_bars=2, atr_period=5, min_range_atr=10.0)
        self.assertEqual(found, [])

    def test_flat_double_top_yields_one_mother_not_two(self):
        candles = self._series(self.DOUBLE_TOP)
        found = find_mother_candles(candles, left_bars=2, right_bars=2, atr_period=5, min_range_atr=0.0)
        self.assertEqual([row.index for row in found], [10])

    def test_atr_is_none_until_the_period_is_full(self):
        candles = self._series([100] * 10)
        values = atr_series(candles, 5)
        self.assertEqual(values[:4], [None, None, None, None])
        self.assertIsNotNone(values[4])


class ReplayOrderingTests(unittest.TestCase):
    def test_close_time_comes_from_the_next_bar_in_session(self):
        rows = [Candle(t(9), 1, 1, 1, 1), Candle(t(10), 1, 1, 1, 1), Candle(t(14), 1, 1, 1, 1)]
        closes = _close_times(rows, "1h")
        self.assertEqual(closes[0], t(10))
        self.assertEqual(closes[1], t(14))
        # Last bar of the session has no successor, so it falls back to nominal.
        self.assertEqual(closes[2], t(14) + timedelta(minutes=60))

    def test_merged_timeframes_replay_in_close_order_not_open_order(self):
        """A 15m bar must not reach the engine before the 5m bars inside it."""
        seen = []

        class Recording(OneHourCascade):
            def on_candle(self, candle, *, timeframe=None):
                seen.append((timeframe, candle.timestamp))

        config = CascadeConfig(
            mother_timestamp=t(9),
            mother_high=25000,
            mother_low=24900,
            timeframe="5m",
            stage_timeframes=("5m", "15m", "1h"),
        )
        resolver = NiftyContractResolver([date(2026, 7, 28), date(2026, 8, 4)])
        five = [Candle(t(10, minute), 1, 1, 1, 1) for minute in (15, 20, 25, 30, 35, 40)]
        fifteen = [Candle(t(10, 15), 1, 1, 1, 1), Candle(t(10, 30), 1, 1, 1, 1)]

        Recording(config, resolver, flat_option).run({"5m": five, "15m": fifteen})

        # The 15m bar opening 10:15 closes at 10:30, so it must land after the
        # 5m bars at 10:15, 10:20 and 10:25 -- never before them.
        first_fifteen = seen.index(("15m", t(10, 15)))
        for minute in (15, 20, 25):
            self.assertLess(seen.index(("5m", t(10, minute))), first_fifteen)


class RuleInterpretationTests(unittest.TestCase):
    def setUp(self):
        self.expiries = [date(2026, 7, 28), date(2026, 8, 4), date(2026, 8, 11)]
        self.resolver = NiftyContractResolver(self.expiries, strike_step=50, lot_size=65)

    def _run(self, candles, **overrides):
        config = CascadeConfig(
            mother_timestamp=t(9),
            mother_high=25000,
            mother_low=24900,
            stage_timeframes=("1h", "1h", "1h"),
            **overrides,
        )
        return OneHourCascade(config, self.resolver, flat_option).run(candles)

    def test_previous_candle_mode_arms_off_the_green_it_follows(self):
        """Rule 4 read literally: compare to the previous candle, any colour."""
        candles = [
            Candle(t(10), 24950, 24960, 24880, 24890),  # red below mother low
            Candle(t(11), 24890, 24930, 24880, 24920),  # green, closes 24,920
            Candle(t(12), 24920, 24925, 24860, 24870),  # red, below the green
            Candle(t(13), 24870, 24990, 24865, 24980),  # recovery
        ]
        literal = self._run(candles, arm_compare="previous_candle")
        self.assertEqual([entry.spot for entry in literal.entries], [24920])

        # Comparing to the previous *red* close instead needs 24,870 < 24,890,
        # which is true, so it arms on the earlier red's close.
        qualifying = self._run(candles, arm_compare="last_qualifying")
        self.assertEqual([entry.spot for entry in qualifying.entries], [24890])

    def test_marked_low_mode_changes_where_the_next_stage_waits(self):
        candles = [
            Candle(t(10), 24950, 24960, 24800, 24890),  # deep wick to 24,800
            Candle(t(11), 24890, 24900, 24870, 24880),  # arms; latest low 24,870
            Candle(t(12), 24880, 24990, 24875, 24980),  # fills stage 1
        ]
        lowest = self._run(candles, mark_low_mode="lowest")
        latest = self._run(candles, mark_low_mode="latest")
        self.assertEqual(lowest.events[-1]["marked_low"], 24800)
        self.assertEqual(latest.events[-1]["marked_low"], 24870)

    def test_slippage_pushes_the_fill_through_the_trigger(self):
        candles = [
            Candle(t(10), 24950, 24960, 24880, 24890),
            Candle(t(11), 24890, 24900, 24850, 24870),
            Candle(t(12), 24870, 24990, 24865, 24980),
        ]
        clean = self._run(candles)
        slipped = self._run(candles, slippage_points=2.0, option_slippage_pct=0.01)
        self.assertEqual(slipped.entries[0].spot, clean.entries[0].spot + 2.0)
        self.assertGreater(slipped.entries[0].option_price, clean.entries[0].option_price)

    def test_restrike_repicks_the_strike_after_the_stop_walks_down(self):
        candles = [
            Candle(t(10), 24950, 24960, 24880, 24890),
            Candle(t(11), 24890, 24895, 24850, 24870),  # arms at 24,890
            Candle(t(12), 24870, 24875, 24500, 24520),  # walks far down
            Candle(t(13), 24520, 24900, 24515, 24890),  # fills
        ]
        frozen = self._run(candles, restrike_on_stop_walk=False)
        repicked = self._run(candles, restrike_on_stop_walk=True)
        self.assertGreater(frozen.entries[0].contract.strike, repicked.entries[0].contract.strike)

    def test_expiry_square_off_is_priced_from_intrinsic_without_premium_data(self):
        """The one exit that stays exact even with no option history at all."""
        config = CascadeConfig(
            mother_timestamp=t(9),
            mother_high=25000,
            mother_low=24900,
            stage_timeframes=("1h", "1h", "1h"),
            strict_option_data=False,
            # Pinned to 7-13 so the 28 July candle below is still expiry day.
            # This test is about intrinsic pricing at expiry, not the DTE band.
            min_dte=7,
            max_dte=13,
        )
        candles = [
            Candle(t(10), 24950, 24960, 24880, 24890),
            Candle(t(11), 24890, 24900, 24850, 24870),
            Candle(t(12), 24870, 24990, 24865, 24980),  # fills stage 1
            Candle(datetime(2026, 7, 28, 15, 15), 24000, 24010, 23900, 23950),  # expiry
        ]

        def only_entry_priced(timestamp, _contract):
            return OptionCandle(timestamp, 300, 305, 295, 302) if timestamp == t(12) else None

        result = OneHourCascade(config, NiftyContractResolver(self.expiries), only_entry_priced).run(candles)
        self.assertEqual(result.status, "expired")
        # Strike 24,850 CE against a 23,950 settlement is worthless: the whole
        # premium is lost, and that loss must be booked rather than skipped.
        self.assertEqual(result.exit_option_prices, [0.0])
        self.assertLess(result.net_pnl, 0)
        self.assertGreater(result.costs_total, 0)


if __name__ == "__main__":
    unittest.main()
