import json
import os
import subprocess
import sys
import tempfile
import unittest

import pandas as pd


class ReplayScriptTests(unittest.TestCase):
    def _write_json(self, path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _write_csv(self, path, rows):
        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)

    def _script_path(self):
        repo_root = os.path.dirname(os.path.dirname(__file__))
        return os.path.join(repo_root, "scripts", "replay_session.py")

    def test_replay_script_reports_summary_and_signals(self):
        with tempfile.TemporaryDirectory() as tempdir:
            csv_path = os.path.join(tempdir, "session.csv")
            indicators_path = os.path.join(tempdir, "indicators.json")
            entry_path = os.path.join(tempdir, "entry.json")
            self._write_csv(
                csv_path,
                [
                    {
                        "timestamp": "2026-03-27 09:15:00",
                        "open": 100.0,
                        "high": 100.2,
                        "low": 99.9,
                        "close": 100.0,
                        "volume": 10,
                    },
                    {
                        "timestamp": "2026-03-27 09:16:00",
                        "open": 100.0,
                        "high": 101.2,
                        "low": 99.9,
                        "close": 101.0,
                        "volume": 11,
                    },
                    {
                        "timestamp": "2026-03-27 09:17:00",
                        "open": 101.0,
                        "high": 102.2,
                        "low": 100.8,
                        "close": 102.0,
                        "volume": 12,
                    },
                ],
            )
            self._write_json(indicators_path, [])
            self._write_json(
                entry_path,
                [{"left": "current_close", "operator": "is_above", "right": "number", "right_number_value": 100.5}],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    self._script_path(),
                    csv_path,
                    "--indicators-file",
                    indicators_path,
                    "--entry-file",
                    entry_path,
                    "--default-timeframe",
                    "1",
                    "--source-timeframe",
                    "1",
                    "--signals-only",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Rows loaded: 3", result.stdout)
            self.assertIn("Replay candles: 3", result.stdout)
            self.assertIn("Signals: 2", result.stdout)
            self.assertIn("Missing-data candles: 0", result.stdout)
            self.assertIn("2026-03-27 09:16  PASS", result.stdout)
            self.assertIn("2026-03-27 09:17  PASS", result.stdout)

    def test_replay_script_can_fail_on_missing_condition_data(self):
        with tempfile.TemporaryDirectory() as tempdir:
            csv_path = os.path.join(tempdir, "session.csv")
            indicators_path = os.path.join(tempdir, "indicators.json")
            entry_path = os.path.join(tempdir, "entry.json")
            self._write_csv(
                csv_path,
                [
                    {
                        "timestamp": "2026-03-27 09:15:00",
                        "open": 100.0,
                        "high": 100.2,
                        "low": 99.9,
                        "close": 100.0,
                        "volume": 10,
                    },
                    {
                        "timestamp": "2026-03-27 09:16:00",
                        "open": 100.0,
                        "high": 100.1,
                        "low": 99.7,
                        "close": 99.8,
                        "volume": 11,
                    },
                ],
            )
            self._write_json(indicators_path, [])
            self._write_json(
                entry_path,
                [{"left": "current_close", "operator": "is_below", "right": "CPR_BC"}],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    self._script_path(),
                    csv_path,
                    "--indicators-file",
                    indicators_path,
                    "--entry-file",
                    entry_path,
                    "--default-timeframe",
                    "1",
                    "--source-timeframe",
                    "1",
                    "--fail-on-missing-data",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("Missing-data candles: 2", result.stdout)
            self.assertIn("missing_condition_data (CPR_BC)", result.stdout)

    def test_replay_script_can_process_directory_of_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            indicators_path = os.path.join(tempdir, "indicators.json")
            entry_path = os.path.join(tempdir, "entry.json")
            self._write_json(indicators_path, [])
            self._write_json(
                entry_path,
                [{"left": "current_close", "operator": "is_above", "right": "number", "right_number_value": 100.5}],
            )
            self._write_csv(
                os.path.join(tempdir, "day1.csv"),
                [
                    {
                        "timestamp": "2026-03-27 09:15:00",
                        "open": 100.0,
                        "high": 100.2,
                        "low": 99.9,
                        "close": 100.0,
                        "volume": 10,
                    },
                    {
                        "timestamp": "2026-03-27 09:16:00",
                        "open": 100.0,
                        "high": 101.2,
                        "low": 99.9,
                        "close": 101.0,
                        "volume": 11,
                    },
                ],
            )
            self._write_csv(
                os.path.join(tempdir, "day2.csv"),
                [
                    {
                        "timestamp": "2026-03-28 09:15:00",
                        "open": 101.0,
                        "high": 102.2,
                        "low": 100.8,
                        "close": 102.0,
                        "volume": 12,
                    },
                    {
                        "timestamp": "2026-03-28 09:16:00",
                        "open": 102.0,
                        "high": 102.4,
                        "low": 101.6,
                        "close": 102.2,
                        "volume": 13,
                    },
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    self._script_path(),
                    tempdir,
                    "--glob",
                    "*.csv",
                    "--indicators-file",
                    indicators_path,
                    "--entry-file",
                    entry_path,
                    "--default-timeframe",
                    "1",
                    "--source-timeframe",
                    "1",
                    "--signals-only",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Files processed: 2", result.stdout)
            self.assertIn("Total rows loaded: 4", result.stdout)
            self.assertIn("Total replay candles: 4", result.stdout)
            self.assertIn("Total signals: 3", result.stdout)
            self.assertIn("Total missing-data candles: 0", result.stdout)

    def test_replay_script_directory_fail_on_missing_data_aggregates(self):
        with tempfile.TemporaryDirectory() as tempdir:
            indicators_path = os.path.join(tempdir, "indicators.json")
            entry_path = os.path.join(tempdir, "entry.json")
            self._write_json(indicators_path, [])
            self._write_json(entry_path, [{"left": "current_close", "operator": "is_below", "right": "CPR_BC"}])
            self._write_csv(
                os.path.join(tempdir, "day1.csv"),
                [
                    {
                        "timestamp": "2026-03-27 09:15:00",
                        "open": 100.0,
                        "high": 100.2,
                        "low": 99.9,
                        "close": 100.0,
                        "volume": 10,
                    },
                    {
                        "timestamp": "2026-03-27 09:16:00",
                        "open": 100.0,
                        "high": 100.1,
                        "low": 99.7,
                        "close": 99.8,
                        "volume": 11,
                    },
                ],
            )
            self._write_csv(
                os.path.join(tempdir, "day2.csv"),
                [
                    {
                        "timestamp": "2026-03-28 09:15:00",
                        "open": 99.8,
                        "high": 100.0,
                        "low": 99.5,
                        "close": 99.6,
                        "volume": 12,
                    },
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    self._script_path(),
                    tempdir,
                    "--glob",
                    "*.csv",
                    "--indicators-file",
                    indicators_path,
                    "--entry-file",
                    entry_path,
                    "--default-timeframe",
                    "1",
                    "--source-timeframe",
                    "1",
                    "--fail-on-missing-data",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("Files processed: 2", result.stdout)
            self.assertIn("Total missing-data candles: 3", result.stdout)


if __name__ == "__main__":
    unittest.main()
