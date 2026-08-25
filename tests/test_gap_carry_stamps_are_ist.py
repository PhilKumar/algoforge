"""A Gap Carry stamp is IST-aware, so its marker lands on its candle.

Phil, 2026-08-25, on the chart of the night it bought: "The buy arrow is
somewhere where nothing is on the screen".

`datetime.combine` builds a NAIVE datetime and prod runs on a UTC box, so
`.timestamp()` read a naive 15:10 as 15:10 UTC — 19,800 seconds late. The
chart plots markers on an epoch axis against timezone-AWARE candles and
extrapolates past the last one at a bar per `barSec`, so a 5m chart put the
arrow 66 bars beyond the final candle, out in the empty right-hand margin.

This pins the invariant that actually matters: the stamp a position carries
must equal the epoch of the candle it was read from.
"""

import os
import sys
import unittest
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import gap_carry, gap_carry_paper  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class StampsAreAwareTests(unittest.TestCase):
    def test_both_modules_stamp_naive_wall_clock_as_ist(self):
        naive = datetime(2026, 8, 25, 15, 10)
        for name, fn in (("backtest", gap_carry._ist), ("paper", gap_carry_paper._ist)):
            stamped = fn(naive)
            self.assertIsNotNone(stamped.tzinfo, f"{name} left the stamp naive")
            self.assertEqual(stamped.utcoffset().total_seconds(), 5.5 * 3600, name)

    def test_an_aware_stamp_is_left_alone(self):
        aware = datetime(2026, 8, 25, 9, 20, tzinfo=IST)
        for fn in (gap_carry._ist, gap_carry_paper._ist):
            self.assertIs(fn(aware), aware)

    def test_the_stamp_matches_its_candles_epoch(self):
        """The marker's x comes from this number; the candle's comes from that."""
        candle_ts = datetime(2026, 8, 25, 15, 10, tzinfo=IST)
        stamped = gap_carry_paper._ist(datetime.combine(date(2026, 8, 25), time(15, 10)))
        self.assertEqual(
            int(stamped.timestamp()),
            int(candle_ts.timestamp()),
            "the buy would be drawn away from the candle it was read on",
        )

    def test_a_naive_stamp_on_a_utc_box_really_is_5h30_late(self):
        """Why this matters — the failure the fix removes."""
        naive = datetime(2026, 8, 25, 15, 10)
        as_utc = naive.replace(tzinfo=timezone.utc)
        candle = datetime(2026, 8, 25, 15, 10, tzinfo=IST)
        self.assertEqual(int(as_utc.timestamp()) - int(candle.timestamp()), 19800)

    def test_a_position_saved_before_the_fix_is_repaired_on_restore(self):
        """The open position on disk is naive; restoring it must not keep it so.

        Without this the deploy fixes only the NEXT trade, and the arrow Phil is
        looking at right now stays out in the margin until the position closes.
        """
        candle = datetime(2026, 8, 25, 15, 10, tzinfo=IST)
        restored = gap_carry_paper._as_datetime("2026-08-25T15:10:00")
        self.assertIsNotNone(restored.tzinfo, "a stamp from disk came back naive")
        self.assertEqual(int(restored.timestamp()), int(candle.timestamp()))

    def test_restoring_does_not_shift_a_stamp_that_already_has_an_offset(self):
        """Once written aware, a round trip through disk must be lossless."""
        for text in ("2026-08-25T15:10:00+05:30", "2026-08-25T09:40:00+00:00"):
            restored = gap_carry_paper._as_datetime(text)
            self.assertEqual(restored, datetime.fromisoformat(text), text)


if __name__ == "__main__":
    unittest.main()
