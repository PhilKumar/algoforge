import unittest
from datetime import date, datetime

from engine.cascade_options import CascadeOptionsAdapter, is_nse_cash_session, option_expiry_squareoff_due


class NextWeeklyExpiryTests(unittest.TestCase):
    def test_current_week_is_skipped_from_friday_and_monday(self):
        expiries = [date(2026, 7, 21), date(2026, 7, 28), date(2026, 8, 4)]
        self.assertEqual(CascadeOptionsAdapter._next_weekly_expiry(expiries, date(2026, 7, 17)), date(2026, 7, 28))
        self.assertEqual(CascadeOptionsAdapter._next_weekly_expiry(expiries, date(2026, 7, 20)), date(2026, 7, 28))

    def test_holiday_shifted_monday_is_allowed_at_six_dte(self):
        expiries = [date(2026, 4, 13), date(2026, 4, 21)]
        self.assertEqual(CascadeOptionsAdapter._next_weekly_expiry(expiries, date(2026, 4, 7)), date(2026, 4, 13))

    def test_session_and_expiry_deadline(self):
        self.assertTrue(is_nse_cash_session(datetime(2026, 7, 20, 9, 15)))
        self.assertFalse(is_nse_cash_session(datetime(2026, 7, 20, 15, 30)))
        expiry = date(2026, 7, 28)
        self.assertFalse(option_expiry_squareoff_due(datetime(2026, 7, 28, 14, 59), expiry))
        self.assertTrue(option_expiry_squareoff_due(datetime(2026, 7, 28, 15, 0), expiry))


if __name__ == "__main__":
    unittest.main()
