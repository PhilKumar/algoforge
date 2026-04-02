"""Quick integration test for the multi-tenant auth system."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = Path("/tmp/philforge-feature-test-auth.db")
if TEST_DB.exists():
    TEST_DB.unlink()
TEST_USER_DATA = Path("/tmp/philforge-feature-test-user-data")
if TEST_USER_DATA.exists():
    shutil.rmtree(TEST_USER_DATA)

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""


def _write_chart(user_id: int, year: str, month: str, day: str, filename: str):
    folder = TEST_USER_DATA / str(user_id) / "charts" / year / month / day
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(b"fake-image")


async def main():
    import config as app_config
    import db
    from app import _init_database, app

    admin_name = app_config.ADMIN_USERNAME or "admin"
    await _init_database()

    passed = 0
    failed = 0

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        # 1. Unauthenticated status
        r = await c.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["authenticated"] is False
        print("  1. Unauthed status: PASS")
        passed += 1

        # 2. Login as admin
        r = await c.post("/api/auth/login", json={"username": admin_name, "password": "123456"})
        assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
        data = r.json()
        assert data["username"] == admin_name
        assert data["role"] == "admin"
        print(f"  2. Admin login: PASS ({data})")
        passed += 1

        # 3. Authenticated status
        r = await c.get("/api/auth/status")
        assert r.status_code == 200
        data = r.json()
        assert data["authenticated"] is True
        assert data["username"] == admin_name
        assert data["role"] == "admin"
        admin_id = data["user_id"]
        print(f"  3. Authed status: PASS ({data})")
        passed += 1

        # 4. Admin strategy save
        r = await c.post("/api/strategies", json={"run_name": "Admin Strategy", "folder": "Intraday"})
        assert r.status_code == 200
        assert r.json()["run_name"] == "Admin Strategy"
        print("  4. Admin strategy save: PASS")
        passed += 1

        # 5. Admin run creation via DB helper
        await db.create_run_record(
            admin_id,
            {
                "mode": "backtest",
                "run_name": "Admin Backtest",
                "instrument": "26000",
                "trade_count": 1,
                "total_pnl": 123.45,
                "trades": [{"entry_time": "2026-03-18 09:20", "exit_time": "2026-03-18 09:25", "pnl": 123.45}],
                "created_at": "2026-03-18 09:30:00",
            },
        )
        r = await c.get("/api/runs")
        assert r.status_code == 200
        runs = r.json()
        assert len(runs) == 1
        assert runs[0]["run_name"] == "Admin Backtest"
        print("  5. Admin run storage: PASS")
        passed += 1

        await db.upsert_journal_entry(
            admin_id,
            "2026-03-18",
            {"asset": "NIFTY", "strategy": "Admin Journal", "grade": "A"},
        )
        await db.upsert_trade_history_entry(
            admin_id,
            "2026-03-18",
            {
                "pnl": 111.0,
                "net_pnl": 108.5,
                "charges": 2.5,
                "trades": 1,
                "trade_legs": 2,
                "wins": 1,
                "mode": "real",
                "details": [{"symbol": "ADMIN", "pnl": 111.0, "qty": 1, "buy_avg": 10.0, "sell_avg": 121.0}],
            },
        )
        await db.create_scalp_trade(
            admin_id,
            {"trade_id": 9001, "underlying": "NIFTY", "strike": 23000, "option_type": "CE", "pnl": 55.0},
        )
        _write_chart(admin_id, "2026", "Mar-2026", "18-Mar-2026", "admin.png")

        # 6. Admin list users
        r = await c.get("/api/admin/users")
        assert r.status_code == 200
        users = r.json()["users"]
        assert len(users) >= 1
        print(f"  6. Admin list users: PASS ({len(users)} users)")
        passed += 1

        # 7. Admin create user
        r = await c.post("/api/admin/users", json={"username": "phil", "password": "654321", "role": "user"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "phil"
        phil_id = data["user_id"]
        print(f"  7. Create user 'phil': PASS (id={phil_id})")
        passed += 1

        # 8. Admin list users after create
        r = await c.get("/api/admin/users")
        users = r.json()["users"]
        assert len(users) == 2
        print(f"  8. Users after create: PASS ({len(users)} users)")
        passed += 1

        # 9. Logout
        r = await c.post("/api/auth/logout")
        assert r.status_code == 200
        print("  9. Logout: PASS")
        passed += 1

        # 10. Status after logout
        r = await c.get("/api/auth/status")
        assert r.json()["authenticated"] is False
        print(" 10. Status after logout: PASS")
        passed += 1

        # 11. Login as phil
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "654321"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "phil"
        assert data["role"] == "user"
        print(f" 11. Login as phil: PASS ({data})")
        passed += 1

        await db.upsert_journal_entry(
            phil_id,
            "2026-03-17",
            {"asset": "BANKNIFTY", "strategy": "Phil Journal", "grade": "B"},
        )
        await db.upsert_trade_history_entry(
            phil_id,
            "2026-03-17",
            {
                "pnl": 22.0,
                "net_pnl": 20.0,
                "charges": 2.0,
                "trades": 1,
                "trade_legs": 2,
                "wins": 1,
                "mode": "real",
                "details": [{"symbol": "PHIL", "pnl": 22.0, "qty": 1, "buy_avg": 10.0, "sell_avg": 32.0}],
            },
        )
        await db.create_scalp_trade(
            phil_id,
            {"trade_id": 7002, "underlying": "BANKNIFTY", "strike": 51000, "option_type": "PE", "pnl": 18.0},
        )
        _write_chart(phil_id, "2026", "Mar-2026", "17-Mar-2026", "phil.png")

        # 12. Phil sees isolated strategies
        r = await c.get("/api/strategies")
        assert r.status_code == 200
        assert r.json() == []
        print(" 12. Phil strategy isolation: PASS")
        passed += 1

        # 13. Phil sees isolated runs
        r = await c.get("/api/runs")
        assert r.status_code == 200
        assert r.json() == []
        print(" 13. Phil run isolation: PASS")
        passed += 1

        # 14. Phil journal isolation
        r = await c.get("/api/journal/list")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["date"] == "2026-03-17"
        r = await c.get("/api/journal/2026-03-18")
        assert r.status_code == 200
        assert r.json()["data"] is None
        print(" 14. Phil journal isolation: PASS")
        passed += 1

        # 15. Phil chart isolation
        r = await c.get("/api/charts/tree")
        assert r.status_code == 200
        years = r.json()["years"]
        phil_days = [d["folder"] for months in years.values() for month in months for d in month["days"]]
        assert phil_days == ["17-Mar-2026"]
        print(" 15. Phil chart isolation: PASS")
        passed += 1

        # 16. Phil trade-history isolation
        r = await c.get("/api/portfolio/history")
        assert r.status_code == 200
        daily = r.json()["daily"]
        assert "2026-03-17" in daily
        assert "2026-03-18" not in daily
        assert daily["2026-03-17"]["real_pnl"] == 22.0
        print(" 16. Phil trade-history isolation: PASS")
        passed += 1

        # 17. Phil scalp-trade isolation
        r = await c.get("/api/scalp/trades")
        assert r.status_code == 200
        trades = r.json()
        assert len(trades) == 1
        assert trades[0]["trade_id"] == 7002
        print(" 17. Phil scalp-trade isolation: PASS")
        passed += 1

        # 18. Phil can't access admin
        r = await c.get("/api/admin/users")
        assert r.status_code == 403
        print(" 18. Phil admin access denied: PASS (403)")
        passed += 1

        # 19. Phil change own password
        r = await c.put("/api/user/password", json={"current_password": "654321", "new_password": "999999"})
        assert r.status_code == 200
        print(" 19. Phil change password: PASS")
        passed += 1

        # 20. Current session is revoked after password change
        r = await c.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["authenticated"] is False
        print(" 20. Session revoked after password change: PASS")
        passed += 1

        # 21. Login with new password
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "999999"})
        assert r.status_code == 200
        print(" 21. Login with new password: PASS")
        passed += 1

        # 22. Legacy PIN login (no username)
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"password": "123456"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == admin_name
        print(" 22. Legacy PIN login (no username): PASS")
        passed += 1

        # 23. Admin journal isolation
        r = await c.get("/api/journal/list")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["date"] == "2026-03-18"
        r = await c.get("/api/journal/2026-03-17")
        assert r.status_code == 200
        assert r.json()["data"] is None
        print(" 23. Admin journal isolation: PASS")
        passed += 1

        # 24. Admin chart isolation
        r = await c.get("/api/charts/tree")
        assert r.status_code == 200
        years = r.json()["years"]
        admin_days = [d["folder"] for months in years.values() for month in months for d in month["days"]]
        assert admin_days == ["18-Mar-2026"]
        print(" 24. Admin chart isolation: PASS")
        passed += 1

        # 25. Admin trade-history isolation
        r = await c.get("/api/portfolio/history")
        assert r.status_code == 200
        daily = r.json()["daily"]
        assert "2026-03-18" in daily
        assert "2026-03-17" not in daily
        assert daily["2026-03-18"]["real_pnl"] == 111.0
        print(" 25. Admin trade-history isolation: PASS")
        passed += 1

        # 26. Admin scalp-trade isolation
        r = await c.get("/api/scalp/trades")
        assert r.status_code == 200
        trades = r.json()
        assert len(trades) == 1
        assert trades[0]["trade_id"] == 9001
        print(" 26. Admin scalp-trade isolation: PASS")
        passed += 1

        # 27. Wrong password
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": admin_name, "password": "wrong"})
        assert r.status_code == 401
        print(" 27. Wrong password rejected: PASS")
        passed += 1

        # 28. Admin toggle (disable) user
        r = await c.post("/api/auth/login", json={"username": admin_name, "password": "123456"})
        assert r.status_code == 200
        r = await c.put(f"/api/admin/users/{phil_id}/toggle")
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is False
        print(" 28. Disable user 'phil': PASS")
        passed += 1

        # 29. Disabled user can't login
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "999999"})
        assert r.status_code == 403
        print(" 29. Disabled user login blocked: PASS")
        passed += 1

    print(f"\n{'=' * 40}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 40}")

    if TEST_DB.exists():
        TEST_DB.unlink()
    if TEST_USER_DATA.exists():
        shutil.rmtree(TEST_USER_DATA)


if __name__ == "__main__":
    asyncio.run(main())
