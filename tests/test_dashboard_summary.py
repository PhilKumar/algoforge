import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/algoforge-test-dashboard-summary.db")
TEST_USER_DATA = Path("/tmp/algoforge-test-dashboard-summary-data")

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


class _DummyScalpEngine:
    def __init__(self, payload: dict):
        self._payload = payload

    def get_status(self):
        return self._payload


class DashboardSummaryTests(unittest.IsolatedAsyncioTestCase):
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
        app_module._DASHBOARD_REAL_CACHE.clear()
        app_module._FII_DII_CACHE["timestamp"] = 0
        app_module._FII_DII_CACHE["data"] = None
        app_module._FII_DII_HISTORY_ROWS = None
        app_module._scalp_engines.clear()
        await app_module._db_mod.init_db()

    async def asyncTearDown(self):
        app_module._scalp_engines.clear()

    async def test_dashboard_summary_aggregates_strategy_scalp_and_dhan_views(self):
        today = app_module._ist_date_str()
        request = _DummyRequest(user_id=7)
        paper_status = {
            "strategy_name": "Paper Alpha",
            "total_pnl": 250.0,
            "trades_today": 3,
            "closed_trades": [],
        }
        live_status = {
            "strategy_name": "Live Beta",
            "total_pnl": 80.0,
            "trades_today": 2,
            "closed_trades": [],
        }
        scalp_status = {
            "running": True,
            "open_trades": [
                {
                    "trade_id": 101,
                    "mode": "paper",
                    "underlying": "NIFTY",
                    "entry_time": f"{today} 10:15:00",
                    "pnl": 50.0,
                },
                {
                    "trade_id": 102,
                    "mode": "live",
                    "underlying": "BANKNIFTY",
                    "entry_time": f"{today} 10:20:00",
                    "pnl": 30.0,
                },
            ],
            "closed_trades": [],
            "total_pnl": 0.0,
        }
        persisted_scalp = [
            {
                "trade_id": 201,
                "mode": "paper",
                "underlying": "NIFTY",
                "exit_time": f"{today} 09:45:00",
                "pnl": 100.0,
            },
            {
                "trade_id": 202,
                "mode": "live",
                "underlying": "BANKNIFTY",
                "exit_time": f"{today} 09:50:00",
                "pnl": -40.0,
            },
        ]
        fii_dii_payload = {
            "status": "partial",
            "source": "Official NSE combined feed",
            "as_of": "25 Mar",
            "latest": {"date": "2026-03-25", "display_date": "25 Mar", "fii_net": -1805.37, "dii_net": 5429.78},
            "rolling_30d": {"fii_net": -1805.37, "dii_net": 5429.78, "days": 1},
            "trend": [{"date": "2026-03-25", "display_date": "25 Mar", "fii_net": -1805.37, "dii_net": 5429.78}],
            "message": "Rolling history builds from the official NSE daily feed.",
        }

        app_module._scalp_engines[7] = _DummyScalpEngine(scalp_status)

        with (
            patch.object(app_module, "_get_session_token", return_value="tok"),
            patch.object(app_module, "_validate_session_async", AsyncMock(return_value={"user_id": 7})),
            patch.object(app_module._db_mod, "list_strategies", AsyncMock(return_value=[{"name": "Paper Alpha"}])),
            patch.object(app_module._db_mod, "list_runs", AsyncMock(return_value=[{"id": 1, "mode": "backtest"}])),
            patch.object(app_module._db_mod, "list_scalp_trades", AsyncMock(return_value=persisted_scalp)),
            patch.object(app_module, "_running_statuses_for_user", side_effect=[[paper_status], [live_status]]),
            patch.object(
                app_module,
                "_request_broker_context",
                AsyncMock(return_value=({"id": 7, "role": "user"}, object(), "user")),
            ),
            patch.object(
                app_module,
                "_load_dashboard_real_snapshot",
                AsyncMock(
                    return_value={
                        "available": True,
                        "source": "dhan",
                        "source_label": "Dhan today",
                        "gross_pnl": 720.0,
                        "net_pnl": 700.0,
                        "charges": 20.0,
                        "brokerage": 0.0,
                        "trades": 9,
                        "message": "Dhan tradebook",
                        "stale": False,
                    }
                ),
            ),
            patch.object(app_module, "_load_dashboard_fii_dii_snapshot", AsyncMock(return_value=fii_dii_payload)),
        ):
            result = await app_module.dashboard_summary(request)

        self.assertEqual(result["paper_total_pnl"], 400.0)
        self.assertEqual(result["paper_total_trades"], 5)
        self.assertEqual(result["paper_scalp_pnl"], 150.0)
        self.assertEqual(result["real_total_pnl"], 700.0)
        self.assertEqual(result["real_total_trades"], 9)
        self.assertEqual(result["real_scalp_pnl"], -10.0)
        self.assertEqual(result["active_count"], 3)
        self.assertEqual(result["paper_flow"]["name"], "Paper Alpha · SCALP NIFTY")
        self.assertEqual(result["real_flow"]["name"], "Live Beta · SCALP BANKNIFTY")
        self.assertEqual(result["paper_strategy_flow"]["pnl"], 250.0)
        self.assertEqual(result["live_strategy_flow"]["pnl"], 710.0)
        self.assertEqual(result["scalp_flow"]["paper_pnl"], 150.0)
        self.assertEqual(result["scalp_flow"]["real_pnl"], -10.0)
        self.assertEqual(result["real_source_label"], "Dhan today")
        self.assertEqual(result["fii_dii"]["latest"]["display_date"], "25 Mar")

    async def test_load_dashboard_real_snapshot_prefers_order_count_over_fill_count(self):
        user_id = 7
        broker_client = SimpleNamespace(
            get_trades=lambda: [
                {
                    "exchangeTime": "2026-03-27 09:16:00",
                    "orderId": "A1",
                    "exchangeTradeId": "1",
                    "transactionType": "BUY",
                    "securityId": "123",
                    "tradingSymbol": "NIFTY XYZ",
                    "tradedPrice": 100,
                    "tradedQuantity": 50,
                },
                {
                    "exchangeTime": "2026-03-27 09:16:01",
                    "orderId": "A1",
                    "exchangeTradeId": "2",
                    "transactionType": "BUY",
                    "securityId": "123",
                    "tradingSymbol": "NIFTY XYZ",
                    "tradedPrice": 100,
                    "tradedQuantity": 15,
                },
                {
                    "exchangeTime": "2026-03-27 09:20:00",
                    "orderId": "B1",
                    "exchangeTradeId": "3",
                    "transactionType": "SELL",
                    "securityId": "123",
                    "tradingSymbol": "NIFTY XYZ",
                    "tradedPrice": 110,
                    "tradedQuantity": 50,
                },
                {
                    "exchangeTime": "2026-03-27 09:20:01",
                    "orderId": "B1",
                    "exchangeTradeId": "4",
                    "transactionType": "SELL",
                    "securityId": "123",
                    "tradingSymbol": "NIFTY XYZ",
                    "tradedPrice": 110,
                    "tradedQuantity": 15,
                },
            ]
        )

        with patch.object(app_module, "_ist_date_str", return_value="2026-03-27"):
            result = app_module._load_dashboard_real_snapshot_sync(user_id, broker_client)

        self.assertTrue(result["available"])
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["fill_count"], 4)

    def test_normalize_fii_dii_snapshot_rows_collapses_categories_into_one_day(self):
        records = [
            {"category": "FII/FPI", "date": "25-Mar-2026", "netValue": "-1805.37"},
            {"category": "DII", "date": "25-Mar-2026", "netValue": "5429.78"},
        ]

        rows = app_module._normalize_fii_dii_snapshot_rows(records)

        self.assertEqual(
            rows,
            [
                {
                    "date": "2026-03-25",
                    "display_date": "25 Mar",
                    "fii_net": -1805.37,
                    "dii_net": 5429.78,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
