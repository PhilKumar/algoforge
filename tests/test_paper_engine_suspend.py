"""A deploy must not end a paper trade.

Phil, 2026-08-17. A deploy restarted the app at 09:58 while his PE run was
holding a position. The shutdown handler called `engine.stop()`, which force
-closed the position at the last quote it happened to hold, booked it as
ENGINE_STOP, and spent the day's single allowed entry. The trade was ended by a
deploy, not by his strategy.

The live engine has always got this right -- its `stop()` leaves positions alone
and `_restore_live_engines` picks them up again. Paper has the same state file
and the same restore; it only lacked a way to be told "the process is going
away" as distinct from "the user pressed Stop".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.paper_trading import PaperTradingEngine  # noqa: E402


def _engine_holding_one():
    engine = PaperTradingEngine.__new__(PaperTradingEngine)
    engine.running = True
    engine.positions = [
        {
            "underlying": "NIFTY",
            "strike": 24550,
            "option_type": "PE",
            "entry_premium": 242.29,
            "current_premium": 256.19,
            "qty": 260,
            "lots": 4,
        }
    ]
    engine.closed_trades = []
    engine.events = []
    engine.trades_today = 1
    engine._state_file = None
    engine.log_event = lambda kind, message, **kw: engine.events.append((kind, message))
    engine._save_state = lambda: engine.events.append(("saved", "state"))
    engine._close_position = lambda pos, reason, px: (
        engine.closed_trades.append({"reason": reason, "exit": px, "pnl": (px - pos["entry_premium"]) * pos["qty"]}),
        engine.positions.remove(pos),
    )
    return engine


class SuspendTests(unittest.TestCase):
    def test_a_restart_keeps_the_position_and_books_nothing(self):
        engine = _engine_holding_one()
        engine.stop(close_positions=False)
        self.assertFalse(engine.running, "the run is quiet")
        self.assertEqual(len(engine.positions), 1, "the position is still on")
        self.assertEqual(engine.closed_trades, [], "and nothing was booked")

    def test_the_suspended_run_saves_its_state_so_it_can_come_back(self):
        engine = _engine_holding_one()
        engine.stop(close_positions=False)
        self.assertIn(("saved", "state"), engine.events)

    def test_it_says_out_loud_that_it_will_resume(self):
        engine = _engine_holding_one()
        engine.stop(close_positions=False)
        said = " ".join(message for _kind, message in engine.events)
        self.assertIn("Suspended", said)
        self.assertIn("resuming after restart", said)
        self.assertNotIn("Force closing", said)

    def test_the_user_pressing_stop_still_flattens(self):
        """The other half of the rule: a deliberate Stop means get me out."""
        engine = _engine_holding_one()
        engine.stop()
        self.assertEqual(engine.positions, [])
        self.assertEqual(len(engine.closed_trades), 1)
        self.assertEqual(engine.closed_trades[0]["reason"], "ENGINE_STOP")

    def test_closing_is_still_the_default(self):
        """So no caller gets the new behaviour by accident."""
        engine = _engine_holding_one()
        engine.stop()
        self.assertEqual(engine.positions, [])


if __name__ == "__main__":
    unittest.main()
