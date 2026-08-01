"""The hybrid premium lookup prices what actually traded, and says what broke.

Born from the 2026-08-01 fib-backtest failure: Dhan HAD the 10:00 bar for
NIFTY 23900CE, but the lookup keyed its series with the frame's naive
timestamps while the engine asked with IST-aware ones -- an aware and a naive
datetime never compare equal, so every Dhan-priced minute "gapped" and the
whole backtest withheld its P&L.  These tests pin the three behaviours the fix
introduced:

  * one canonical naive-IST minute key on both sides of the dict,
  * a bounded last-traded-bar fallback for genuinely quiet minutes,
  * source failures (dead token, missing scrip id) reported as failures,
    never disguised as market gaps.
"""

import os
import sys
import unittest
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from app import IST, _hybrid_premium_lookup, _premium_minute  # noqa: E402


@dataclass(frozen=True)
class _Contract:
    strike: float = 23900.0
    expiry: date = date(2026, 8, 25)
    option_type: str = "CE"


def _frame(minutes: dict[datetime, float]) -> pd.DataFrame:
    index = pd.DatetimeIndex(sorted(minutes))  # naive, exactly as Dhan's frame arrives
    return pd.DataFrame({"open": [minutes[m] for m in sorted(minutes)]}, index=index)


class _Broker:
    def __init__(self, minutes=None, error: Exception | None = None):
        self.minutes = minutes or {}
        self.error = error
        self.calls = 0

    def get_historical_data(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _frame(self.minutes)


class _ScripMaster:
    security_id = "61557"

    @classmethod
    def lookup(cls, *_args, **_kwargs):
        return cls.security_id


def _build(broker, forward_minutes: int = 5):
    # ScripMaster is resolved when the lookup RUNS, so the patch has to outlive
    # this call -- the test classes swap app_module.ScripMaster in setUp.
    return _hybrid_premium_lookup(
        broker, "NIFTY", None, set(), date(2026, 7, 17), date(2026, 7, 28), forward_minutes=forward_minutes
    )


class MinuteKeyTests(unittest.TestCase):
    def test_aware_and_naive_collapse_to_the_same_minute(self):
        aware = datetime(2026, 7, 22, 10, 0, 30, tzinfo=IST)
        naive = datetime(2026, 7, 22, 10, 0, 15)
        self.assertEqual(_premium_minute(aware), _premium_minute(naive))
        self.assertIsNone(_premium_minute(aware).tzinfo)


class HybridLookupTests(unittest.TestCase):
    def setUp(self):
        self._original_scrip = app_module.ScripMaster
        app_module.ScripMaster = _ScripMaster

    def tearDown(self):
        app_module.ScripMaster = self._original_scrip

    def test_an_aware_engine_timestamp_finds_the_naive_dhan_bar(self):
        # The exact 2026-08-01 failure: the bar exists, the tzinfo hid it.
        broker = _Broker({datetime(2026, 7, 22, 10, 0): 485.75})
        lookup = _build(broker)
        price = lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract())
        self.assertEqual(price, 485.75)
        self.assertEqual(lookup.source_failures, [])
        self.assertEqual(lookup.stale_fills, [])

    def test_a_quiet_minute_prices_from_the_last_traded_bar_and_says_so(self):
        broker = _Broker({datetime(2026, 7, 22, 9, 57): 480.0})
        lookup = _build(broker)
        price = lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract())
        self.assertEqual(price, 480.0)
        self.assertEqual(len(lookup.stale_fills), 1)
        self.assertIn("3 min earlier", lookup.stale_fills[0])

    def test_a_session_open_fill_prices_from_the_days_first_trade(self):
        # The 2026-07-27 failure: a gap-up crossed the target on the 09:15
        # candle, but the deep strike's first print of the day came at 09:17.
        # An exit order resting there fills at that next trade — walking
        # backward finds only yesterday, which the lookup must refuse.
        broker = _Broker({datetime(2026, 7, 27, 9, 17): 402.0})
        lookup = _build(broker, forward_minutes=15)
        price = lookup(datetime(2026, 7, 27, 9, 15, tzinfo=IST), _Contract())
        self.assertEqual(price, 402.0)
        self.assertEqual(len(lookup.stale_fills), 1)
        self.assertIn("2 min into the candle", lookup.stale_fills[0])

    def test_the_forward_scan_stays_inside_the_fills_own_candle(self):
        # A 5m replay owns minutes T..T+4; a bar at T+5 belongs to the next
        # candle and reading it would be lookahead.
        broker = _Broker({datetime(2026, 7, 22, 10, 5): 402.0})
        lookup = _build(broker, forward_minutes=5)
        self.assertIsNone(lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract()))
        self.assertEqual(lookup.stale_fills, [])

    def test_the_exact_bar_wins_over_every_neighbour(self):
        broker = _Broker(
            {
                datetime(2026, 7, 22, 9, 59): 470.0,
                datetime(2026, 7, 22, 10, 0): 485.75,
                datetime(2026, 7, 22, 10, 1): 490.0,
            }
        )
        lookup = _build(broker, forward_minutes=15)
        self.assertEqual(lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract()), 485.75)
        self.assertEqual(lookup.stale_fills, [])

    def test_the_fallback_stops_at_ten_minutes(self):
        broker = _Broker({datetime(2026, 7, 22, 9, 49): 470.0})
        lookup = _build(broker)
        self.assertIsNone(lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract()))
        self.assertEqual(lookup.stale_fills, [])

    def test_the_fallback_never_reaches_into_yesterday(self):
        broker = _Broker({datetime(2026, 7, 21, 15, 29): 470.0})
        lookup = _build(broker)
        self.assertIsNone(lookup(datetime(2026, 7, 22, 9, 16, tzinfo=IST), _Contract()))

    def test_a_dead_dhan_fetch_is_a_source_failure_not_a_gap(self):
        broker = _Broker(error=Exception("DH-901 token expired"))
        lookup = _build(broker)
        self.assertIsNone(lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract()))
        self.assertEqual(len(lookup.source_failures), 1)
        self.assertIn("DH-901", lookup.source_failures[0])
        # One failed fetch is remembered, not retried per minute.
        lookup(datetime(2026, 7, 22, 10, 1, tzinfo=IST), _Contract())
        self.assertEqual(broker.calls, 1)
        self.assertEqual(len(lookup.source_failures), 1)

    def test_a_missing_scrip_id_names_itself(self):
        class _NoScrip:
            @classmethod
            def lookup(cls, *_a, **_k):
                return None

        app_module.ScripMaster = _NoScrip
        broker = _Broker({datetime(2026, 7, 22, 10, 0): 485.75})
        lookup = _build(broker)
        self.assertIsNone(lookup(datetime(2026, 7, 22, 10, 0, tzinfo=IST), _Contract()))
        self.assertIn("no security id", lookup.source_failures[0])
        self.assertEqual(broker.calls, 0)


if __name__ == "__main__":
    unittest.main()
