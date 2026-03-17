"""Quick integration test for the multi-tenant auth system."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ALGOFORGE_PIN"] = "123456"

import httpx
import uvicorn


async def main():
    from app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=8799, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(4)

    passed = 0
    failed = 0

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8799", timeout=15) as c:
        # 1. Unauthenticated status
        r = await c.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["authenticated"] is False
        print("  1. Unauthed status: PASS")
        passed += 1

        # 2. Login as admin
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "123456"})
        assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
        data = r.json()
        assert data["username"] == "admin"
        assert data["role"] == "admin"
        print(f"  2. Admin login: PASS ({data})")
        passed += 1

        # 3. Authenticated status
        r = await c.get("/api/auth/status")
        assert r.status_code == 200
        data = r.json()
        assert data["authenticated"] is True
        assert data["username"] == "admin"
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

        # 12. Logout and login with new password
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "999999"})
        assert r.status_code == 200
        print(" 12. Login with new password: PASS")
        passed += 1

        # 13. Legacy PIN login (no username)
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"password": "123456"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "admin"
        print(" 13. Legacy PIN login (no username): PASS")
        passed += 1

        # 14. Wrong password
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
        print(" 14. Wrong password rejected: PASS")
        passed += 1

        # 15. Admin toggle (disable) user
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "123456"})
        assert r.status_code == 200
        r = await c.put(f"/api/admin/users/{phil_id}/toggle")
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is False
        print(" 15. Disable user 'phil': PASS")
        passed += 1

        # 16. Disabled user can't login
        await c.post("/api/auth/logout")
        r = await c.post("/api/auth/login", json={"username": "phil", "password": "999999"})
        assert r.status_code == 403
        print(" 16. Disabled user login blocked: PASS")
        passed += 1

    print(f"\n{'='*40}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*40}")

    server.should_exit = True
    await task


if __name__ == "__main__":
    asyncio.run(main())
