"""The Candle Entry routes' rules that do not need a broker: the contract, the
pricing-by-age lookup, and that Backtest and Start ask the same questions."""

import base64
import os
import unittest
from datetime import date, datetime, timedelta

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-candle-entry-routes.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-candle-entry-routes-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import app as app_module  # noqa: E402

IST = app_module.IST


class ContractRuleTests(unittest.TestCase):
    """One CE for the whole ladder: the monthly, ATM-2 of the mother close, lot by date."""

    def _mother(self, when: datetime, close: float):
        return app_module.IndexCandle(when.replace(tzinfo=IST), close, close + 20, close - 20, close)

    def test_the_monthly_inside_fifteen_to_forty_five_days_is_taken(self):
        mother = self._mother(datetime(2026, 7, 22, 9, 15), 24110.0)
        expiries = [
            date(2026, 7, 28),
            date(2026, 8, 4),
            date(2026, 8, 11),
            date(2026, 8, 18),
            date(2026, 8, 25),
            date(2026, 9, 29),
        ]
        contract = app_module._candle_entry_contract(mother, expiries, -2)
        self.assertEqual(contract.expiry, date(2026, 8, 25))  # 34 DTE, the August monthly
        self.assertEqual(contract.strike, 24000)  # ATM 24100 - 100
        self.assertEqual(contract.option_type, "CE")
        self.assertEqual(contract.lot_size, 65)
        self.assertEqual(contract.security_id, "")

    def test_the_lot_size_follows_the_mothers_date_not_todays(self):
        mother = self._mother(datetime(2025, 3, 4, 9, 15), 22000.0)
        expiries = [date(2025, 3, 27), date(2025, 4, 24)]
        contract = app_module._candle_entry_contract(mother, expiries, -2)
        self.assertEqual(contract.lot_size, 75)  # the 2025 lot
        self.assertEqual(contract.expiry, date(2025, 3, 27))

    def test_the_offset_is_measured_from_the_mothers_atm(self):
        mother = self._mother(datetime(2026, 7, 22, 9, 15), 24124.0)  # rounds to 24100
        contract = app_module._candle_entry_contract(mother, [date(2026, 8, 25)], 0)
        self.assertEqual(contract.strike, 24100)
        contract = app_module._candle_entry_contract(mother, [date(2026, 8, 25)], -4)
        self.assertEqual(contract.strike, 23900)

    def test_the_weekly_rule_takes_the_first_expiry_at_least_four_days_out(self):
        mother = self._mother(datetime(2026, 7, 22, 9, 15), 24110.0)  # a Wednesday
        expiries = [date(2026, 7, 28), date(2026, 8, 4), date(2026, 8, 25)]
        contract = app_module._candle_entry_contract(mother, expiries, -2, "weekly4")
        self.assertEqual(contract.expiry, date(2026, 7, 28))  # 6 days: the first one >= 4
        mother = self._mother(datetime(2026, 7, 25, 9, 15), 24110.0)  # 3 days before the 28th
        contract = app_module._candle_entry_contract(mother, expiries, -2, "weekly4")
        self.assertEqual(contract.expiry, date(2026, 8, 4))
        with self.assertRaises(app_module.HTTPException):
            app_module._candle_entry_contract(mother, expiries, -2, "fortnightly")

    def test_no_expiry_at_all_is_refused_plainly(self):
        mother = self._mother(datetime(2026, 7, 22, 9, 15), 24110.0)
        with self.assertRaises(app_module.HTTPException) as caught:
            app_module._candle_entry_contract(mother, [], -2)
        self.assertEqual(caught.exception.status_code, 503)


class _Broker:
    def __init__(self, ltp=None):
        self.ltp = ltp
        self.asked = []

    def get_option_ltp(self, underlying, strike, expiry, side):
        self.asked.append((underlying, strike, expiry, side))
        return self.ltp


class PricingByAgeTests(unittest.TestCase):
    """A fresh minute gets the live quote; an old one only recorded history."""

    CONTRACT = app_module.FixedCampaignOption("NIFTY", 24000, date(2026, 8, 25), "CE", 65, "")

    def test_a_fresh_minute_is_the_live_quote(self):
        broker = _Broker(ltp=321.5)
        lookup = app_module._candle_entry_premium_lookup(broker, history=lambda _w, _c: 999.0)
        self.assertEqual(lookup(datetime.now(IST) - timedelta(seconds=30), self.CONTRACT), 321.5)
        self.assertEqual(broker.asked[0][:2], ("NIFTY", 24000))

    def test_an_old_minute_never_gets_todays_quote(self):
        broker = _Broker(ltp=321.5)
        history_calls = []

        def history(when, contract):
            history_calls.append((when, contract.strike))
            return 210.25

        lookup = app_module._candle_entry_premium_lookup(broker, history=history)
        old = datetime.now(IST) - timedelta(hours=3)
        self.assertEqual(lookup(old, self.CONTRACT), 210.25)
        self.assertEqual(broker.asked, [])
        self.assertEqual(history_calls[0][1], 24000)

    def test_an_old_minute_with_no_history_is_a_gap_not_a_guess(self):
        broker = _Broker(ltp=321.5)
        lookup = app_module._candle_entry_premium_lookup(broker, history=None)
        self.assertIsNone(lookup(datetime.now(IST) - timedelta(hours=3), self.CONTRACT))
        self.assertEqual(broker.asked, [])

    def test_a_failed_live_quote_does_not_fall_through_to_history(self):
        broker = _Broker(ltp=None)
        lookup = app_module._candle_entry_premium_lookup(broker, history=lambda _w, _c: 999.0)
        self.assertIsNone(lookup(datetime.now(IST), self.CONTRACT))


class PayloadIdentityTests(unittest.TestCase):
    """Backtest and Start trade ONE ladder, so they must ask the same questions."""

    def test_backtest_and_start_share_every_field(self):
        start = set(app_module.CandleEntryPaperStartPayload.model_fields)
        backtest = set(app_module.CandleEntryBacktestPayload.model_fields)
        self.assertEqual(start, backtest)
        self.assertEqual(
            start,
            {
                "mother_timestamp",
                "timeframe",
                "ce_offset_steps",
                "intraday_close",
                "expiry_rule",
                "target_fraction",
                "trailing_target",
            },
        )

    def test_intraday_defaults_off_the_way_the_ladder_was_measured(self):
        self.assertFalse(app_module.CandleEntryPaperStartPayload(mother_timestamp="2026-08-10T12:30").intraday_close)
        self.assertFalse(app_module.CandleEntryBacktestPayload(mother_timestamp="2026-08-10T12:30").intraday_close)


class MotherValidationTests(unittest.TestCase):
    def test_a_mother_must_sit_on_its_charts_grid_and_be_closed(self):
        now = datetime(2026, 8, 18, 12, 0, tzinfo=IST)
        ok = datetime(2026, 8, 18, 10, 15, tzinfo=IST)
        app_module._candle_entry_validate_mother(ok, "1h", now)  # 10:15 is on the 1H grid
        with self.assertRaises(app_module.HTTPException):
            app_module._candle_entry_validate_mother(datetime(2026, 8, 18, 10, 30, tzinfo=IST), "1h", now)
        with self.assertRaises(app_module.HTTPException):
            app_module._candle_entry_validate_mother(
                datetime(2026, 8, 18, 11, 15, tzinfo=IST), "1h", now
            )  # closes 12:15
        app_module._candle_entry_validate_mother(datetime(2026, 8, 18, 11, 40, tzinfo=IST), "5m", now)
        with self.assertRaises(app_module.HTTPException):
            app_module._candle_entry_validate_mother(datetime(2026, 8, 18, 11, 42, tzinfo=IST), "5m", now)


if __name__ == "__main__":
    unittest.main()
