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

    async def test_gap_carry_reads_the_shape_the_engine_reports(self):
        """The real 25-Aug night, exactly as get_status() hands it over.

        A position is persisted FLAT (entry_premium, exit_timestamp, charges) and
        reported NESTED (entry.premium, exit.timestamp, charges). Reading one
        with the other's names fails silently -- the money still lands, so the
        row looks right while closed_at and the charges come back empty, which
        is what happened to this night the first time it was archived.
        """
        night = {
            "session": "2026-08-25",
            "side": "CE",
            "strike": 24050,
            "expiry": "2026-09-01",
            "lots": 1,
            "lot_size": 65,
            "quantity": 65,
            "entry": {
                "timestamp": "2026-08-25T15:10:00+05:30",
                "spot": 24260.15,
                "premium": 332.7,
                "capital": 21625.5,
            },
            "exit": {
                "timestamp": "2026-08-26T09:20:18.504506+05:30",
                "spot": 24260.15,
                "premium": 381.25,
                "reason": "MORNING_EXIT",
                "priced": True,
            },
            "charges": 92.43,
            "gross": 3155.75,
            "net": 3063.32,
            "open": False,
        }
        await app_module._archive_gap_carry_nights(self.USER, {"status": "CLOSED", "history": [night]})
        self.assertEqual(len(self.saved), 1)
        row = self.saved[0][1]
        self.assertEqual(row["net_pnl"], 3063.32)
        self.assertEqual(row["gross_pnl"], 3155.75)
        # The three that came back empty when the names were wrong:
        self.assertEqual(row["closed_at"], "2026-08-26T09:20:18.504506+05:30")
        self.assertEqual(row["costs_total"], 92.43)
        self.assertEqual(row["opened_at"], "2026-08-25T15:10:00+05:30")
        self.assertEqual(row["deployed_inr"], 21625.5)
        self.assertEqual(row["exit_reason"], "MORNING_EXIT")

    async def test_a_night_settled_at_intrinsic_says_so(self):
        night = {
            "session": "2026-08-27",
            "side": "CE",
            "strike": 24100,
            "lots": 1,
            "entry": {"timestamp": "2026-08-27T15:10:00+05:30", "premium": 100.0, "capital": 6500.0},
            "exit": {"timestamp": "2026-08-28T09:20:00+05:30", "premium": 40.0, "reason": None, "priced": False},
            "charges": 12.0,
            "gross": -3900.0,
            "net": -3912.0,
        }
        await app_module._archive_gap_carry_nights(self.USER, {"status": "CLOSED", "history": [night]})
        self.assertEqual(self.saved[0][1]["exit_reason"], "at intrinsic")

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


# The Fib Boundary ladder as it ACTUALLY reports itself -- flat keys, banked
# rounds, and the money in the rounds rather than a nested exit. Lifted from
# the 26-Aug NIFTY campaign on prod that the ledger silently dropped.
FIB_LADDER_STATUS = {
    "status": "CLOSED",
    "symbol": "NIFTY",
    "mother_timestamp": "2026-08-26T09:15:00+05:30",
    "exit_timestamp": "2026-08-26T15:15:00+05:30",
    "exit_reason": "intraday_close",
    "deployed_inr": 47334.0,
    "gross_pnl": -120.25,
    "costs_total": 202.79,
    "net_pnl": -323.04,
    "fills": [],
    "rounds": [
        {
            "round": 1,
            "gross_pnl": -120.25,
            "costs_total": 202.79,
            "net_pnl": -323.04,
            "deployed_inr": 47334.0,
            "exit_timestamp": "2026-08-26T15:15:00+05:30",
            "exit_reason": "intraday_close",
            "fills": [
                {
                    "timestamp": "2026-08-26T10:22:00+05:30",
                    "strike": 24200.0,
                    "option_type": "CE",
                    "quantity": 65,
                    "premium": 231.55,
                },
                {
                    "timestamp": "2026-08-26T13:57:00+05:30",
                    "strike": 24200.0,
                    "option_type": "CE",
                    "quantity": 130,
                    "premium": 245.9,
                },
            ],
        }
    ],
}


class FibBoundaryRowTests(unittest.TestCase):
    """The generic builder read an empty `mother` here and archived nothing."""

    def test_the_generic_builder_cannot_read_a_ladder(self):
        # Proof of the bug this shape caused: no mother{}, so no key, so no row.
        self.assertIsNone(app_module._paper_campaign_row(FIB_LADDER_STATUS))

    def test_a_closed_ladder_becomes_a_row(self):
        row = app_module._fib_boundary_campaign_row(FIB_LADDER_STATUS)
        self.assertIsNotNone(row)
        self.assertEqual(row["campaign_key"], "2026-08-26T09:15:00+05:30")
        self.assertEqual(row["symbol"], "NIFTY")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["exit_reason"], "intraday_close")
        self.assertEqual(row["closed_at"], "2026-08-26T15:15:00+05:30")

    def test_the_buys_come_from_the_round_because_fills_are_cleared(self):
        # A parked mother empties `fills`; the round keeps its own legs.
        row = app_module._fib_boundary_campaign_row(FIB_LADDER_STATUS)
        self.assertEqual(row["buys"], 2)
        self.assertEqual(row["opened_at"], "2026-08-26T10:22:00+05:30")
        self.assertEqual(row["contract"], "24200.0 CE")

    def test_the_money_is_the_sum_of_the_rounds(self):
        two_rounds = dict(FIB_LADDER_STATUS)
        second = dict(FIB_LADDER_STATUS["rounds"][0])
        second.update({"round": 2, "net_pnl": 1000.0, "gross_pnl": 1200.0, "costs_total": 200.0, "fills": []})
        two_rounds["rounds"] = [FIB_LADDER_STATUS["rounds"][0], second]
        row = app_module._fib_boundary_campaign_row(two_rounds)
        # A ladder banks rounds, so the last one's net is NOT the campaign's.
        self.assertEqual(row["net_pnl"], 676.96)
        self.assertEqual(row["costs_total"], 402.79)

    def test_a_running_ladder_is_not_archived(self):
        running = dict(FIB_LADDER_STATUS, status="RUNNING")
        self.assertIsNone(app_module._fib_boundary_campaign_row(running))


class FibBoundaryArchiveTests(unittest.IsolatedAsyncioTestCase):
    USER = 9932

    async def asyncSetUp(self):
        self.saved = []

        async def fake_save(user_id, strategy, row):
            self.saved.append(((int(user_id), strategy, row["campaign_key"]), row))
            return True

        self._orig = app_module._db_mod.save_paper_campaign
        app_module._db_mod.save_paper_campaign = fake_save
        app_module._paper_ledger_written.clear()

    async def asyncTearDown(self):
        app_module._db_mod.save_paper_campaign = self._orig

    async def test_a_closed_ladder_reaches_the_ledger(self):
        await app_module._archive_paper_campaign(self.USER, "fib_boundary", FIB_LADDER_STATUS)
        self.assertEqual(len(self.saved), 1)
        (_key, row) = self.saved[0]
        self.assertEqual(row["net_pnl"], -323.04)
        self.assertEqual(row["buys"], 2)

    async def test_a_ladder_is_archived_once_per_money_change(self):
        for _ in range(4):
            await app_module._archive_paper_campaign(self.USER, "fib_boundary", FIB_LADDER_STATUS)
        self.assertEqual(len(self.saved), 1)
