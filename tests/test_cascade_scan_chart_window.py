"""The scanner's chart must contain the high the row was ranked on.

Phil, 2026-08-12, on LODHA: *"15m not showing the 1D high from where market fell
hence I am unable to capture the correct 15m candle. I need candles at least from
that high and some candles before as well."*

The row said -11.0% off the 20-session high. The 15m chart was a blind
`bars[-90:]` tail, and ninety 15m bars is under four NSE sessions, so a high set
three weeks earlier was simply not on it -- and the mother candle, the only
reason to open that chart, could not be picked from it.

These drive `engine.cascade_scanner.chart_window`, the function the endpoint
calls, not a copy of its arithmetic.
"""

import unittest
from datetime import datetime, timedelta

from engine.cascade_scanner import CHART_LEAD_BARS, CHART_MAX_BARS, chart_window


class Bar:
    """Only what chart_window reads: a timestamp and a high."""

    def __init__(self, timestamp: datetime, high: float) -> None:
        self.timestamp = timestamp
        self.high = high


def session_bars(sessions: int, peak_session: int, peak: float = 1300.0, per_session: int = 25):
    """`sessions` NSE days of 15m bars, with one session carrying the high.

    The peak prints on the 11th bar of its session, not the first, because that
    is what makes the difference between "lead before the high's session" and
    "lead before the high itself" observable.
    """
    start = datetime(2026, 7, 14, 9, 15)
    fine, daily = [], []
    for index in range(sessions):
        day = start + timedelta(days=index)
        high = peak if index == peak_session else 1200.0 + index
        for slot in range(per_session):
            fine.append(Bar(day + timedelta(minutes=15 * slot), high if slot == 10 else high - 20))
        daily.append(Bar(day, high))
    return fine, daily


class ScanChartWindowTests(unittest.TestCase):
    def setUp(self):
        # 22 sessions; the high sits in session 2, inside the 20-session daily
        # lookback the ranking uses and three weeks of 15m bars back.
        self.fine, self.daily = session_bars(22, peak_session=2)
        self.high_date = self.daily[2].timestamp.date()

    def test_the_old_blind_tail_missed_the_high_entirely(self):
        """The bug, stated as a test: this is what the page used to draw."""
        self.assertNotIn(1300.0, [bar.high for bar in self.fine[-90:]])

    def test_the_window_reaches_the_high(self):
        selected, in_view = chart_window(self.fine, self.high_date, minimum=90)
        self.assertTrue(in_view)
        self.assertIn(1300.0, [bar.high for bar in selected])

    def test_there_are_candles_before_the_high_as_well(self):
        """'and some candles before as well' -- a high on the left edge tells
        you nothing about what led into it."""
        selected, _ = chart_window(self.fine, self.high_date, minimum=90)
        peak_at = next(i for i, bar in enumerate(selected) if bar.high == 1300.0)
        # The lead is counted from the first bar of the high's SESSION, so there
        # are CHART_LEAD_BARS before that session plus the ten bars of it that
        # printed before the peak.
        self.assertEqual(peak_at, CHART_LEAD_BARS + 10)

    def test_a_high_made_this_morning_still_draws_a_full_chart(self):
        """Otherwise the window would collapse to the lead — thirty bars."""
        fine, daily = session_bars(22, peak_session=21)
        selected, in_view = chart_window(fine, daily[21].timestamp.date(), minimum=90)
        self.assertTrue(in_view)
        self.assertGreaterEqual(len(selected), 90)

    def test_a_high_older_than_the_fetched_candles_is_declared_not_hidden(self):
        """THE BUG A FIRST DRAFT SHIPPED WITH.

        `>= high_date` matches the very FIRST bar when the high predates the
        whole series, so deciding `high_in_view` inside that branch reported a
        complete window while the high was nowhere in it. It is decided once, at
        the end, on the bars actually being returned.
        """
        older = self.daily[0].timestamp.date() - timedelta(days=180)
        selected, in_view = chart_window(self.fine, older, minimum=90)
        self.assertFalse(in_view)
        # Everything available is still drawn -- withholding bars helps nobody.
        self.assertEqual(len(selected), len(self.fine))

    def test_the_payload_is_capped_however_far_back_the_high_is(self):
        fine, daily = session_bars(80, peak_session=0)
        selected, _ = chart_window(fine, daily[0].timestamp.date(), minimum=90)
        self.assertEqual(len(selected), CHART_MAX_BARS)

    def test_no_candles_is_not_a_crash(self):
        self.assertEqual(chart_window([], self.high_date, minimum=90), ([], False))


if __name__ == "__main__":
    unittest.main()
