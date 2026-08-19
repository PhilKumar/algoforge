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
        # `mode` (paper/live) is the one thing only a Start can mean; a
        # backtest is neither. Everything that shapes the LADDER is shared.
        self.assertEqual(start - {"mode"}, backtest)
        self.assertEqual(
            backtest,
            {
                "mother_mode",
                "mother_timestamp",
                "timeframe",
                "box_bars",
                "box_position",
                "ce_offset_steps",
                "strike_at",
                "intraday_close",
                "expiry_rule",
                "target_fraction",
                "trailing_target",
            },
        )

    def test_the_box_defaults_are_the_measured_winner(self):
        # 278 bars, the bottom quarter -- the configuration the tearsheet is
        # built from. Changing these silently would change what the page runs.
        payload = app_module.CandleEntryBacktestPayload(mother_timestamp="2026-08-10T12:30")
        self.assertEqual(payload.box_bars, 278)
        self.assertEqual(payload.box_position, 0.25)
        self.assertEqual(payload.mother_mode, "manual")
        # The strike is chosen where the first rung fills -- the mother's own
        # ATM-2 sat above the target on half the measured campaigns.
        self.assertEqual(payload.strike_at, "first_buy")
        with self.assertRaises(app_module.HTTPException):
            app_module._candle_entry_strike_at("second_buy")

    def test_live_is_refused_behind_the_same_gate_as_fib_boundary(self):
        with self.assertRaises(app_module.HTTPException) as caught:
            app_module._candle_entry_trade_mode("live")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(app_module._candle_entry_trade_mode("paper"), "paper")
        with self.assertRaises(app_module.HTTPException):
            app_module._candle_entry_trade_mode("demo")


class RegularSessionTests(unittest.TestCase):
    """Diwali's muhurat hour is not a session the ladder reads."""

    def _bar(self, when: datetime):
        return app_module.IndexCandle(when.replace(tzinfo=IST), 100.0, 101.0, 99.0, 100.5)

    def test_a_session_that_opens_in_the_afternoon_is_dropped_whole(self):
        normal = [self._bar(datetime(2025, 10, 20, 9, 15)), self._bar(datetime(2025, 10, 20, 9, 20))]
        muhurat = [self._bar(datetime(2025, 10, 21, 13, 45)), self._bar(datetime(2025, 10, 21, 13, 50))]
        after = [self._bar(datetime(2025, 10, 22, 9, 15))]
        kept = app_module._candle_entry_regular_sessions(normal + muhurat + after)
        self.assertEqual([row.timestamp.date() for row in kept], [date(2025, 10, 20)] * 2 + [date(2025, 10, 22)])

    def test_a_late_first_bar_from_a_partial_fetch_is_also_dropped(self):
        # The rule is "opens at 09:15": a fetch that starts mid-session on its
        # first day is treated the same way, which is the safe direction.
        rows = [self._bar(datetime(2025, 10, 20, 11, 0)), self._bar(datetime(2025, 10, 21, 9, 15))]
        kept = app_module._candle_entry_regular_sessions(rows)
        self.assertEqual([row.timestamp.date() for row in kept], [date(2025, 10, 21)])


class BoxMotherTests(unittest.IsolatedAsyncioTestCase):
    """The mother is FOUND: the latest bar making the N-bar high at or before the look-back moment."""

    class _Adapter:
        def __init__(self, rows):
            self.rows = rows

        async def async_get_candles(self, _symbol, _timeframe, from_date, to_date):
            return [row for row in self.rows if from_date <= row.timestamp.date() <= to_date]

    def _day(self, day: date, highs):
        out = []
        for i, high in enumerate(highs):
            when = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15, tzinfo=IST) + timedelta(
                minutes=5 * i
            )
            out.append(app_module.IndexCandle(when, high - 5, high, high - 10, high - 2))
        return out

    async def test_it_finds_the_latest_box_high_and_hands_back_its_window(self):
        # Three sessions of 75 bars; the box is 150 bars. The last bar to make
        # a 150-bar high is at 11:00 on day three (bar 21), after which highs fall.
        d1 = self._day(date(2026, 8, 3), [100 + i * 0.1 for i in range(75)])
        d2 = self._day(date(2026, 8, 4), [108 + i * 0.1 for i in range(75)])
        d3 = self._day(date(2026, 8, 5), [116 + i * 0.1 for i in range(22)] + [110 - i * 0.1 for i in range(53)])
        adapter = self._Adapter(d1 + d2 + d3)
        mother, window = await app_module._candle_entry_find_box_mother(
            adapter, "5m", 150, datetime(2026, 8, 5, 15, 30, tzinfo=IST)
        )
        self.assertEqual(mother.timestamp, d3[21].timestamp)
        self.assertEqual(len(window), 150)
        self.assertEqual(window[-1].timestamp, mother.timestamp)
        self.assertEqual(max(row.high for row in window), mother.high)

    async def test_the_look_back_moment_is_respected(self):
        d1 = self._day(date(2026, 8, 3), [100 + i * 0.1 for i in range(75)])
        d2 = self._day(date(2026, 8, 4), [108 + i * 0.1 for i in range(75)])
        d3 = self._day(date(2026, 8, 5), [116 + i * 0.1 for i in range(75)])
        adapter = self._Adapter(d1 + d2 + d3)
        # Looking back from day two's close must not see day three's highs.
        mother, _ = await app_module._candle_entry_find_box_mother(
            adapter, "5m", 100, datetime(2026, 8, 4, 15, 30, tzinfo=IST)
        )
        self.assertEqual(mother.timestamp.date(), date(2026, 8, 4))

    async def test_too_little_history_is_a_plain_404(self):
        adapter = self._Adapter(self._day(date(2026, 8, 5), [100 + i for i in range(30)]))
        with self.assertRaises(app_module.HTTPException) as caught:
            await app_module._candle_entry_find_box_mother(adapter, "5m", 278, datetime(2026, 8, 5, 15, 30, tzinfo=IST))
        self.assertEqual(caught.exception.status_code, 404)

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
