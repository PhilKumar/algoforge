import os
import shutil
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/algoforge-test-portfolio.db")
if TEST_DB.exists():
    TEST_DB.unlink()
TEST_USER_DATA = Path("/tmp/algoforge-test-portfolio-data")
if TEST_USER_DATA.exists():
    shutil.rmtree(TEST_USER_DATA)

os.environ["ALGOFORGE_PIN"] = "123456"
os.environ["ALGOFORGE_DB"] = str(TEST_DB)
os.environ["ALGOFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["ALGOFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

from app import _aggregate_portfolio_history, _ist_date_str, _summarize_real_trade_fills


class PortfolioHistoryRegressionTests(unittest.TestCase):
    def test_summarize_real_trade_fills_uses_fifo_not_day_average(self):
        entry = _summarize_real_trade_fills(
            [
                {
                    "orderId": "B1",
                    "exchangeTradeId": "1",
                    "transactionType": "BUY",
                    "securityId": "NIFTY-1",
                    "tradedQuantity": 10,
                    "tradedPrice": 100,
                    "exchangeTime": "2026-03-12 09:15:00",
                },
                {
                    "orderId": "S1",
                    "exchangeTradeId": "2",
                    "transactionType": "SELL",
                    "securityId": "NIFTY-1",
                    "tradedQuantity": 10,
                    "tradedPrice": 110,
                    "exchangeTime": "2026-03-12 09:20:00",
                },
                {
                    "orderId": "B2",
                    "exchangeTradeId": "3",
                    "transactionType": "BUY",
                    "securityId": "NIFTY-1",
                    "tradedQuantity": 10,
                    "tradedPrice": 120,
                    "exchangeTime": "2026-03-12 09:25:00",
                },
                {
                    "orderId": "S2",
                    "exchangeTradeId": "4",
                    "transactionType": "SELL",
                    "securityId": "NIFTY-1",
                    "tradedQuantity": 10,
                    "tradedPrice": 121,
                    "exchangeTime": "2026-03-12 09:30:00",
                },
            ]
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry["pnl"], 110.0)
        self.assertEqual(entry["net_pnl"], 110.0)
        self.assertEqual(entry["trades"], 4)
        self.assertEqual(entry["trade_legs"], 4)

    def test_summarize_real_trade_fills_counts_unique_orders_and_preserves_partial_fills(self):
        entry = _summarize_real_trade_fills(
            [
                {
                    "orderId": "B1",
                    "exchangeTradeId": "fill-1",
                    "transactionType": "BUY",
                    "securityId": "BANK-1",
                    "tradedQuantity": 5,
                    "tradedPrice": 100,
                    "exchangeTime": "2026-03-16 09:15:00",
                    "brokerageCharges": 5,
                },
                {
                    "orderId": "B1",
                    "exchangeTradeId": "fill-2",
                    "transactionType": "BUY",
                    "securityId": "BANK-1",
                    "tradedQuantity": 5,
                    "tradedPrice": 100,
                    "exchangeTime": "2026-03-16 09:15:01",
                    "brokerageCharges": 5,
                },
                {
                    "orderId": "S1",
                    "exchangeTradeId": "fill-3",
                    "transactionType": "SELL",
                    "securityId": "BANK-1",
                    "tradedQuantity": 5,
                    "tradedPrice": 110,
                    "exchangeTime": "2026-03-16 09:20:00",
                    "brokerageCharges": 5,
                },
                {
                    "orderId": "S1",
                    "exchangeTradeId": "fill-4",
                    "transactionType": "SELL",
                    "securityId": "BANK-1",
                    "tradedQuantity": 5,
                    "tradedPrice": 110,
                    "exchangeTime": "2026-03-16 09:20:01",
                    "brokerageCharges": 5,
                },
            ]
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry["pnl"], 100.0)
        self.assertEqual(entry["charges"], 20.0)
        self.assertEqual(entry["net_pnl"], 80.0)
        self.assertEqual(entry["trades"], 2)
        self.assertEqual(entry["trade_legs"], 4)

    def test_aggregate_portfolio_history_tracks_gross_and_net_totals(self):
        real_history = {
            "2026-03-31": {
                "pnl": 100.0,
                "net_pnl": 90.0,
                "charges": 10.0,
                "trades": 1,
                "trade_legs": 2,
                "wins": 1,
            },
            "2026-04-01": {
                "pnl": -50.0,
                "net_pnl": -55.0,
                "charges": 5.0,
                "trades": 1,
                "trade_legs": 2,
                "wins": 0,
            },
        }
        runs = [
            {
                "mode": "paper",
                "started_at": "2026-03-31 09:15:00",
                "trades": [
                    {"entry_time": "2026-03-31 09:15:00", "exit_time": "2026-03-31 09:20:00", "pnl": 20.0},
                    {"entry_time": "2026-04-01 09:15:00", "exit_time": "2026-04-01 09:20:00", "pnl": -5.0},
                ],
            }
        ]

        daily, monthly, yearly = _aggregate_portfolio_history(real_history, runs)

        self.assertEqual(daily["2026-03-31"]["real_net_pnl"], 90.0)
        self.assertEqual(daily["2026-03-31"]["paper_pnl"], 20.0)
        self.assertEqual(monthly["2026-03"]["real_pnl"], 100.0)
        self.assertEqual(monthly["2026-03"]["real_net_pnl"], 90.0)
        self.assertEqual(monthly["2026-03"]["paper_pnl"], 20.0)
        self.assertEqual(monthly["2026-03"]["total_pnl"], 120.0)
        self.assertEqual(monthly["2026-03"]["total_net_pnl"], 110.0)
        self.assertEqual(monthly["2026-04"]["total_pnl"], -55.0)
        self.assertEqual(monthly["2026-04"]["total_net_pnl"], -60.0)
        self.assertEqual(yearly["2026"]["real_pnl"], 50.0)
        self.assertEqual(yearly["2026"]["real_net_pnl"], 35.0)
        self.assertEqual(yearly["2026"]["paper_pnl"], 15.0)
        self.assertEqual(yearly["2026"]["total_pnl"], 65.0)
        self.assertEqual(yearly["2026"]["total_net_pnl"], 50.0)
        self.assertEqual(yearly["2026"]["trades"], 4)
        self.assertEqual(yearly["2026"]["wins"], 2)

    def test_aggregate_portfolio_history_falls_back_to_run_level_paper_totals(self):
        daily, monthly, yearly = _aggregate_portfolio_history(
            {},
            [
                {
                    "mode": "paper",
                    "started_at": "2026-05-04 09:15:00",
                    "trade_count": 2,
                    "total_pnl": 12.5,
                    "stats": {"winning_trades": 1},
                }
            ],
        )

        self.assertEqual(daily["2026-05-04"]["paper_pnl"], 12.5)
        self.assertEqual(daily["2026-05-04"]["paper_trades"], 2)
        self.assertEqual(monthly["2026-05"]["paper_pnl"], 12.5)
        self.assertEqual(monthly["2026-05"]["total_net_pnl"], 12.5)
        self.assertEqual(yearly["2026"]["wins"], 1)

    def test_ist_date_str_uses_india_calendar_day(self):
        boundary = datetime(2026, 3, 31, 18, 45, tzinfo=timezone.utc)
        self.assertEqual(_ist_date_str(boundary), "2026-04-01")


if __name__ == "__main__":
    unittest.main()
