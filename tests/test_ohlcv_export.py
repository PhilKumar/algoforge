import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/algoforge-test-ohlcv-export.db")
TEST_USER_DATA = Path("/tmp/algoforge-test-ohlcv-export-data")

os.environ["ALGOFORGE_PIN"] = "123456"
os.environ["ALGOFORGE_DB"] = str(TEST_DB)
os.environ["ALGOFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["ALGOFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

import app as app_module


class _DummyRequest:
    def __init__(self, user_id: int = 7):
        self.state = SimpleNamespace(user_id=user_id)


class OhlcvExportRouteTests(unittest.IsolatedAsyncioTestCase):
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

    def _sample_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [100.0, 101.0, 102.0],
                "high": [100.5, 101.5, 102.5],
                "low": [99.5, 100.5, 101.5],
                "close": [100.2, 101.2, 102.2],
                "volume": [10, 11, 12],
            },
            index=pd.to_datetime(
                [
                    "2026-03-26 09:15:00",
                    "2026-03-26 09:16:00",
                    "2026-03-27 09:15:00",
                ]
            ),
        )

    async def test_export_replay_ohlcv_writes_split_day_csvs_and_manifest(self):
        payload = app_module.OhlcvExportPayload(
            instrument="26000",
            from_date="2026-03-26",
            to_date="2026-03-27",
            candle_interval="1",
            split_by_day=True,
        )
        request = _DummyRequest(user_id=7)
        frame = self._sample_frame()

        with (
            patch.object(app_module, "_get_session_token", return_value="tok"),
            patch.object(app_module, "_validate_session_async", AsyncMock(return_value={"user_id": 7})),
            patch.object(app_module, "_fetch_data", return_value=frame),
        ):
            result = await app_module.export_replay_ohlcv(payload, request)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows"], 3)
        self.assertTrue(result["split_by_day"])
        self.assertEqual([item["name"] for item in result["files"]], ["2026-03-26.csv", "2026-03-27.csv"])

        export_dir = Path(result["export_dir"])
        self.assertTrue(export_dir.is_dir())
        self.assertTrue((export_dir / "2026-03-26.csv").exists())
        self.assertTrue((export_dir / "2026-03-27.csv").exists())

        day_one = pd.read_csv(export_dir / "2026-03-26.csv")
        self.assertEqual(day_one.columns.tolist(), ["timestamp", "open", "high", "low", "close", "volume"])
        self.assertEqual(day_one["timestamp"].tolist(), ["2026-03-26 09:15:00", "2026-03-26 09:16:00"])

        with open(export_dir / "manifest.json", "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["instrument"], "26000")
        self.assertEqual(manifest["effective_interval"], "1")
        self.assertEqual(len(manifest["files"]), 2)

    async def test_export_replay_ohlcv_can_write_single_csv(self):
        payload = app_module.OhlcvExportPayload(
            instrument="26000",
            from_date="2026-03-26",
            to_date="2026-03-27",
            candle_interval="1",
            split_by_day=False,
            export_name="March Replay",
        )
        request = _DummyRequest(user_id=9)
        frame = self._sample_frame()

        with (
            patch.object(app_module, "_get_session_token", return_value="tok"),
            patch.object(app_module, "_validate_session_async", AsyncMock(return_value={"user_id": 9})),
            patch.object(app_module, "_fetch_data", return_value=frame),
        ):
            result = await app_module.export_replay_ohlcv(payload, request)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["split_by_day"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["name"], "26000_2026-03-26_to_2026-03-27_1.csv")

        export_dir = Path(result["export_dir"])
        combined = pd.read_csv(export_dir / "26000_2026-03-26_to_2026-03-27_1.csv")
        self.assertEqual(len(combined), 3)
        self.assertEqual(combined["timestamp"].iloc[-1], "2026-03-27 09:15:00")


if __name__ == "__main__":
    unittest.main()
