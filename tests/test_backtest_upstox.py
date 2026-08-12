import gc
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


class PremiumTargetSelectionCacheTests(unittest.TestCase):
    def test_current_week_does_not_jump_across_a_missing_expiry(self):
        selected = UpstoxHistoricalPremiumSelector._expiry_for(
            [date(2026, 3, 19)],
            date(2026, 3, 1),
            "current_week",
        )

        self.assertIsNone(selected)

    def test_selected_frame_cache_does_not_pin_closed_contract_history(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _FakeUpstoxSource),
        ):
            _FakeUpstoxSource.cache_dir = tmpdir
            selector = UpstoxHistoricalPremiumSelector("NIFTY")
            expiry = date(2026, 3, 19)
            frame = selector._frame("contract", expiry, 25000, 5)

            self.assertTrue(selector._frames)
            del frame
            gc.collect()

            self.assertFalse(selector._frames)

    def test_expiry_transition_releases_parsed_contract_frames(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _FakeUpstoxSource),
        ):
            _FakeUpstoxSource.cache_dir = tmpdir
            selector = UpstoxHistoricalPremiumSelector("NIFTY")
            selector._activate_expiry(date(2026, 3, 19))
            frame = selector._frame("contract", date(2026, 3, 19), 25000, 5)
            self.assertTrue(selector._frames)

            selector._activate_expiry(date(2026, 3, 26))

            self.assertFalse(selector._frames)
            self.assertEqual(selector.source.release_calls, 2)

    def test_progress_callback_is_accepted_and_reports_resolution(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _FakeUpstoxSource),
        ):
            _FakeUpstoxSource.cache_dir = tmpdir
            messages = []
            selector = UpstoxHistoricalPremiumSelector("NIFTY", progress=messages.append)

            selection = selector.select(
                datetime(2026, 3, 18, 9, 15),
                25010,
                {"option_type": "CE", "strike_type": "premium_above", "strike_value": 250},
                5,
            )

            self.assertIsNotNone(selection)
            self.assertTrue(messages)
            self.assertIn("Resolving real Upstox options", messages[0])
            self.assertIn("expiry 2026-03-19", messages[0])

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

    def test_cache_summary_reports_expiry_coverage(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("data.backtest_upstox.UpstoxPremiumSource", _FakeUpstoxSource),
        ):
            _FakeUpstoxSource.cache_dir = tmpdir
            selector = UpstoxHistoricalPremiumSelector("NIFTY")

            summary = selector.cache_summary()

            self.assertEqual(summary["first_expiry"], "2026-03-19")
            self.assertEqual(summary["last_expiry"], "2026-03-19")


if __name__ == "__main__":
    unittest.main()
