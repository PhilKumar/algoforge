"""Quick integration test for the multi-tenant auth system."""

import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = Path("/tmp/algoforge-feature-test-auth.db")
if TEST_DB.exists():
    TEST_DB.unlink()

os.environ["ALGOFORGE_PIN"] = "123456"
os.environ["ALGOFORGE_DB"] = str(TEST_DB)
os.environ["ALGOFORGE_SKIP_STARTUP_JOBS"] = "1"


async def main():
    import config as app_config
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
        print(f"  3. Authed status: PASS ({data})")
        passed += 1

        # 4. Admin list users
        r = await c.get("/api/admin/users")
        assert r.status_code == 200
        users = r.json()["users"]
        assert len(users) >= 1
        print(f"  4. Admin list users: PASS ({len(users)} users)")
        passed += 1

        # 5. Admin create user
        r = await c.post("/api/admin/users", json={"username": "phil", "password": "654321", "role": "user"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "phil"
        phil_id = data["user_id"]
        print(f"  5. Create user 'phil': PASS (id={phil_id})")
        passed += 1

        # 6. Admin list users after create
        r = await c.get("/api/admin/users")
        users = r.json()["users"]
        assert len(users) == 2
        print(f"  6. Users after create: PASS ({len(users)} users)")
        passed += 1

        # 7. Logout
        r = await c.post("/api/auth/logout")
        assert r.status_code == 200
        print("  7. Logout: PASS")
        passed += 1

        # 8. Status after logout
        r = await c.get("/api/auth/status")
        assert r.json()["authenticated"] is False
        print("  8. Status after logout: PASS")
        passed += 1

        # 9. Login as phil
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "654321"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "phil"
        assert data["role"] == "user"
        print(f"  9. Login as phil: PASS ({data})")
        passed += 1

        # 10. Phil can't access admin
        r = await c.get("/api/admin/users")
        assert r.status_code == 403
        print(" 10. Phil admin access denied: PASS (403)")
        passed += 1

        # 11. Phil change own password
        r = await c.put("/api/user/password", json={"current_password": "654321", "new_password": "999999"})
        assert r.status_code == 200
        print(" 11. Phil change password: PASS")
        passed += 1

        # 12. Current session is revoked after password change
        r = await c.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["authenticated"] is False
        print(" 12. Session revoked after password change: PASS")
        passed += 1

        # 13. Login with new password
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "999999"})
        assert r.status_code == 200
        print(" 13. Login with new password: PASS")
        passed += 1

        # 14. Legacy PIN login (no username)
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"password": "123456"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == admin_name
        print(" 14. Legacy PIN login (no username): PASS")
        passed += 1

        # 15. Wrong password
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": admin_name, "password": "wrong"})
        assert r.status_code == 401
        print(" 15. Wrong password rejected: PASS")
        passed += 1

        # 16. Admin toggle (disable) user
        r = await c.post("/api/auth/login", json={"username": admin_name, "password": "123456"})
        assert r.status_code == 200
        r = await c.put(f"/api/admin/users/{phil_id}/toggle")
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is False
        print(" 16. Disable user 'phil': PASS")
        passed += 1

        # 17. Disabled user can't login
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "999999"})
        assert r.status_code == 403
        print(" 17. Disabled user login blocked: PASS")
        passed += 1

    print(f"\n{'=' * 40}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 40}")

    if TEST_DB.exists():
        TEST_DB.unlink()


if __name__ == "__main__":
    asyncio.run(main())
