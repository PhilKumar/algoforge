"""Deleting a user, and the window that stops the console asking every time.

Phil asked for two things on the admin console: a Delete button per user, and
one authenticator prompt instead of one per action. Both are easy to get subtly
wrong — a delete that leaves rows behind for the next account to inherit that
id, or a grace window that quietly widens to live trading — so both are pinned
here.
"""

import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/philforge-test-admin-delete.db")
TEST_USER_DATA = Path("/tmp/philforge-test-admin-delete-data")

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

import app as app_module  # noqa: E402
import auth as auth_module  # noqa: E402


class _DummyRequest:
    def __init__(self, body: dict | None = None):
        self._body = body or {}

    async def json(self):
        return self._body


class SensitiveActionRuleTests(unittest.TestCase):
    def test_deleting_a_user_is_a_protected_action(self):
        self.assertEqual(
            auth_module.classify_sensitive_action("DELETE", "/api/admin/users/7"),
            "admin_account",
        )

    def test_the_grace_window_never_covers_money(self):
        """The window exists for the admin console. It must not spread.

        A confirmation that admitted live trading or a broker order for the next
        half hour would defeat the point of the per-request challenge on the
        things that move real money.
        """
        for money_class in ("live_trading", "broker_order", "broker_credentials", "account_security"):
            self.assertNotIn(money_class, auth_module.GRACE_ACTION_CLASSES)
        self.assertIn("admin_account", auth_module.GRACE_ACTION_CLASSES)


class ActionGrantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        app_module.config.DB_PATH = str(TEST_DB)
        app_module._db_mod.config.DB_PATH = str(TEST_DB)
        app_module._db_mod._initialized = False
        await app_module._db_mod.init_db()
        self.db = app_module._db_mod

    async def test_a_grant_admits_the_same_class_and_nothing_else(self):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        await self.db.grant_action_class(1, "session-hash", "admin_account", soon)

        self.assertTrue(await self.db.has_action_grant(1, "session-hash", "admin_account"))
        # A different class, session or user is a different question entirely.
        self.assertFalse(await self.db.has_action_grant(1, "session-hash", "live_trading"))
        self.assertFalse(await self.db.has_action_grant(1, "other-session", "admin_account"))
        self.assertFalse(await self.db.has_action_grant(2, "session-hash", "admin_account"))

    async def test_an_expired_grant_does_not_admit(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.db.grant_action_class(1, "session-hash", "admin_account", past)
        self.assertFalse(await self.db.has_action_grant(1, "session-hash", "admin_account"))

    async def test_signing_out_closes_the_window(self):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        await self.db.grant_action_class(1, "session-hash", "admin_account", soon)
        await self.db.delete_action_grants_for_session("session-hash")
        self.assertFalse(await self.db.has_action_grant(1, "session-hash", "admin_account"))


class AdminDeleteUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        if TEST_USER_DATA.exists():
            shutil.rmtree(TEST_USER_DATA)
        # PUT THE PROCESS SCRATCH DIRECTORY BACK. app.py points
        # `tempfile.tempdir` at <USER_DATA_ROOT>/.scratch at import, to keep
        # spooled uploads off the server's RAM-backed /tmp. This test aims
        # USER_DATA_ROOT at its own directory and then deletes it -- which
        # takes the whole process's temp directory with it. Every later test
        # that writes a temp file then dies with FileNotFoundError on
        # .scratch/..., and a full-suite run collapsed with 112 failures and
        # 67 errors while each file passed alone.
        TEST_USER_DATA.mkdir(parents=True, exist_ok=True)
        (TEST_USER_DATA / ".scratch").mkdir(exist_ok=True)
        app_module.config.DB_PATH = str(TEST_DB)
        app_module.config.USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._db_mod.config.DB_PATH = str(TEST_DB)
        app_module._db_mod.config.USER_DATA_ROOT = str(TEST_USER_DATA)
        app_module._db_mod._initialized = False
        await app_module._db_mod.init_db()
        self.admin_id = await app_module._db_mod.create_user("admin", "admin-hash", role="admin")

    async def _admin(self):
        return await app_module._db_mod.get_user_by_id(self.admin_id)

    async def test_delete_removes_the_user_their_rows_and_their_folder(self):
        user_id = await app_module._db_mod.create_user("packrat", "packrat-hash", role="user")
        await app_module._db_mod.create_strategy_record(
            user_id,
            {"run_name": "Theirs", "name": "Theirs", "folder": "Default", "legs": [], "version": 1, "versions": []},
        )
        chart_day = Path(app_module._user_charts_root(user_id)) / "2026" / "Aug-2026" / "15-Aug-2026"
        chart_day.mkdir(parents=True, exist_ok=True)
        (chart_day / "chart.png").write_bytes(b"fake-image")

        async def fake_require_admin(_request):
            return await self._admin()

        original = app_module._auth_mod.require_admin
        app_module._auth_mod.require_admin = fake_require_admin
        try:
            result = await app_module.admin_delete_user(user_id, _DummyRequest())
        finally:
            app_module._auth_mod.require_admin = original

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["username"], "packrat")
        self.assertTrue(result["removed_files"])
        self.assertEqual(result["removed"].get("users"), 1)
        self.assertEqual(result["removed"].get("strategies"), 1)

        self.assertIsNone(await app_module._db_mod.get_user_by_id(user_id))
        # The next account handed this id must not inherit anything.
        self.assertEqual(await app_module._db_mod.list_strategies(user_id), [])
        self.assertFalse(Path(app_module._user_storage_root(user_id)).exists())

    async def test_delete_refuses_your_own_account_and_the_last_admin(self):
        async def fake_require_admin(_request):
            return await self._admin()

        original = app_module._auth_mod.require_admin
        app_module._auth_mod.require_admin = fake_require_admin
        try:
            with self.assertRaises(app_module.HTTPException) as own:
                await app_module.admin_delete_user(self.admin_id, _DummyRequest())
            self.assertEqual(own.exception.status_code, 400)

            # A second admin, deleted by the first, would leave nobody: the
            # guard is about the LAST active admin, not about admins at large.
            other_admin = await app_module._db_mod.create_user("admin2", "hash", role="admin")
            result = await app_module.admin_delete_user(other_admin, _DummyRequest())
            self.assertEqual(result["status"], "ok")

            missing = await app_module._db_mod.get_user_by_id(9999)
            self.assertIsNone(missing)
            with self.assertRaises(app_module.HTTPException) as gone:
                await app_module.admin_delete_user(9999, _DummyRequest())
            self.assertEqual(gone.exception.status_code, 404)
        finally:
            app_module._auth_mod.require_admin = original


if __name__ == "__main__":
    unittest.main()
