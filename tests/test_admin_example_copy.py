import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/philforge-test-admin-example-copy.db")
TEST_USER_DATA = Path("/tmp/philforge-test-admin-example-copy-data")

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

import app as app_module


class _DummyRequest:
    def __init__(self, body: dict | None = None):
        self._body = body or {}

    async def json(self):
        return self._body


class AdminExampleCopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        if TEST_USER_DATA.exists():
            shutil.rmtree(TEST_USER_DATA)
        app_module.config.DB_PATH = str(TEST_DB)
        app_module.config.USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._db_mod.config.DB_PATH = str(TEST_DB)
        app_module._db_mod.config.USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._db_mod._initialized = False
        await app_module._db_mod.init_db()

    async def _create_user(self, username: str, role: str = "user") -> int:
        return await app_module._db_mod.create_user(username, f"{username}-hash", role=role)

    def _write_chart_day(self, user_id: int, year: str, month: str, day: str, filenames: list[str]) -> None:
        day_path = Path(app_module._user_charts_root(user_id)) / year / month / day
        day_path.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            (day_path / filename).write_bytes(b"fake-image")

    async def test_admin_create_user_auto_seeds_default_examples_for_new_user(self):
        admin_id = await self._create_user("admin", role="admin")

        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "Starter Pack",
                "name": "Starter Pack",
                "folder": "Default",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )
        await app_module._db_mod.create_run_record(
            admin_id,
            {
                "mode": "backtest",
                "run_name": "Starter Backtest",
                "strategy_name": "Starter Backtest",
                "folder": "Default",
                "trade_count": 3,
                "total_pnl": 150.0,
                "summary": {"net": 150.0},
                "trades": [{"id": 1, "pnl": 50.0}],
                "created_at": "2026-03-24 09:15:00",
            },
        )
        self._write_chart_day(admin_id, "2026", "Mar-2026", "24-Mar-2026", ["starter.png"])

        request = _DummyRequest(
            {
                "username": "newuser",
                "password": "Password123",
                "role": "user",
            }
        )

        with patch.object(
            app_module._auth_mod,
            "require_admin",
            return_value={"id": admin_id, "username": "admin", "role": "admin"},
        ):
            result = await app_module.admin_create_user(request)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["copied"]["strategies"], 1)
        self.assertEqual(result["copied"]["backtests"], 1)
        self.assertEqual(result["copied"]["charts"], 1)
        self.assertEqual(result["copied"]["journal"], 0)

        created_user = await app_module._db_mod.get_user_by_username("newuser")
        self.assertIsNotNone(created_user)

        target_strategies = await app_module._db_mod.list_strategies(int(created_user["id"]))
        self.assertEqual(len(target_strategies), 1)
        self.assertEqual(target_strategies[0]["run_name"], "Starter Pack (Admin Example)")

        target_runs = await app_module._db_mod.list_runs(int(created_user["id"]))
        self.assertEqual(len(target_runs), 1)
        self.assertEqual(target_runs[0]["run_name"], "Starter Backtest (Admin Example)")
        self.assertEqual(str(target_runs[0].get("folder") or ""), "Default")

        self.assertEqual(await app_module._db_mod.list_journal_entries(int(created_user["id"])), [])
        chart_manifest = app_module._load_chart_seed_manifest(int(created_user["id"]))
        self.assertEqual(chart_manifest["day"], "24-Mar-2026")
        target_chart_dir = (
            TEST_USER_DATA / str(int(created_user["id"])) / "charts" / "2026" / "Mar-2026" / "24-Mar-2026"
        )
        self.assertEqual(sorted(path.name for path in target_chart_dir.iterdir()), ["AdminExample_starter.png"])

    async def test_existing_users_are_backfilled_once_from_default_examples(self):
        admin_id = await self._create_user("admin", role="admin")
        alice_id = await self._create_user("alice")
        bob_id = await self._create_user("bob")

        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "Default Starter",
                "name": "Default Starter",
                "folder": "Default",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )
        await app_module._db_mod.create_run_record(
            admin_id,
            {
                "mode": "backtest",
                "run_name": "Default Backtest",
                "strategy_name": "Default Backtest",
                "folder": "Default",
                "trade_count": 4,
                "total_pnl": 200.0,
                "summary": {"net": 200.0},
                "trades": [{"id": 1, "pnl": 50.0}],
                "created_at": "2026-03-24 09:15:00",
            },
        )
        self._write_chart_day(admin_id, "2026", "Mar-2026", "20-Mar-2026", ["older.png"])
        self._write_chart_day(admin_id, "2026", "Mar-2026", "24-Mar-2026", ["latest.png"])

        await app_module._db_mod.upsert_journal_entry(
            alice_id,
            "2026-03-21",
            {
                "asset": "NIFTY",
                "strategy": "Should Remove",
                app_module._ADMIN_EXAMPLE_SEED_KEY: {
                    "kind": "journal",
                    "source_user_id": admin_id,
                    "source_date": "2026-03-21",
                },
            },
        )
        await app_module._db_mod.upsert_journal_entry(
            bob_id,
            "2026-03-22",
            {
                "asset": "BANKNIFTY",
                "strategy": "Manual Note",
                "grade": "B",
            },
        )

        first = await app_module._backfill_default_examples_for_existing_users_once()

        self.assertEqual(first["status"], "done")
        self.assertEqual(first["processed_users"], 2)
        self.assertEqual(first["seeded_users"], 2)
        self.assertEqual(
            await app_module._db_mod.get_app_state(app_module._DEFAULT_EXAMPLES_BACKFILL_STATE_KEY),
            "done",
        )

        for user_id in (alice_id, bob_id):
            strategies = await app_module._db_mod.list_strategies(user_id)
            runs = await app_module._db_mod.list_runs(user_id)
            self.assertEqual([item["run_name"] for item in strategies], ["Default Starter (Admin Example)"])
            self.assertEqual([item["run_name"] for item in runs], ["Default Backtest (Admin Example)"])
            manifest = app_module._load_chart_seed_manifest(user_id)
            self.assertEqual(manifest["day"], "24-Mar-2026")

        self.assertIsNone(await app_module._db_mod.get_journal_entry(alice_id, "2026-03-21"))
        self.assertEqual((await app_module._db_mod.get_journal_entry(bob_id, "2026-03-22"))["strategy"], "Manual Note")

        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "Late Addition",
                "name": "Late Addition",
                "folder": "Default",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )

        second = await app_module._backfill_default_examples_for_existing_users_once()

        self.assertEqual(second["status"], "skipped")
        alice_strategies = await app_module._db_mod.list_strategies(alice_id)
        self.assertEqual(len(alice_strategies), 1)

    async def test_copy_admin_examples_copies_only_default_folder_strategies_and_latest_two_default_backtests(self):
        admin_id = await self._create_user("admin", role="admin")
        target_id = await self._create_user("alice")

        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "Momentum Builder",
                "name": "Momentum Builder",
                "folder": "Default",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )
        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "Skip Me",
                "name": "Skip Me",
                "folder": "Experimental",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )
        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "",
                "name": "",
                "folder": "Empty Folder",
                "_placeholder": True,
                "version": 1,
                "versions": [],
            },
        )

        for index in range(6):
            await app_module._db_mod.create_run_record(
                admin_id,
                {
                    "mode": "backtest",
                    "run_name": f"Example Backtest {index + 1}",
                    "strategy_name": f"Example Backtest {index + 1}",
                    "folder": "Default" if index >= 2 else "Archive",
                    "trade_count": index + 2,
                    "total_pnl": float((index + 1) * 100),
                    "summary": {"net": (index + 1) * 100},
                    "trades": [{"id": index + 1, "pnl": (index + 1) * 10}],
                    "created_at": f"2026-03-{index + 1:02d} 09:15:00",
                },
            )
        await app_module._db_mod.create_run_record(
            admin_id,
            {
                "mode": "live",
                "run_name": "Live Should Skip",
                "strategy_name": "Live Should Skip",
                "trade_count": 1,
                "total_pnl": 0.0,
                "summary": {},
                "trades": [],
                "created_at": "2026-03-26 09:15:00",
            },
        )
        self._write_chart_day(admin_id, "2026", "Mar-2026", "20-Mar-2026", ["older.png"])
        self._write_chart_day(admin_id, "2026", "Mar-2026", "24-Mar-2026", ["latest.png"])

        copied = await app_module._copy_admin_examples_to_user(admin_id, target_id)

        self.assertEqual(copied["strategies"]["copied"], 1)
        self.assertEqual(copied["backtests"]["copied"], 2)
        self.assertEqual(copied["charts"]["copied"], 1)
        self.assertEqual(copied["charts"]["target_date"], "2026-Mar-2026/24-Mar-2026")

        target_strategies = await app_module._db_mod.list_strategies(target_id)
        self.assertEqual(len(target_strategies), 1)
        self.assertEqual(target_strategies[0]["run_name"], "Momentum Builder (Admin Example)")
        self.assertEqual(target_strategies[0]["_example_seed"]["source_user_id"], admin_id)

        target_runs = [run for run in await app_module._db_mod.list_runs(target_id) if run.get("mode") == "backtest"]
        self.assertEqual(len(target_runs), 2)
        copied_source_names = {run["_example_seed"]["source_id"] for run in target_runs}
        self.assertEqual(len(copied_source_names), 2)
        self.assertTrue(all("(Admin Example)" in str(run.get("run_name") or "") for run in target_runs))
        self.assertTrue(all(str(run.get("folder") or "") == "Default" for run in target_runs))
        self.assertEqual(
            {run.get("strategy_name") for run in target_runs},
            {"Example Backtest 6 (Admin Example)", "Example Backtest 5 (Admin Example)"},
        )

        self.assertEqual(await app_module._db_mod.list_journal_entries(target_id), [])
        chart_manifest = app_module._load_chart_seed_manifest(target_id)
        self.assertEqual(chart_manifest["day"], "24-Mar-2026")
        copied_chart_dir = TEST_USER_DATA / str(target_id) / "charts" / "2026" / "Mar-2026" / "24-Mar-2026"
        self.assertEqual(sorted(path.name for path in copied_chart_dir.iterdir()), ["AdminExample_latest.png"])

    async def test_copy_admin_examples_refreshes_seeded_records_and_removes_seeded_journal_examples(self):
        admin_id = await self._create_user("admin", role="admin")
        target_id = await self._create_user("bob")

        await app_module._db_mod.create_strategy_record(
            admin_id,
            {
                "run_name": "Alpha",
                "name": "Alpha",
                "folder": "Default",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )
        await app_module._db_mod.create_strategy_record(
            target_id,
            {
                "run_name": "Alpha (Admin Example)",
                "name": "Alpha (Admin Example)",
                "folder": "Manual",
                "instrument": "NIFTY",
                "legs": [],
                "entry_conditions": [],
                "exit_conditions": [],
                "version": 1,
                "versions": [],
            },
        )

        await app_module._db_mod.create_run_record(
            admin_id,
            {
                "mode": "backtest",
                "run_name": "March Sample",
                "strategy_name": "March Sample",
                "folder": "Default",
                "trade_count": 7,
                "total_pnl": 420.0,
                "summary": {"net": 420.0},
                "trades": [{"id": 1, "pnl": 42.0}],
                "created_at": "2026-03-24 10:00:00",
            },
        )
        await app_module._db_mod.create_run_record(
            target_id,
            {
                "mode": "backtest",
                "run_name": "March Sample (Admin Example)",
                "strategy_name": "March Sample (Admin Example)",
                "folder": "Default",
                "trade_count": 1,
                "total_pnl": 1.0,
                "summary": {"net": 1.0},
                "trades": [{"id": 99, "pnl": 1.0}],
                "created_at": "2026-03-22 10:00:00",
            },
        )

        await app_module._db_mod.upsert_journal_entry(
            target_id,
            "2026-03-24",
            {
                "asset": "BANKNIFTY",
                "strategy": "Personal Note",
                "grade": "C",
                "went_well": "Manual journal must stay",
            },
        )
        await app_module._db_mod.upsert_journal_entry(
            target_id,
            "2026-03-25",
            {
                "asset": "NIFTY",
                "strategy": "Old Seeded Journal",
                app_module._ADMIN_EXAMPLE_SEED_KEY: {
                    "kind": "journal",
                    "source_user_id": admin_id,
                    "source_date": "2026-03-25",
                },
            },
        )
        self._write_chart_day(admin_id, "2026", "Mar-2026", "24-Mar-2026", ["latest.png"])

        first_copy = await app_module._copy_admin_examples_to_user(admin_id, target_id)
        second_copy = await app_module._copy_admin_examples_to_user(admin_id, target_id)

        self.assertEqual(first_copy["removed_seeded_journals"], 1)
        self.assertEqual(second_copy["removed_seeded_journals"], 0)
        self.assertEqual(first_copy["charts"]["target_date"], "2026-Mar-2026/24-Mar-2026")
        self.assertEqual(second_copy["charts"]["target_date"], "2026-Mar-2026/24-Mar-2026")

        target_strategies = await app_module._db_mod.list_strategies(target_id)
        seeded_strategies = [item for item in target_strategies if item.get("_example_seed")]
        self.assertEqual(len(seeded_strategies), 1)
        self.assertEqual(seeded_strategies[0]["run_name"], "Alpha (Admin Example) 2")

        target_runs = [run for run in await app_module._db_mod.list_runs(target_id) if run.get("mode") == "backtest"]
        seeded_runs = [run for run in target_runs if run.get("_example_seed")]
        self.assertEqual(len(seeded_runs), 1)
        self.assertEqual(seeded_runs[0]["run_name"], "March Sample (Admin Example) 2")
        self.assertEqual(seeded_runs[0]["trade_count"], 7)

        manual_journal = await app_module._db_mod.get_journal_entry(target_id, "2026-03-24")
        self.assertEqual(manual_journal["strategy"], "Personal Note")
        self.assertIsNone(await app_module._db_mod.get_journal_entry(target_id, "2026-03-25"))

        chart_manifest = app_module._load_chart_seed_manifest(target_id)
        self.assertEqual(chart_manifest["day"], "24-Mar-2026")
        copied_chart_dir = TEST_USER_DATA / str(target_id) / "charts" / "2026" / "Mar-2026" / "24-Mar-2026"
        self.assertEqual(sorted(path.name for path in copied_chart_dir.iterdir()), ["AdminExample_latest.png"])


if __name__ == "__main__":
    unittest.main()
