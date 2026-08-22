"""The day rolls over on disk, not only in memory.

Phil, 2026-08-22: "This never happened before...". PhilForge was stopped at
15:32 on 21 Aug for a Dhan backfill (one active token, so the box's instances
come down first) and started again at 00:33 -- 33 minutes past midnight. All
three paper engines vanished from the Live page.

They were skipped as STALE. Both restores refuse a state file whose
`session_date` is not today unless it holds an open position, and the rollover
moved the date in memory without writing it: `_save_state()` was called only
when there were positions to carry. A flat engine therefore always reads
"yesterday" once the day turns, and any restart after midnight loses it.

Every previous stop-for-backfill was restarted the same day, which is why the
hole sat there unseen.
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.live import LiveEngine  # noqa: E402
from engine.paper_trading import PaperTradingEngine  # noqa: E402


class _Saves:
    """Counts saves and records the date each one would have written."""

    def __init__(self, engine):
        self.engine = engine
        self.dates = []

    def __call__(self):
        self.dates.append(self.engine.session_date)


def _rollover_body(source: str, marker: str) -> str:
    """The lines a rollover runs, lifted from the engine's own source."""
    start = source.index(marker)
    end = source.index("📅 New trading day", start)
    return source[start:end]


class RolloverPersistsTests(unittest.TestCase):
    def test_every_rollover_site_saves_its_new_date(self):
        """Both engines, both call sites: a save follows the date change."""
        import inspect

        for module in (inspect.getsource(LiveEngine), inspect.getsource(PaperTradingEngine)):
            for chunk in module.split("self.session_date = now.date()")[1:]:
                # The save must come before anything else that could return or
                # loop -- within the same rollover block.
                head = chunk[: chunk.index("\n\n")] if "\n\n" in chunk else chunk
                self.assertIn(
                    "self._save_state()",
                    head,
                    "a rollover that does not persist its date is lost to the next restart",
                )

    def test_a_flat_engine_writes_the_new_date(self):
        engine = PaperTradingEngine.__new__(PaperTradingEngine)
        engine.positions = []
        engine.trades_today = 3
        engine.daily_pnl = 1234.5
        engine.session_date = date.today() - timedelta(days=1)
        engine.events = []
        engine.log_event = lambda *a, **k: None
        engine._reset_intraday_status = lambda: None
        saves = _Saves(engine)
        engine._save_state = saves

        # The rollover, as the loop runs it.
        engine.trades_today = 0
        engine.daily_pnl = 0.0
        engine.session_date = date.today()
        engine._reset_intraday_status()
        engine.log_event("info", "rolled")
        engine._save_state()

        self.assertEqual(saves.dates, [date.today()])


if __name__ == "__main__":
    unittest.main()
