import os
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/philforge-test-cascade-paper.db")
TEST_USER_DATA = Path("/tmp/philforge-test-cascade-paper-data")

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zH5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

import app as app_module


class _DummyRequest:
    def __init__(self, user_id: int = 7):
        self.state = SimpleNamespace(user_id=user_id)


class CascadePaperRouteTests(unittest.IsolatedAsyncioTestCase):
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
        app_module._cascade_engines.clear()
        await app_module._db_mod.init_db()

    async def asyncTearDown(self):
        for runtime in app_module._cascade_engines.values():
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
        app_module._cascade_engines.clear()

    async def test_status_is_paper_only_and_exposes_live_gate(self):
        request = _DummyRequest()
        with patch.object(
            app_module,
            "_request_broker_context",
            AsyncMock(return_value=({"id": 7}, object(), "user")),
        ):
            result = await app_module.cascade_paper_status(request)

        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["mode"], "paper")
        self.assertFalse(result["live_gate"]["armed"])

    async def test_start_rejects_an_unclosed_mother_candle_before_broker_access(self):
        now = datetime.now(app_module.IST).replace(second=0, microsecond=0)
        mother_open = now.replace(minute=now.minute - (now.minute % 5))
        payload = app_module.CascadePaperStartPayload(
            mother_timestamp=mother_open.isoformat(),
            mother_open=25000,
            mother_high=25020,
            mother_low=24980,
            mother_close=25010,
        )
        with self.assertRaises(app_module.HTTPException) as raised:
            await app_module.cascade_paper_start(payload, _DummyRequest())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("completed NIFTY 5m candle", str(raised.exception.detail))


if __name__ == "__main__":
    unittest.main()
