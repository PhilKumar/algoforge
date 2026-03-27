import json
import os
import subprocess
import sys
import tempfile
import unittest

import pandas as pd


class ReconcilePaperSessionScriptTests(unittest.TestCase):
    def _script_path(self):
        repo_root = os.path.dirname(os.path.dirname(__file__))
        return os.path.join(repo_root, "scripts", "reconcile_paper_session.py")

    def _write_json(self, path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _write_csv(self, path, rows):
        pd.DataFrame(rows).to_csv(path, index=False)

    def test_reconcile_script_reports_pass_for_matching_paper_trade(self):
        with tempfile.TemporaryDirectory() as tempdir:
            status_path = os.path.join(tempdir, "paper_status.json")
            csv_path = os.path.join(tempdir, "session.csv")
            output_dir = os.path.join(tempdir, "reports")
            self._write_json(
                status_path,
                {
                    "run_id": "Strategy_PE",
                    "strategy_name": "Strategy_PE",
                    "strategy": {
                        "run_name": "Strategy_PE",
                        "instrument": "26000",
                        "timeframe_minutes": 1,
                        "fetch_timeframe_minutes": 1,
                        "max_trades_per_day": 1,
                        "market_open": "09:15",
                        "market_close": "15:25",
                        "indicators": [],
                        "entry_conditions": [
                            {
                                "logic": "IF",
                                "left": "current_close",
                                "operator": "is_above",
                                "right": "number",
                                "right_number_value": 100.5,
                            }
                        ],
                        "exit_conditions": [],
                    },
                    "closed_trades": [
                        {
                            "entry_time": "2026-03-30 09:17:00",
                            "exit_time": "2026-03-30 09:20:00",
                            "exit_reason": "EXIT_SIGNAL",
                            "pnl": 12.5,
                            "symbol": "TEST",
                        }
                    ],
                    "event_log": [],
                },
            )
            self._write_csv(
                csv_path,
                [
                    {"timestamp": "2026-03-30 09:15:00", "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
                    {"timestamp": "2026-03-30 09:16:00", "open": 100.0, "high": 101.1, "low": 99.9, "close": 101.0},
                    {"timestamp": "2026-03-30 09:17:00", "open": 101.0, "high": 101.3, "low": 100.8, "close": 101.2},
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    self._script_path(),
                    "--date",
                    "2026-03-30",
                    "--status-json",
                    status_path,
                    "--ohlcv-path",
                    csv_path,
                    "--output-dir",
                    output_dir,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Overall: PASS", result.stdout)
            report_dirs = os.listdir(output_dir)
            self.assertEqual(len(report_dirs), 1)
            report_path = os.path.join(output_dir, report_dirs[0], "report.json")
            with open(report_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["summary"]["overall"], "PASS")
            self.assertEqual(report["paper"]["trade_count"], 1)
            self.assertEqual(report["replay"]["pass_window_count"], 1)

    def test_reconcile_script_fails_when_replay_has_missing_data(self):
        with tempfile.TemporaryDirectory() as tempdir:
            status_path = os.path.join(tempdir, "paper_status.json")
            csv_path = os.path.join(tempdir, "session.csv")
            output_dir = os.path.join(tempdir, "reports")
            self._write_json(
                status_path,
                {
                    "run_id": "Strategy_PE",
                    "strategy_name": "Strategy_PE",
                    "strategy": {
                        "run_name": "Strategy_PE",
                        "instrument": "26000",
                        "timeframe_minutes": 1,
                        "fetch_timeframe_minutes": 1,
                        "max_trades_per_day": 1,
                        "market_open": "09:15",
                        "market_close": "15:25",
                        "indicators": [],
                        "entry_conditions": [
                            {
                                "logic": "IF",
                                "left": "current_close",
                                "operator": "is_below",
                                "right": "CPR_BC",
                            }
                        ],
                        "exit_conditions": [],
                    },
                    "closed_trades": [],
                    "event_log": [],
                },
            )
            self._write_csv(
                csv_path,
                [
                    {"timestamp": "2026-03-30 09:15:00", "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
                    {"timestamp": "2026-03-30 09:16:00", "open": 100.0, "high": 100.1, "low": 99.7, "close": 99.8},
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    self._script_path(),
                    "--date",
                    "2026-03-30",
                    "--status-json",
                    status_path,
                    "--ohlcv-path",
                    csv_path,
                    "--output-dir",
                    output_dir,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1, msg=result.stderr)
            self.assertIn("Overall: FAIL", result.stdout)
            report_dirs = os.listdir(output_dir)
            report_path = os.path.join(output_dir, report_dirs[0], "report.json")
            with open(report_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["summary"]["overall"], "FAIL")
            self.assertGreater(report["replay"]["missing_data_candles"], 0)


if __name__ == "__main__":
    unittest.main()
