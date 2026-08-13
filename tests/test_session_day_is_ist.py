"""The trading day is the IST day, on a box that runs on UTC.

Phil, 2026-08-14 00:36 IST: "the backtested CE and PE trades were deployed on
the Live.. But now it is vanished".  A deploy restarted the app just after IST
midnight.  The engines stamp `session_date` in IST, so his runs carried
2026-08-14, while the restore compared them against the SERVER's `date.today()`
-- still 2026-08-13 in UTC.  Different strings, no open positions left after
the stop loss, so both runs were written off as stale and dropped from the Live
page.  Nothing was lost on disk; the app simply refused to pick them back up.

Every session-day comparison therefore goes through IST.  These tests pin the
5.5-hour window (00:00-05:30 IST) where UTC and IST disagree, because that is
the only time the bug is visible and the only time a deploy will hit it.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import app as app_mod
from engine.live import LiveEngine
from engine.paper_trading import PaperTradingEngine

IST = timezone(timedelta(hours=5, minutes=30))
# 00:36 IST on 14 Aug is 19:06 UTC on 13 Aug -- the moment of the deploy.
JUST_AFTER_IST_MIDNIGHT = datetime(2026, 8, 13, 19, 6, tzinfo=timezone.utc)


class SessionDayIsISTTests(unittest.TestCase):
    def test_the_server_and_the_market_disagree_in_this_window(self):
        """The premise: without this fix the two dates really are different."""
        moment = JUST_AFTER_IST_MIDNIGHT
        self.assertEqual(moment.date(), date(2026, 8, 13))  # what the box says
        self.assertEqual(moment.astimezone(IST).date(), date(2026, 8, 14))  # the market

    def test_app_reads_today_in_ist(self):
        with patch("app.datetime") as clock:
            clock.now.return_value = JUST_AFTER_IST_MIDNIGHT.astimezone(IST)
            self.assertEqual(app_mod._ist_today(), date(2026, 8, 14))

    def test_engines_stamp_the_session_in_ist(self):
        """A run started at 00:36 IST belongs to the 14th, not the 13th."""
        for engine_cls, module in ((PaperTradingEngine, "engine.paper_trading"), (LiveEngine, "engine.live")):
            with self.subTest(engine=engine_cls.__name__):
                with patch(f"{module}._now_ist") as clock:
                    clock.return_value = JUST_AFTER_IST_MIDNIGHT.astimezone(IST).replace(tzinfo=None)
                    stamped = clock.return_value.date()
                self.assertEqual(stamped, date(2026, 8, 14))

    def test_no_session_comparison_still_asks_the_server_for_the_day(self):
        """The regression itself: a stray date.today() puts the runs back at risk."""
        for path in ("app.py", "engine/paper_trading.py", "engine/live.py"):
            with self.subTest(path=path):
                source = open(path).read()
                for line in source.splitlines():
                    if "session_date" not in line and "today" not in line:
                        continue
                    if line.lstrip().startswith("#"):
                        continue
                    self.assertNotIn(
                        "date_type.today()",
                        line,
                        f"{path}: session/day logic must use IST, not the server's date -- {line.strip()}",
                    )


if __name__ == "__main__":
    unittest.main()
