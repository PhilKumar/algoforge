"""A finished paper campaign outlives the campaign that replaces it.

Phil, 2026-08-25, hours after the first live Candle Entry campaign expired:
"Where is that trade that is completed today with expiry?"

It was gone. Candle Entry, Fib Boundary and Gap Carry each keep their WHOLE
state in a single app_state row, so when the auto mother opened the next
campaign at 15:25 it OVERWROTE the one that had just settled -- mother 3 Aug,
NIFTY 24400 CE, two rungs, ~-Rs 52,891 -- and the only backup that day was
taken at 09:08, before the expiry. The settlement is not recoverable.

So a terminal campaign is now archived to its own table the moment the save
path sees it, keyed so it can never be written twice.
"""

import base64
import os
import sys
import unittest

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-ledger-test.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-ledger-test-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

# The campaign that was lost, as it stood in the 09:08 backup.
LOST_CAMPAIGN = {
    "status": "EXPIRED",
    "mother": {"timestamp": "2026-08-03T15:25:00+05:30"},
    "contract": {"underlying": "NIFTY", "strike": 24400, "option_type": "CE", "expiry": "2026-08-25"},
    "exit": {"timestamp": "2026-08-25T15:30:00+05:30", "reason": "expiry"},
    "fills": [
        {"timestamp": "2026-08-11T09:25:00+05:30", "strike": 24400, "quantity": 65, "option_premium": 309.0},
        {"timestamp": "2026-08-12T10:15:00+05:30", "strike": 24300, "quantity": 130, "option_premium": 283.6},
    ],
    "deployed_inr": 56953.0,
    "gross_pnl": -50453.0,
    "costs_total": 240.0,
    "net_pnl": -50693.0,
    "rounds": [],
}


class RowShapeTests(unittest.TestCase):
    def test_an_unfinished_campaign_is_not_archived(self):
        for state in ("OPEN", "WAITING_TWO_RED", "ARMED", "HOLDING"):
            live = dict(LOST_CAMPAIGN, status=state)
            self.assertIsNone(app_module._paper_campaign_row(live), state)

    def test_a_finished_campaign_becomes_a_row(self):
        row = app_module._paper_campaign_row(LOST_CAMPAIGN)
        self.assertIsNotNone(row)
        self.assertEqual(row["campaign_key"], "2026-08-03T15:25:00+05:30")
        self.assertEqual(row["contract"], "24400 CE")
        self.assertEqual(row["buys"], 2)
        self.assertEqual(row["net_pnl"], -50693.0)
        self.assertEqual(row["exit_reason"], "expiry")
        self.assertEqual(row["source"], "live")
        # It carries its own fills, so the row can be read back without the engine.
        self.assertEqual(len(row["payload"]["fills"]), 2)

    def test_the_key_is_the_mother_so_one_campaign_is_one_row(self):
        a = app_module._paper_campaign_row(LOST_CAMPAIGN)
        b = app_module._paper_campaign_row(dict(LOST_CAMPAIGN, net_pnl=-1.0))
        self.assertEqual(a["campaign_key"], b["campaign_key"])

    def test_a_campaign_that_never_bought_says_so(self):
        row = app_module._paper_campaign_row(dict(LOST_CAMPAIGN, fills=[], exit={}))
        self.assertEqual(row["buys"], 0)
        self.assertEqual(row["exit_reason"], "no_buy")


class LedgerWriteTests(unittest.IsolatedAsyncioTestCase):
    USER = 9931

    async def asyncSetUp(self):
        self.saved = []

        async def fake_save(user_id, strategy, row):
            key = (int(user_id), strategy, row["campaign_key"])
            existing = [i for i, (k, _r) in enumerate(self.saved) if k == key]
            if existing:
                self.saved[existing[0]] = (key, row)
                return False
            self.saved.append((key, row))
            return True

        self._orig = app_module._db_mod.save_paper_campaign
        app_module._db_mod.save_paper_campaign = fake_save
        # The "written once" guard is process-wide, so a test must start clean.
        app_module._paper_ledger_written.clear()

    async def asyncTearDown(self):
        app_module._db_mod.save_paper_campaign = self._orig

    async def test_the_same_campaign_is_never_archived_twice(self):
        """The save path runs on every poll; a campaign that ended stays ended."""
        for _ in range(5):
            await app_module._archive_paper_campaign(self.USER, "candle_entry", LOST_CAMPAIGN)
        self.assertEqual(len(self.saved), 1)

    async def test_a_late_price_corrects_the_row_in_place(self):
        await app_module._archive_paper_campaign(self.USER, "candle_entry", LOST_CAMPAIGN)
        await app_module._archive_paper_campaign(self.USER, "candle_entry", dict(LOST_CAMPAIGN, net_pnl=-52891.0))
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0][1]["net_pnl"], -52891.0)

    async def test_the_guard_stops_a_write_on_every_poll(self):
        """The save path ticks every few seconds; the ledger must not follow it.

        Unguarded, this opened a database connection per poll and Gap Carry
        re-wrote every settled night each time -- enough to time the Playwright
        suite out.
        """
        await app_module._archive_paper_campaign(self.USER, "candle_entry", LOST_CAMPAIGN)
        calls = len(self.saved)
        for _ in range(20):
            await app_module._archive_paper_campaign(self.USER, "candle_entry", LOST_CAMPAIGN)
        self.assertEqual(len(self.saved), calls)

    async def test_gap_carry_does_not_rewrite_settled_nights_every_save(self):
        status = {
            "status": "HOLDING",
            "history": [
                {
                    "session": "2026-08-25",
                    "side": "CE",
                    "strike": 24050,
                    "net": 1200.0,
                    "entry": {"at": "x"},
                    "exit": {"at": "y"},
                }
            ],
        }
        for _ in range(10):
            await app_module._archive_gap_carry_nights(self.USER, status)
        self.assertEqual(len(self.saved), 1)

    async def test_an_open_campaign_writes_nothing(self):
        await app_module._archive_paper_campaign(self.USER, "candle_entry", dict(LOST_CAMPAIGN, status="OPEN"))
        self.assertEqual(self.saved, [])

    async def test_a_ledger_failure_never_breaks_the_state_save(self):
        async def boom(*_a, **_k):
            raise RuntimeError("disk full")

        app_module._db_mod.save_paper_campaign = boom
        await app_module._archive_paper_campaign(self.USER, "candle_entry", LOST_CAMPAIGN)

    async def test_gap_carry_archives_each_closed_night(self):
        status = {
            "status": "HOLDING",
            "history": [
                {
                    "session": "2026-08-25",
                    "side": "CE",
                    "strike": 24050,
                    "net": 1200.0,
                    "entry": {"at": "2026-08-25T15:10:00+05:30"},
                    "exit": {"at": "2026-08-26T09:20:00+05:30"},
                },
                {
                    "session": "2026-08-26",
                    "side": "CE",
                    "strike": 24100,
                    "net": -800.0,
                    "entry": {"at": "2026-08-26T15:10:00+05:30"},
                    "exit": {"at": "2026-08-27T09:20:00+05:30"},
                },
            ],
        }
        await app_module._archive_gap_carry_nights(self.USER, status)
        self.assertEqual(len(self.saved), 2)
        self.assertEqual({k[2] for k, _ in self.saved}, {"2026-08-25", "2026-08-26"})


if __name__ == "__main__":
    unittest.main()
