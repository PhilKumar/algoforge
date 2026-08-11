"""How a fib-space paper leg gets its rupee price.

For a while the paper driver could only ask for a LIVE quote, inside a seven
minute window. That is the right rule for a decision seen as it happens, but it
meant a mother named after its ladder had already filled produced a campaign
with no money in it at all — Phil, 2026-08-11: a real BANKNIFTY target hit,
reported "unpriced", with the whole point of the paper run being to judge the
rupees.

It was a wiring gap, not a data limit: Dhan serves one-minute candles for any
still-listed contract, and the fib-space BACKTEST already prices every fill from
them. These tests pin the two rules that make using them honest:

  * a live quote always wins, so a decision seen as it happens is never
    re-priced from a candle
  * a price read back from a candle is labelled "history" all the way out, so
    the caveat travels with the number instead of dissolving into the total
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
# Generated, never written down. A literal Fernet key in the file is high
# enough entropy that the secret scanner blocks the push, and rightly so — a
# committed key reads the same to a scanner whether or not it guards anything.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

import app as app_module  # noqa: E402


class _Contract:
    underlying = "BANKNIFTY"
    strike = 57_100
    option_type = "CE"
    security_id = "44444"

    def __init__(self, expiry: date):
        self.expiry = expiry


class _Broker:
    """Records what was asked of it; answers candles, never a live quote."""

    def __init__(self, minutes=None, ltp=None):
        self.minutes = minutes or {}
        self.ltp = ltp
        self.historical_calls = 0
        self.ltp_calls = 0

    def get_option_ltp(self, *args, **kwargs):
        self.ltp_calls += 1
        if self.ltp is None:
            raise RuntimeError("no live quote for a contract that already traded")
        return self.ltp

    def get_historical_data(self, *args, **kwargs):
        self.historical_calls += 1
        if not self.minutes:
            return pd.DataFrame()
        index = pd.DatetimeIndex(list(self.minutes))
        return pd.DataFrame({"open": list(self.minutes.values())}, index=index)


class _Archive:
    """Stand-in for the on-disk option archive: empty, and remembers stores."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.stored = []
        _Archive.instances.append(self)

    def load(self, **kwargs):
        return {}

    def store(self, **kwargs):
        self.stored.append(kwargs)


class FibSpacePremiumLookupTests(unittest.TestCase):
    def setUp(self):
        _Archive.instances = []
        self.today = datetime.now(app_module.IST).replace(tzinfo=None)
        self.expiry = (self.today + timedelta(days=20)).date()
        self.contract = _Contract(self.expiry)
        # A minute earlier today, well outside the live-quote window.
        self.minute = self.today.replace(hour=13, minute=0, second=0, microsecond=0)
        if self.minute > self.today:
            self.minute -= timedelta(days=1)
        patcher = patch("data.option_archive.OptionDataArchive", _Archive)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lookup(self, broker):
        return app_module._fib_space_premium_lookup(broker, "BANKNIFTY")

    def test_a_live_quote_wins_and_no_candle_is_fetched(self):
        """A decision seen as it happens is never re-priced from history."""
        broker = _Broker(minutes={self.minute: 999.0}, ltp=250.0)
        answer = self._lookup(broker)(self.today, self.contract)

        self.assertEqual(answer, (250.0, "live"))
        self.assertEqual(broker.historical_calls, 0, "a live quote must not trigger a fetch")

    def test_an_old_fill_is_priced_from_its_own_recorded_minute(self):
        """This is the whole fix: a real traded price instead of a blank."""
        broker = _Broker(minutes={self.minute: 187.5})
        answer = self._lookup(broker)(self.minute, self.contract)

        self.assertEqual(answer, (187.5, "history"))

    def test_an_illiquid_minute_walks_back_within_the_same_day_only(self):
        """A strike can go minutes without a trade; a real order still fills
        near the last one. Across a session boundary it is a different market."""
        traded = self.minute - timedelta(minutes=3)
        broker = _Broker(minutes={traded: 143.0})
        self.assertEqual(self._lookup(broker)(self.minute, self.contract), (143.0, "history"))

        stale = _Broker(minutes={self.minute - timedelta(days=1): 143.0})
        self.assertIsNone(self._lookup(stale)(self.minute, self.contract))

    def test_a_minute_neither_source_has_stays_unpriced(self):
        broker = _Broker(minutes={})
        self.assertIsNone(self._lookup(broker)(self.minute, self.contract))

    def test_a_broken_fetch_leaves_the_leg_unpriced_instead_of_raising(self):
        """A paper run must not halt because the broker blinked."""

        class _Angry(_Broker):
            def get_historical_data(self, *args, **kwargs):
                raise RuntimeError("dead token")

        self.assertIsNone(self._lookup(_Angry())(self.minute, self.contract))

    def test_the_contract_is_fetched_once_and_reused(self):
        """The Dhan budget is account-wide; one campaign must not spend it per
        leg. Every later leg of the same contract comes out of the cache."""
        broker = _Broker(minutes={self.minute: 187.5, self.minute - timedelta(minutes=1): 190.0})
        lookup = self._lookup(broker)
        lookup(self.minute, self.contract)
        lookup(self.minute - timedelta(minutes=1), self.contract)
        lookup(self.minute, self.contract)

        self.assertEqual(broker.historical_calls, 1)

    def test_what_is_fetched_is_written_to_the_archive(self):
        """So a restart, and the backtest, reuse it instead of refetching."""
        broker = _Broker(minutes={self.minute: 187.5})
        self._lookup(broker)(self.minute, self.contract)

        stored = [row for archive in _Archive.instances for row in archive.stored]
        self.assertTrue(stored, "the fetched candles were never archived")
        self.assertEqual(stored[0]["provider"], "dhan")
        self.assertEqual(stored[0]["strike"], self.contract.strike)

    def test_no_contract_is_not_a_crash(self):
        """campaign.contract is None until the first fill picks one."""
        self.assertIsNone(self._lookup(_Broker())(self.minute, None))


if __name__ == "__main__":
    unittest.main()
