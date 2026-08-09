"""What the option-candle cache is allowed to skip.

The cache was written on every replay and read back on none of them. The test
that decided whether it covered the requested range asked for a candle at or
before midnight of the first day and one at or after 23:59 of the last:

    cached_raw.index.min() <= from_dt
    cached_raw.index.max() >= (to_dt + timedelta(hours=23, minutes=59))

Option candles run 09:15 to 15:30, so neither can ever be true. The answer was
always "not covered", and every run re-downloaded, one 30-day chunk at a time,
a range already sitting on disk in full — thirteen Dhan calls per leg for a
year, which is the wait.

Coverage is now read off the cached index. That buys speed by SKIPPING network
calls, so the thing these tests guard hardest is the other direction: a hole in
the cache must never be mistaken for coverage, because the cost of that is a
backtest quietly priced on candles that were never fetched.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _sessions(first: str, last: str, freq: str = "5min") -> pd.DataFrame:
    """Candles at 09:15–15:25 on every weekday in the range — close enough to a
    real option series for coverage arithmetic."""
    stamps = []
    for day in pd.date_range(first, last, freq="D"):
        if day.weekday() >= 5:
            continue
        stamps.extend(pd.date_range(f"{day.date()} 09:15", f"{day.date()} 15:25", freq=freq))
    return pd.DataFrame({"close": 1.0}, index=pd.DatetimeIndex(stamps))


def _dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d")


class OptionHistoryGapTests(unittest.TestCase):
    def test_the_old_cover_test_could_never_pass(self):
        """Pinned as the reason this code changed. A full year on disk failed
        both halves of the check that was supposed to spare the download."""
        cached = _sessions("2025-01-01", "2025-12-31")
        from_dt, to_dt = _dt("2025-01-01"), _dt("2025-12-31")
        self.assertFalse(cached.index.min() <= from_dt, "first candle is 09:15, not midnight")
        self.assertFalse(cached.index.max() >= to_dt + timedelta(hours=23, minutes=59), "last candle is 15:25")

    def test_a_fully_cached_range_costs_no_network_calls(self):
        cached = _sessions("2025-01-01", "2025-12-31")
        gaps = app_module._option_history_gaps(cached, _dt("2025-02-01"), _dt("2025-11-30"))
        self.assertEqual(gaps, [])
        self.assertEqual(app_module._option_history_chunks(gaps), [])

    def test_an_empty_cache_fetches_the_whole_range(self):
        for empty in (None, pd.DataFrame()):
            with self.subTest(empty=type(empty).__name__):
                gaps = app_module._option_history_gaps(empty, _dt("2025-01-01"), _dt("2025-06-30"))
                self.assertEqual(gaps, [(_dt("2025-01-01"), _dt("2025-06-30"))])

    def test_only_the_missing_tail_is_fetched(self):
        """The run that stalled is the common case: some months landed, the
        rest did not. Re-downloading the months already held is the bug."""
        cached = _sessions("2025-01-01", "2025-03-31")
        gaps = app_module._option_history_gaps(cached, _dt("2025-01-01"), _dt("2025-06-30"))
        self.assertEqual(len(gaps), 1)
        start, end = gaps[0]
        self.assertEqual(end, _dt("2025-06-30"))
        self.assertLessEqual(start, _dt("2025-03-31"), "the last cached day may be partial, so re-fetch it")
        self.assertGreaterEqual(start, _dt("2025-03-28"), "but do not re-fetch the months already held")

    def test_only_the_missing_head_is_fetched(self):
        cached = _sessions("2025-04-01", "2025-06-30")
        gaps = app_module._option_history_gaps(cached, _dt("2025-01-01"), _dt("2025-06-30"))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0], _dt("2025-01-01"))
        self.assertLessEqual(gaps[0][1], _dt("2025-04-01"))

    def test_a_hole_in_the_middle_is_never_treated_as_covered(self):
        """Two disjoint runs leave a gap between them. Reading coverage as
        "earliest candle to latest candle" would price a backtest on months
        that were never downloaded — worse than the slow fetch it replaces."""
        cached = pd.concat([_sessions("2025-01-01", "2025-01-31"), _sessions("2025-05-01", "2025-05-31")])
        gaps = app_module._option_history_gaps(cached, _dt("2025-01-01"), _dt("2025-05-31"))
        self.assertTrue(gaps, "February to April is missing and must be fetched")
        covered_by_gap = any(start <= _dt("2025-03-15") <= end for start, end in gaps)
        self.assertTrue(covered_by_gap, f"March is missing from disk but no gap covers it: {gaps}")

    def test_a_long_weekend_is_not_a_hole(self):
        """Holidays on a Friday and the following Monday put five calendar days
        between sessions. Calling that a gap would re-fetch a month every run
        and undo the whole point."""
        cached = pd.concat([_sessions("2025-01-01", "2025-03-13"), _sessions("2025-03-19", "2025-06-30")])
        gaps = app_module._option_history_gaps(cached, _dt("2025-01-01"), _dt("2025-06-30"))
        self.assertEqual(gaps, [], f"a closed exchange is not missing data: {gaps}")

    def test_a_missing_month_is_wider_than_any_exchange_holiday(self):
        """The rule separating the two cases only works if a real gap is bigger
        than the widest shutdown. A 30-day chunk is."""
        self.assertLess(app_module._MAX_SESSION_GAP_DAYS, app_module.ROLLING_OPTION_CHUNK_DAYS)


class OptionHistoryChunkTests(unittest.TestCase):
    def test_chunks_tile_the_gap_without_overlap_or_holes(self):
        gaps = [(_dt("2025-01-01"), _dt("2025-06-30"))]
        chunks = app_module._option_history_chunks(gaps, chunk_days=30)
        self.assertEqual(chunks[0][0], _dt("2025-01-01"))
        for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
            self.assertEqual(prev_end, next_start, "a skipped day is a month of missing candles")
        self.assertGreaterEqual(chunks[-1][1], _dt("2025-06-30"))

    def test_a_year_is_thirteen_calls_and_a_cached_year_is_none(self):
        """The number the user waits on."""
        full_year = app_module._option_history_chunks([(_dt("2025-01-01"), _dt("2025-12-31"))])
        self.assertEqual(len(full_year), 13)
        cached = _sessions("2025-01-01", "2025-12-31")
        gaps = app_module._option_history_gaps(cached, _dt("2025-01-01"), _dt("2025-12-31"))
        self.assertEqual(len(app_module._option_history_chunks(gaps)), 0)

    def test_each_chunk_is_capped_at_the_api_limit(self):
        chunks = app_module._option_history_chunks([(_dt("2025-01-01"), _dt("2025-12-31"))], chunk_days=30)
        for start, end in chunks:
            self.assertLessEqual((end - start).days, 30)


class OptionHistoryMergeTests(unittest.TestCase):
    def test_re_fetching_the_last_cached_day_is_harmless(self):
        cached = _sessions("2025-01-01", "2025-01-31")
        overlap = _sessions("2025-01-31", "2025-02-28")
        merged = app_module._concat_option_history([cached, overlap])
        self.assertTrue(merged.index.is_monotonic_increasing)
        self.assertFalse(merged.index.duplicated().any())
        self.assertEqual(merged.index.min(), cached.index.min())
        self.assertEqual(merged.index.max(), overlap.index.max())


class ProgressReportingTests(unittest.TestCase):
    def test_a_broken_progress_sink_cannot_fail_the_replay(self):
        def explode(_text):
            raise RuntimeError("no")

        token = app_module._backtest_stage.set(explode)
        try:
            app_module._say_stage("month 3 of 13")  # must not raise
        finally:
            app_module._backtest_stage.reset(token)

    def test_progress_reaches_the_job_when_a_sink_is_set(self):
        seen = []
        token = app_module._backtest_stage.set(seen.append)
        try:
            app_module._say_stage("month 3 of 13")
        finally:
            app_module._backtest_stage.reset(token)
        self.assertEqual(seen, ["month 3 of 13"])

    def test_no_sink_is_the_normal_case_and_is_silent(self):
        """The plain /api/backtest route runs with no job attached."""
        self.assertIsNone(app_module._backtest_stage.get())
        app_module._say_stage("month 3 of 13")


if __name__ == "__main__":
    unittest.main()
