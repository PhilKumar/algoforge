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

    def test_session_and_expiry_deadline(self):
        self.assertTrue(is_nse_cash_session(datetime(2026, 7, 20, 9, 15)))
        self.assertFalse(is_nse_cash_session(datetime(2026, 7, 20, 15, 30)))
        expiry = date(2026, 7, 28)
        self.assertFalse(option_expiry_squareoff_due(datetime(2026, 7, 28, 14, 59), expiry))
        self.assertTrue(option_expiry_squareoff_due(datetime(2026, 7, 28, 15, 0), expiry))


if __name__ == "__main__":
    unittest.main()
