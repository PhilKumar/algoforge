import unittest
from datetime import date, datetime

from engine.cascade_options import CascadeOptionsAdapter, is_nse_cash_session, option_expiry_squareoff_due


class NextWeeklyExpiryTests(unittest.TestCase):
    """The weekly rule -- no longer the default, still reachable and still correct."""

    def test_current_week_is_skipped_from_friday_and_monday(self):
        expiries = [date(2026, 7, 21), date(2026, 7, 28), date(2026, 8, 4)]
        for day in (date(2026, 7, 17), date(2026, 7, 20)):
            self.assertEqual(CascadeOptionsAdapter._next_expiry(expiries, day, monthly_only=False), date(2026, 7, 28))

    def test_holiday_shifted_monday_is_allowed_at_six_dte(self):
        expiries = [date(2026, 4, 13), date(2026, 4, 21)]
        self.assertEqual(
            CascadeOptionsAdapter._next_expiry(expiries, date(2026, 4, 7), monthly_only=False), date(2026, 4, 13)
        )


class MonthlyExpiryTests(unittest.TestCase):
    """The default since 2026-07-30: weeklies are skipped entirely."""

    EXPIRIES = [date(2026, 7, 7), date(2026, 7, 14), date(2026, 7, 21), date(2026, 7, 28), date(2026, 8, 25)]

    def test_only_the_last_expiry_of_a_month_is_eligible(self):
        # The 7th, 14th and 21st all clear ten days from 1 July and are all
        # weeklies, so the campaign must still take the 28th -- July's monthly.
        self.assertEqual(CascadeOptionsAdapter._next_expiry(self.EXPIRIES, date(2026, 7, 1)), date(2026, 7, 28))

    def test_a_monthly_inside_ten_days_rolls_to_the_next_month(self):
        # From 20 July the 28th is only 8 days out, so August is taken instead.
        self.assertEqual(CascadeOptionsAdapter._next_expiry(self.EXPIRIES, date(2026, 7, 20)), date(2026, 8, 25))

    def test_a_natively_monthly_chain_passes_through_untouched(self):
        monthly = [date(2026, 7, 28), date(2026, 8, 25), date(2026, 9, 29)]
        self.assertEqual(CascadeOptionsAdapter._next_expiry(monthly, date(2026, 7, 1), "BANKNIFTY"), date(2026, 7, 28))


class MonthlyDteWindowTests(unittest.TestCase):
    """15-45 DTE, and what happens when nothing is inside it.

    Only the floor used to be applied. That picked a real untradeable contract:
    on 2026-08-11 the August monthly had decayed to 14 DTE, was thrown out by
    the `>= 15` filter, and September was taken at 49 DTE. At ATM-2 that is deep
    ITM and far-dated — it traded 660 units in a whole session with 13 distinct
    prices in 351 minute-bars, so both legs of a round priced off the same stale
    print and the campaign reported a realised P&L of exactly Rs 0.00.
    """

    BANKNIFTY = [date(2026, 7, 28), date(2026, 8, 25), date(2026, 9, 29)]

    def test_the_ceiling_is_applied_not_only_the_floor(self):
        # From 3 August: August is 22 DTE (inside), September 57 (over).
        self.assertEqual(CascadeOptionsAdapter._next_expiry(self.BANKNIFTY, date(2026, 8, 3)), date(2026, 8, 25))

    def test_the_real_case_takes_the_near_expiry_not_the_far_one(self):
        """11 Aug 2026: 14 DTE vs 49 DTE, neither inside 15-45.

        One day under the floor beats four days over the ceiling. This is the
        exact selection that produced the Rs 0.00 campaign.
        """
        picked = CascadeOptionsAdapter._next_expiry(self.BANKNIFTY, date(2026, 8, 11))
        self.assertEqual(picked, date(2026, 8, 25))
        self.assertEqual((picked - date(2026, 8, 11)).days, 14)

    def test_an_expiry_inside_the_window_always_beats_a_closer_one_outside(self):
        # From 20 July: 28 Jul is 8 DTE (outside), 25 Aug is 36 (inside).
        # The inside one wins even though the other is nearer the trade date.
        self.assertEqual(CascadeOptionsAdapter._next_expiry(self.BANKNIFTY, date(2026, 7, 20)), date(2026, 8, 25))

    def test_a_tie_goes_to_the_later_expiry(self):
        """A position with no stop loss ends only at expiry, so leave it road."""
        expiries = [date(2026, 8, 25), date(2026, 9, 29)]
        # From 12 Aug: 13 DTE (gap 2 under) and 48 DTE (gap 3 over) -> near.
        self.assertEqual(CascadeOptionsAdapter._next_expiry(expiries, date(2026, 8, 12)), date(2026, 8, 25))
        # From 13 Aug: 12 DTE (gap 3) and 47 DTE (gap 2) -> the far one now.
        self.assertEqual(CascadeOptionsAdapter._next_expiry(expiries, date(2026, 8, 13)), date(2026, 9, 29))

    def test_an_expired_month_is_never_offered(self):
        self.assertEqual(CascadeOptionsAdapter._next_expiry(self.BANKNIFTY, date(2026, 9, 1)), date(2026, 9, 29))

    def test_nothing_left_in_the_chain_is_refused(self):
        with self.assertRaises(Exception):
            CascadeOptionsAdapter._next_expiry([date(2026, 7, 28)], date(2026, 8, 11))


class MonthlyExpiryDeadlineTests(unittest.TestCase):
    def test_session_and_expiry_deadline(self):
        self.assertTrue(is_nse_cash_session(datetime(2026, 7, 20, 9, 15)))
        self.assertFalse(is_nse_cash_session(datetime(2026, 7, 20, 15, 30)))
        expiry = date(2026, 7, 28)
        self.assertFalse(option_expiry_squareoff_due(datetime(2026, 7, 28, 14, 59), expiry))
        self.assertTrue(option_expiry_squareoff_due(datetime(2026, 7, 28, 15, 0), expiry))


if __name__ == "__main__":
    unittest.main()
