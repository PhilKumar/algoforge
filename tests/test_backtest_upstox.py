import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from data.backtest_upstox import UpstoxHistoricalPremiumSelector
from engine.cascade_options import OptionCandle


class _FakeUpstoxSource:
    cache_dir = None

    def __init__(self, **_kwargs):
        self._cache_dir = Path(self.cache_dir)
        self.requests_made = 0
        self.release_calls = 0

    def available_expiries(self):
        return {date(2026, 3, 19)}

    def _contract_index(self, _expiry):
        return {(25000, "CE"): "NSE_FO|25000CE"}

    def _minute_series(self, _instrument_key, _expiry):
        stamp = datetime(2026, 3, 18, 9, 15)
        return {
            stamp: OptionCandle(stamp, 251.0, 253.0, 249.0, 252.0),
            datetime(2026, 3, 18, 9, 16): OptionCandle(datetime(2026, 3, 18, 9, 16), 252.0, 254.0, 251.0, 253.0),
        }

    def release_memory(self):
        self.release_calls += 1


class _EmptyUpstoxSource(_FakeUpstoxSource):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.series_calls = 0

    def _minute_series(self, _instrument_key, _expiry):
        self.series_calls += 1
        return {}


class PremiumTargetSelectionCacheTests(unittest.TestCase):
    def test_weekly_selection_rejects_a_distant_expiry(self):
        self.assertIsNone(
            UpstoxHistoricalPremiumSelector._expiry_for([date(2026, 3, 19)], date(2026, 3, 1), "current_week")
        )

    def test_repeat_selection_reuses_persisted_strike(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _FakeUpstoxSource),
        ):
            _FakeUpstoxSource.cache_dir = tmpdir
            leg = {"option_type": "CE", "strike_type": "premium_above", "strike_value": 250}
            entry_time = datetime(2026, 3, 18, 9, 15)

            first = UpstoxHistoricalPremiumSelector("NIFTY")
            initial = first.select(entry_time, 25010, leg, 5)
            self.assertEqual(initial.strike, 25000)
            self.assertEqual(first.cache_summary()["selection_misses"], 1)

            repeat = UpstoxHistoricalPremiumSelector("NIFTY")
            resolved = repeat.select(entry_time, 25010, leg, 5)
            self.assertEqual(resolved.strike, 25000)
            self.assertEqual(repeat.cache_summary()["selection_hits"], 1)
            self.assertEqual(repeat.cache_summary()["selection_misses"], 0)

    def test_expiry_transition_releases_parsed_raw_series(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _FakeUpstoxSource),
        ):
            _FakeUpstoxSource.cache_dir = tmpdir
            selector = UpstoxHistoricalPremiumSelector("NIFTY")
            selector._activate_expiry(date(2026, 3, 19))
            selector._frames[("contract", date(2026, 3, 19), 5)] = object()
            selector._activate_expiry(date(2026, 3, 26))
            self.assertEqual(selector._frames, {})
            self.assertEqual(selector.source.release_calls, 2)

    def test_empty_contract_series_is_negative_cached(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _EmptyUpstoxSource),
        ):
            _EmptyUpstoxSource.cache_dir = tmpdir
            selector = UpstoxHistoricalPremiumSelector("NIFTY")
            selector._frame("NSE_FO|empty", date(2026, 3, 19), 25000, 5)
            selector._frame("NSE_FO|empty", date(2026, 3, 19), 25000, 5)
            self.assertEqual(selector.source.series_calls, 1)


if __name__ == "__main__":
    unittest.main()
