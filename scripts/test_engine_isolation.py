#!/usr/bin/env python3
"""Regression test for user-scoped live/paper engine isolation."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = Path("/tmp/philforge-feature-test-engines.db")
if TEST_DB.exists():
    TEST_DB.unlink()
TEST_USER_DATA = Path("/tmp/philforge-feature-test-engine-data")
if TEST_USER_DATA.exists():
    shutil.rmtree(TEST_USER_DATA)

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""


class DummyEngine:
    def __init__(self, user_id: int, run_id: str, strategy_name: str, total_pnl: float, trades_today: int):
        self._user_id = user_id
        self.run_id = run_id
        self.strategy_name = strategy_name
        self.instrument = "26000"
        self.running = True
        self.positions = []
        self.trades_today = trades_today
        self.total_pnl = total_pnl
        self.closed_trades = [
            {
                "id": 1,
                "entry_time": "2026-03-19 09:20:00",
                "exit_time": "2026-03-19 09:25:00",
                "entry_premium": 100.0,
                "exit_premium": 110.0,
                "option_type": "CE",
                "strike": 23000,
                "lots": 1,
                "lot_size": 75,
                "pnl": total_pnl,
                "exit_reason": "TARGET",
            }
        ]
        self.strategy = {"run_name": strategy_name, "_user_id": user_id}
        self.saved = False

    def get_status(self):
        return {
            "running": self.running,
            "run_id": self.run_id,
            "mode": "paper",
            "in_trade": False,
            "positions": list(self.positions),
            "closed_trades": list(self.closed_trades),
            "total_pnl": self.total_pnl,
            "trades_today": self.trades_today,
            "strategy_name": self.strategy_name,
            "instrument": self.instrument,
            "current_candle": {},
            "current_indicators": {},
            "event_log": [],
            "strategy": dict(self.strategy),
        }

    def stop(self):
        self.running = False

    def _save_state(self):
        self.saved = True

    def _delete_state_file(self):
        self.saved = False

    def debug_engine_state(self):
        return {"run_id": self.run_id, "user_id": self._user_id, "running": self.running}


async def _login(client: httpx.AsyncClient, username: str, password: str) -> int:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    status = await client.get("/api/auth/status")
    assert status.status_code == 200
    return int(status.json()["user_id"])


async def main():
    import app as app_module
    from app import _init_database, app

    await _init_database()

    passed = 0

    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as phil_client,
    ):
        admin_id = await _login(admin_client, "admin", "123456")
        r = await admin_client.post("/api/admin/users", json={"username": "phil", "password": "654321", "role": "user"})
        assert r.status_code == 200, r.text
        phil_id = int(r.json()["user_id"])
        await admin_client.post("/api/auth/logout")
        assert await _login(phil_client, "phil", "654321") == phil_id
        assert await _login(admin_client, "admin", "123456") == admin_id

        app_module.live_engines.clear()
        app_module.paper_engines.clear()
        app_module._live_tasks.clear()
        app_module._paper_tasks.clear()
        app_module._stopped_engines.clear()
        app_module._alert_state.clear()

        app_module.live_engines[admin_id]["shared-run"] = DummyEngine(admin_id, "shared-run", "Admin Live", 11.0, 1)
        app_module.live_engines[phil_id]["shared-run"] = DummyEngine(phil_id, "shared-run", "Phil Live", 22.0, 2)
        app_module.paper_engines[admin_id]["paper-shared"] = DummyEngine(
            admin_id, "paper-shared", "Admin Paper", 33.0, 3
        )
        app_module.paper_engines[phil_id]["paper-shared"] = DummyEngine(phil_id, "paper-shared", "Phil Paper", 44.0, 4)
        app_module._stopped_engines[admin_id] = {"stopped-admin": {"run_id": "stopped-admin", "mode": "paper"}}
        app_module._stopped_engines[phil_id] = {"stopped-phil": {"run_id": "stopped-phil", "mode": "auto"}}

        r = await admin_client.get("/api/live/status", params={"run_id": "shared-run"})
        assert r.status_code == 200
        assert r.json()["strategy_name"] == "Admin Live"
        passed += 1
        print("  1. Admin live status isolated: PASS")

        r = await phil_client.get("/api/live/status", params={"run_id": "shared-run"})
        assert r.status_code == 200
        assert r.json()["strategy_name"] == "Phil Live"
        passed += 1
        print("  2. Phil live status isolated: PASS")

        r = await admin_client.get("/api/paper/status", params={"run_id": "paper-shared"})
        assert r.status_code == 200
        assert r.json()["strategy_name"] == "Admin Paper"
        passed += 1
        print("  3. Admin paper status isolated: PASS")

        r = await phil_client.get("/api/engines/all")
        assert r.status_code == 200
        phil_runs = {engine["run_id"] for engine in r.json()["engines"]}
        assert phil_runs == {"shared-run", "paper-shared", "stopped-phil"}
        passed += 1
        print("  4. Phil combined engine list isolated: PASS")

        r = await admin_client.get("/api/engines/all")
        assert r.status_code == 200
        admin_runs = {engine["run_id"] for engine in r.json()["engines"]}
        assert admin_runs == {"shared-run", "paper-shared", "stopped-admin"}
        passed += 1
        print("  5. Admin combined engine list isolated: PASS")

        r = await admin_client.get("/api/dashboard/summary")
        assert r.status_code == 200
        summary = r.json()
        assert summary["paper_running"] is True
        assert summary["live_running"] is True
        assert summary["paper_strategy"] == "Admin Paper"
        assert summary["live_strategy"] == "Admin Live"
        passed += 1
        print("  6. Dashboard summary uses only admin engines: PASS")

        r = await admin_client.get("/api/live/trades/csv", params={"run_id": "shared-run"})
        assert r.status_code == 200
        assert "11.0" in r.text
        assert "22.0" not in r.text
        passed += 1
        print("  7. Live CSV export isolated by user: PASS")

        r = await phil_client.get("/api/paper/trades/csv", params={"run_id": "paper-shared"})
        assert r.status_code == 200
        assert "44.0" in r.text
        assert "33.0" not in r.text
        passed += 1
        print("  8. Paper CSV export isolated by user: PASS")

        r = await phil_client.post("/api/emergency-stop")
        assert r.status_code == 200
        assert app_module.live_engines[admin_id]["shared-run"].running is True
        assert app_module.paper_engines[admin_id]["paper-shared"].running is True
        assert app_module.live_engines[phil_id] == {}
        assert app_module.paper_engines[phil_id] == {}
        passed += 1
        print("  9. Emergency stop only stops caller engines: PASS")

        r = await admin_client.post("/api/emergency-stop")
        assert r.status_code == 200
        assert app_module.live_engines[admin_id] == {}
        assert app_module.paper_engines[admin_id] == {}
        passed += 1
        print(" 10. Admin emergency stop clears remaining engines: PASS")

    print(f"\n{'=' * 40}")
    print(f"  Results: {passed} passed, 0 failed")
    print(f"{'=' * 40}")

    if TEST_DB.exists():
        TEST_DB.unlink()
    if TEST_USER_DATA.exists():
        shutil.rmtree(TEST_USER_DATA)


if __name__ == "__main__":
    asyncio.run(main())
