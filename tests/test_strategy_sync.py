import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["ALGOFORGE_SKIP_STARTUP_JOBS"] = "1"

import app as app_module


class RuntimeStrategySyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_saved_strategy_from_runtime_updates_drifted_conditions(self):
        existing = {
            "id": 12,
            "run_name": "Strategy_PE",
            "name": "Strategy_PE",
            "folder": "Intraday",
            "instrument": "26000",
            "indicators": ["Current_Candle_5m", "EMA_20_5m", "CPR_0.2_0.5", "RSI_14"],
            "entry_conditions": [
                {"logic": "IF", "left": "current_close", "operator": "is_below", "right": "EMA_20_5m"},
                {"logic": "AND", "left": "CPR_is_wide", "operator": "==", "right": "false"},
                {"logic": "AND", "left": "Day_Of_Week", "operator": "contains", "right": "days"},
                {"logic": "AND", "left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "11:00"},
            ],
            "exit_conditions": [],
            "version": 1,
            "versions": [{"version": 1, "saved_at": "2026-03-01 09:15:00", "changes": "Initial save"}],
            "updated_at": "2026-03-01 09:15:00",
        }
        runtime = {
            "strategy_id": 12,
            "run_name": "Strategy_PE",
            "name": "Strategy_PE",
            "folder": "Intraday",
            "instrument": "26000",
            "indicators": ["Current_Candle_5m", "CPR_0.2_0.5", "RSI_14_5m", "EMA_20_5m"],
            "entry_conditions": [
                {"logic": "IF", "left": "current_close", "operator": "is_below", "right": "EMA_20_5m"},
                {"logic": "AND", "left": "CPR_is_wide", "operator": "==", "right": "false"},
                {"logic": "AND", "left": "Day_Of_Week", "operator": "contains", "right": "days"},
                {"logic": "AND", "left": "current_close", "operator": "is_below", "right": "CPR_BC"},
                {"logic": "AND", "left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "11:00"},
            ],
            "exit_conditions": [],
        }

        with (
            patch.object(app_module._db_mod, "get_strategy", AsyncMock(return_value=existing)),
            patch.object(app_module._db_mod, "replace_strategy_record", AsyncMock()) as replace_mock,
        ):
            changed = await app_module._sync_saved_strategy_from_runtime(
                1,
                12,
                runtime,
                runtime["entry_conditions"],
                runtime["exit_conditions"],
                source_label="paper start",
            )

        self.assertTrue(changed)
        saved = replace_mock.await_args.args[2]
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["folder"], "Intraday")
        self.assertIn(
            {"logic": "AND", "left": "current_close", "operator": "is_below", "right": "CPR_BC"},
            saved["entry_conditions"],
        )
        self.assertEqual(saved["indicators"], ["Current_Candle_5m", "CPR_0.2_0.5", "RSI_14_5m", "EMA_20_5m"])

    async def test_sync_saved_strategy_from_runtime_skips_when_no_persisted_drift(self):
        existing = {
            "id": 12,
            "run_name": "Strategy_PE",
            "name": "Strategy_PE",
            "folder": "Intraday",
            "instrument": "26000",
            "indicators": ["Current_Candle_5m", "CPR_0.2_0.5", "RSI_14_5m", "EMA_20_5m"],
            "entry_conditions": [
                {"logic": "IF", "left": "current_close", "operator": "is_below", "right": "EMA_20_5m"},
                {"logic": "AND", "left": "CPR_is_wide", "operator": "==", "right": "false"},
                {"logic": "AND", "left": "Day_Of_Week", "operator": "contains", "right": "days"},
                {"logic": "AND", "left": "current_close", "operator": "is_below", "right": "CPR_BC"},
                {"logic": "AND", "left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "11:00"},
            ],
            "exit_conditions": [],
            "version": 2,
            "versions": [{"version": 2, "saved_at": "2026-03-27 12:00:00", "changes": "Synced from paper start"}],
            "updated_at": "2026-03-27 12:00:00",
        }

        with (
            patch.object(app_module._db_mod, "get_strategy", AsyncMock(return_value=existing)),
            patch.object(app_module._db_mod, "replace_strategy_record", AsyncMock()) as replace_mock,
        ):
            changed = await app_module._sync_saved_strategy_from_runtime(
                1,
                12,
                existing,
                existing["entry_conditions"],
                existing["exit_conditions"],
                source_label="paper start",
            )

        self.assertFalse(changed)
        replace_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
