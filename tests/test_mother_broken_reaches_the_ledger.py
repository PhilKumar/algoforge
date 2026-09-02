"""A campaign that ended because its mother broke is still a campaign.

Phil, 2026-09-02: "The trade taken today was also not in the chart marked."
It was not in the ledger either. The auto log had it --

    09:15  buys 0  Rs 0.00     mother_broken_no_buys
    10:30  buys 1  Rs 649.98   mother_broken        <- a real trade
    14:30  buys 0  Rs 0.00     mother_broken_no_buys

-- while paper_campaigns held only two rows for those two days, both of them
campaigns that ended `intraday_close`. The 10:30 one, the only one that bought
anything, was nowhere: no row, no chart, no record that it happened.

The cause was two lists of terminal states that disagreed. The engine has said
MOTHER_BROKEN is terminal since it was written:

    engine/fib_touch_ladder.py:144
    TERMINAL_STATUSES = frozenset({"CLOSED", "EXPIRED", "KILLED", "MOTHER_BROKEN"})

The ledger kept its own hand-written copy and left MOTHER_BROKEN out, so
`_fib_boundary_campaign_row` returned None for every one of them and the
archive quietly did nothing. app.py had even imported the engine's list
already, at line 154, and did not use it.

These tests pin the agreement rather than the literal set, because the bug was
never a wrong value -- it was two sources for one fact.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PHILFORGE_PIN", "test-pin-not-real")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("PHILFORGE_STARTUP_SCRIP_MASTER", "0")
os.environ.setdefault("PHILFORGE_STARTUP_ENGINE_RESTORE", "0")
os.environ.setdefault("DHAN_CLIENT_ID", "dummy")
os.environ.setdefault("DHAN_ACCESS_TOKEN", "dummy")

import app as app_module  # noqa: E402
from engine.fib_touch_ladder import TERMINAL_STATUSES  # noqa: E402


class TheLedgerAgreesWithTheEngine(unittest.TestCase):
    def test_every_state_the_engine_calls_terminal_reaches_the_ledger(self):
        missing = set(TERMINAL_STATUSES) - set(app_module._PAPER_TERMINAL_STATES)
        self.assertEqual(missing, set(), f"the engine ends campaigns in {missing} and the ledger would drop them")

    def test_mother_broken_in_particular(self):
        """The one that was missing, and the one that cost a real trade."""
        self.assertIn("MOTHER_BROKEN", app_module._PAPER_TERMINAL_STATES)

    def test_the_ledger_is_derived_not_retyped(self):
        """A second hand-written copy is how this happened in the first place."""
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("_PAPER_TERMINAL_STATES = set(_FIB_TOUCH_TERMINAL_STATUSES)", src)

    def test_the_ledger_keeps_the_states_the_engine_does_not_know(self):
        for state in ("ABANDONED", "RECOVERED"):
            self.assertIn(state, app_module._PAPER_TERMINAL_STATES)


class ARowIsBuiltForAMotherBrokenCampaign(unittest.TestCase):
    def _status(self, state, buys):
        fills = [{"strike": 24000, "expiry": "2026-09-04", "option_type": "CE", "quantity": 65} for _ in range(buys)]
        return {
            "status": state,
            "mother_timestamp": "2026-09-02T10:30:00+05:30",
            "exit_timestamp": "2026-09-02T14:32:00+05:30",
            "exit_reason": "mother_broken",
            "symbol": "NIFTY",
            "side": "CE",
            "timeframe": "5m",
            "buy_mode": "levels",
            "rounds": [{"net_pnl": 649.98, "fills": fills}] if buys else [],
            "fills": [],
            "net_pnl": 649.98 if buys else None,
        }

    def test_a_mother_broken_campaign_that_bought_is_archived(self):
        row = app_module._fib_boundary_campaign_row(self._status("MOTHER_BROKEN", 1))
        self.assertIsNotNone(row, "the trade that made Rs 649.98 must reach the ledger")
        self.assertEqual(row["buys"], 1)
        self.assertEqual(row["net_pnl"], 649.98)

    def test_it_carries_what_the_chart_needs(self):
        """No mother in the payload means no chart button on the row."""
        row = app_module._fib_boundary_campaign_row(self._status("MOTHER_BROKEN", 1))
        chart = row["payload"]["chart"]
        self.assertEqual(chart["mother_timestamp"], "2026-09-02T10:30:00+05:30")
        self.assertEqual(chart["timeframe"], "5m")

    def test_a_still_running_campaign_is_not_archived(self):
        self.assertIsNone(app_module._fib_boundary_campaign_row(self._status("RUNNING", 1)))


if __name__ == "__main__":
    unittest.main()
