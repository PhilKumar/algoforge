#!/usr/bin/env python3
"""Regression test for per-user broker context and scalp isolation."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = Path("/tmp/algoforge-feature-test-broker.db")
if TEST_DB.exists():
    TEST_DB.unlink()
TEST_USER_DATA = Path("/tmp/algoforge-feature-test-broker-data")
if TEST_USER_DATA.exists():
    shutil.rmtree(TEST_USER_DATA)

os.environ["ALGOFORGE_PIN"] = "123456"
os.environ["ALGOFORGE_DB"] = str(TEST_DB)
os.environ["ALGOFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["ALGOFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zHn5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""
os.environ["DHAN_CLIENT_ID"] = "global-client"
os.environ["DHAN_ACCESS_TOKEN"] = "global-token-12345678901234567890"


class DummyLiveEngine:
    def __init__(self, dhan, run_id=None, state_dir=None):
        self.dhan = dhan
        self.run_id = run_id or "live"
        self.state_dir = state_dir
        self.running = False
        self.positions = []
        self.closed_trades = []
        self.strategy = {}
        self.event_log = []
        self.in_trade = False
        self.trades_today = 0
        self.instrument = ""
        self.strategy_name = run_id or "live"
        self.current_candle = {}
        self.current_indicators = {}

    def configure(self, strategy=None, entry_conditions=None, exit_conditions=None, deploy_config=None):
        self.strategy = dict(strategy or {})
        self.strategy_name = self.strategy.get("run_name", self.run_id)
        self.instrument = self.strategy.get("instrument", "")

    async def start(self, callback=None):
        self.running = True
        if callback:
            await callback({"type": "status", "running": True})

    def stop(self):
        self.running = False

    def get_status(self):
        return {
            "running": self.running,
            "run_id": self.run_id,
            "mode": "auto",
            "in_trade": self.in_trade,
            "positions": list(self.positions),
            "closed_trades": list(self.closed_trades),
            "total_pnl": 0,
            "trades_today": self.trades_today,
            "strategy_name": self.strategy_name,
            "instrument": self.instrument,
            "current_candle": dict(self.current_candle),
            "current_indicators": dict(self.current_indicators),
            "event_log": list(self.event_log),
            "strategy": dict(self.strategy),
        }

    def _save_state(self):
        return None

    def _delete_state_file(self):
        return None

    def set_feed(self, feed):
        self.feed = feed


class DummyScalpEngine:
    def __init__(self, dhan_client, market_feed=None, on_trade_close=None):
        self.dhan = dhan_client
        self.feed = market_feed
        self.on_trade_close = on_trade_close
        self._running = False
        self._trade_counter = 0
        self._user_id = 0
        self.open_trades = {}
        self.closed_trades = []
        self.event_log = []

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    async def enter_trade(self, **kwargs):
        self._trade_counter += 1
        self._running = True
        trade = {
            "trade_id": self._trade_counter,
            "status": "open",
            "mode": kwargs.get("mode", "live"),
            "entry_premium": 101.0,
            "current_premium": 101.0,
            "underlying": kwargs.get("underlying", "NIFTY"),
            "strike": kwargs.get("strike", 23000),
            "option_type": kwargs.get("option_type", "CE"),
            "transaction_type": kwargs.get("transaction_type", "BUY"),
            "target_premium": kwargs.get("target_premium", 0.0),
            "sl_premium": kwargs.get("sl_premium", 0.0),
        }
        self.open_trades[self._trade_counter] = trade
        return {"status": "ok", "trade_id": self._trade_counter, "trade": dict(trade)}

    async def exit_trade(self, trade_id, reason="manual"):
        trade = self.open_trades.pop(trade_id, None)
        if not trade:
            return {"status": "error", "message": "Trade not found"}
        trade = dict(trade)
        trade["status"] = "closed"
        trade["exit_reason"] = reason
        self.closed_trades.append(trade)
        if self.on_trade_close:
            self.on_trade_close(dict(trade))
        return {"status": "ok", "trade": trade}

    async def kill_all_trades(self):
        closed = len(self.open_trades)
        self.open_trades.clear()
        return {"status": "ok", "closed": closed}

    async def update_trade_targets(self, trade_id, **kwargs):
        trade = self.open_trades.get(trade_id)
        if not trade:
            return {"status": "error", "message": "Trade not found"}
        trade.update(kwargs)
        return {"status": "ok", "trade": dict(trade)}

    def get_status(self):
        return {
            "running": self._running,
            "open_trades": [dict(t) for t in self.open_trades.values()],
            "closed_trades": list(self.closed_trades),
            "event_log": list(self.event_log),
            "total_pnl": 0,
        }


def _balance_for(client_id: str) -> float:
    mapping = {
        "global-client": 111111.0,
        "phil-client": 222222.0,
        "alpha-client": 123456.0,
        "beta-client": 654321.0,
    }
    return mapping.get(client_id, 333333.0)


def _positions_for(client_id: str) -> list:
    return [{"symbol": client_id, "unrealizedProfit": _balance_for(client_id) / 1000}]


async def _login(client: httpx.AsyncClient, username: str, password: str) -> int:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    status = await client.get("/api/auth/status")
    assert status.status_code == 200, status.text
    return int(status.json()["user_id"])


async def main():
    import app as app_module
    import broker.dhan as dhan_module
    import db
    import token_manager as token_manager_module
    from app import _init_database, app

    await _init_database()

    # Monkeypatch broker methods to return account-specific data without touching the network.
    dhan_module._api_cache.clear()

    def fake_get_funds(self):
        return {"availabelBalance": _balance_for(self.client_id)}

    def fake_get_positions(self):
        return _positions_for(self.client_id)

    def fake_get_trades(self, from_date=None, to_date=None):
        return [{"tradingSymbol": f"{self.client_id}-TRADE"}]

    def fake_get_order_book(self):
        return [{"orderId": f"{self.client_id}-ORDER"}]

    def fake_place_order(self, **kwargs):
        return {"status": "ok", "orderId": f"{self.client_id}-PLACED", "client_id": self.client_id, **kwargs}

    def fake_cancel_order(self, order_id):
        return {"status": "ok", "orderId": order_id, "client_id": self.client_id}

    def fake_get_option_ltp(self, underlying, strike, expiry, option_type):
        base = {
            "global-client": 101.25,
            "phil-client": 222.5,
            "beta-client": 333.75,
        }
        return base.get(self.client_id, 99.5)

    def fake_get_ohlc_multi(self, segments):
        nifty_map = {
            "global-client": 24025.0,
            "phil-client": 25175.0,
            "beta-client": 23350.0,
        }
        nifty = nifty_map.get(self.client_id, 24500.0)
        ce = round(nifty / 200, 2)
        pe = round((nifty / 200) + 12, 2)
        return {
            "IDX_I": {
                "13": {"last_price": nifty, "ohlc": {"close": nifty - 100}},
                "25": {"last_price": nifty + 1200, "ohlc": {"close": nifty + 1080}},
                "49": {"last_price": nifty - 300, "ohlc": {"close": nifty - 360}},
                "51": {"last_price": 80000 + (nifty - 24000) * 2, "ohlc": {"close": 79850 + (nifty - 24000) * 2}},
            },
            "NSE_FNO": {
                "900001": {"last_price": ce, "ohlc": {"close": ce - 8}},
                "900002": {"last_price": pe, "ohlc": {"close": pe - 6}},
            },
        }

    def fake_get_ltp_multi(self, segments):
        if self.client_id == "stale-client":
            raise Exception(
                'LTP fetch failed: {"data":{"808":"Authentication Failed - Client ID or Token invalid"},"status":"failed"}'
            )
        return fake_get_ohlc_multi(self, segments)

    def fake_generate_access_token(client_id, pin, totp_secret):
        return {
            "success": True,
            "accessToken": f"{client_id}-fresh-token-12345678901234567890",
            "expiryTime": "2099-12-31T23:59:59",
        }

    def fake_renew_access_token(client_id, current_token):
        return {
            "success": True,
            "accessToken": f"{client_id}-renewed-token-12345678901234567890",
            "expiryTime": "2099-12-31T23:59:59",
        }

    dhan_module.DhanClient.get_funds = fake_get_funds
    dhan_module.DhanClient.get_positions = fake_get_positions
    dhan_module.DhanClient.get_trades = fake_get_trades
    dhan_module.DhanClient.get_order_book = fake_get_order_book
    dhan_module.DhanClient.place_order = fake_place_order
    dhan_module.DhanClient.cancel_order = fake_cancel_order
    dhan_module.DhanClient.get_option_ltp = fake_get_option_ltp
    dhan_module.DhanClient.get_ltp_multi = fake_get_ltp_multi
    dhan_module.DhanClient.get_ohlc_multi = fake_get_ohlc_multi
    token_manager_module.generate_access_token = fake_generate_access_token
    token_manager_module.renew_access_token = fake_renew_access_token

    # Verify cache partitioning first.
    client_a = dhan_module.DhanClient(client_id="alpha-client", access_token="alpha-token-12345678901234567890")
    client_b = dhan_module.DhanClient(client_id="beta-client", access_token="beta-token-12345678901234567890")
    funds_a = client_a.get_funds_cached()
    funds_b = client_b.get_funds_cached()
    assert funds_a["availabelBalance"] != funds_b["availabelBalance"]

    refreshed_tokens: list[str] = []
    refreshable_client = dhan_module.DhanClient(
        client_id="alpha-client",
        access_token="alpha-stale-token-12345678901234567890",
        pin="246810",
        totp_secret="JBSWY3DPEHPK3PXP",
        token_update_cb=refreshed_tokens.append,
    )
    refreshed = refreshable_client.refresh_access_token(force=True)
    assert refreshed == "alpha-client-fresh-token-12345678901234567890"
    assert refreshed_tokens == [refreshed]

    # Replace live/scalp engines with lightweight test doubles.
    app_module.LiveEngine = DummyLiveEngine
    app_module._ScalpEngineClass = DummyScalpEngine
    app_module._HAS_SCALP = True
    app_module._market_feed = None
    app_module._fetch_nse_vix = lambda: {"price": 12.4, "prev_close": 11.8}
    app_module.ScripMaster.ensure_loaded = classmethod(lambda cls: None)
    app_module.ScripMaster.get_nearest_expiry = classmethod(lambda cls, symbol, from_date=None: "2026-03-26")
    app_module.ScripMaster.lookup = classmethod(
        lambda cls, symbol, strike, expiry, option_type: "900001" if option_type == "CE" else "900002"
    )
    app_module.live_engines.clear()
    app_module._live_tasks.clear()
    app_module._scalp_engines.clear()
    app_module._scalp_entry_locks.clear()
    app_module._last_scalp_entry_ts.clear()
    app_module._ticker_cache = {"data": None, "timestamp": 0, "ttl": 30}

    passed = 0
    passed += 1
    print("  1. DhanClient can refresh fixed per-user tokens: PASS")

    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as phil_client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as nobroker_client,
    ):
        admin_id = await _login(admin_client, "admin", "123456")
        r = await admin_client.post("/api/admin/users", json={"username": "phil", "password": "654321", "role": "user"})
        assert r.status_code == 200, r.text
        phil_id = int(r.json()["user_id"])
        r = await admin_client.post(
            "/api/admin/users",
            json={"username": "nobroker", "password": "777777", "role": "user"},
        )
        assert r.status_code == 200, r.text
        nobroker_id = int(r.json()["user_id"])

        await db.update_user(
            phil_id,
            dhan_client_id="phil-client",
            dhan_access_token="phil-token-12345678901234567890",
            dhan_pin="246810",
            dhan_totp_secret="JBSWY3DPEHPK3PXP",
        )

        assert await _login(phil_client, "phil", "654321") == phil_id
        assert await _login(nobroker_client, "nobroker", "777777") == nobroker_id
        assert await _login(admin_client, "admin", "123456") == admin_id

        r = await admin_client.post("/api/broker/check")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "connected"
        assert r.json()["source"] == "global"
        assert r.json()["available_balance"] == 111111.0
        passed += 1
        print("  2. Admin broker check uses global fallback: PASS")

        r = await phil_client.post("/api/broker/check")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "connected"
        assert r.json()["source"] == "user"
        assert r.json()["available_balance"] == 222222.0
        passed += 1
        print("  3. User broker check uses per-user credentials: PASS")

        r = await nobroker_client.post("/api/broker/check")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "not_configured"
        passed += 1
        print("  4. Non-admin without creds does not inherit global broker: PASS")

        r = await phil_client.get("/api/portfolio/summary")
        assert r.status_code == 200, r.text
        summary = r.json()
        assert summary["funds"]["availabelBalance"] == 222222.0
        assert summary["positions"][0]["symbol"] == "phil-client"
        passed += 1
        print("  5. Portfolio summary uses per-user broker account: PASS")

        r = await phil_client.get("/api/option-ltp?underlying=NIFTY&strike=23000&expiry=2026-03-26&option_type=CE")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"
        assert r.json()["ltp"] == 222.5
        passed += 1
        print("  6. Option LTP endpoint uses per-user broker client: PASS")

        app_module._ticker_cache = {"data": None, "timestamp": 0, "ttl": 30}
        r = await phil_client.get("/api/ticker")
        assert r.status_code == 200, r.text
        ticker = r.json()
        assert ticker["status"] == "ok"
        assert ticker["source"] == "dhan"
        assert ticker["nifty"]["price"] == 25175.0
        passed += 1
        print("  7. Ticker endpoint uses per-user broker client: PASS")

        r = await phil_client.post(
            "/api/orders/place",
            json={
                "security_id": "123",
                "exchange_segment": "NSE_EQ",
                "transaction_type": "BUY",
                "quantity": 1,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["client_id"] == "phil-client"
        passed += 1
        print("  8. Order placement uses per-user broker client: PASS")

        r = await admin_client.get("/api/orders")
        assert r.status_code == 200, r.text
        assert r.json()["data"][0]["orderId"] == "global-client-ORDER"
        passed += 1
        print("  9. Admin order book still uses global fallback: PASS")

        r = await admin_client.get("/api/option-ltp?underlying=NIFTY&strike=23000&expiry=2026-03-26&option_type=CE")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"
        assert r.json()["ltp"] == 101.25
        passed += 1
        print(" 10. Admin option LTP still uses global broker fallback: PASS")

        app_module._ticker_cache = {"data": None, "timestamp": 0, "ttl": 30}
        r = await admin_client.get("/api/ticker")
        assert r.status_code == 200, r.text
        ticker = r.json()
        assert ticker["status"] == "ok"
        assert ticker["nifty"]["price"] == 24025.0
        passed += 1
        print(" 11. Admin ticker still uses global broker fallback: PASS")

        r = await nobroker_client.get("/api/option-ltp?underlying=NIFTY&strike=23000&expiry=2026-03-26&option_type=CE")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "error"
        passed += 1
        print(" 12. Option LTP refuses users without broker creds: PASS")

        r = await phil_client.post(
            "/api/live/start",
            json={"strategy_config": {"run_name": "Phil Live", "instrument": "26000", "indicators": [], "legs": []}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "started"
        assert app_module.live_engines[phil_id]["Phil Live"].dhan.client_id == "phil-client"
        passed += 1
        print(" 13. Live engine startup injects user broker client: PASS")

        r = await nobroker_client.post(
            "/api/live/start",
            json={
                "strategy_config": {"run_name": "No Broker Live", "instrument": "26000", "indicators": [], "legs": []}
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "error"
        passed += 1
        print(" 14. Live engine refuses users without broker creds: PASS")

        scalp_body = {
            "underlying": "NIFTY",
            "strike": 23000,
            "option_type": "CE",
            "expiry": "2026-03-26",
            "transaction_type": "BUY",
            "lots": 1,
            "lot_size": 75,
            "mode": "live",
        }
        r = await phil_client.post("/api/scalp/entry", json=scalp_body)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"
        assert app_module._scalp_engines[phil_id].dhan.client_id == "phil-client"
        passed += 1
        print(" 15. Phil scalp engine uses per-user broker client: PASS")

        r = await admin_client.post("/api/scalp/entry", json=scalp_body)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"
        assert app_module._scalp_engines[admin_id].dhan.client_id == "global-client"
        passed += 1
        print(" 16. Admin scalp engine uses global broker fallback: PASS")

        assert phil_id in app_module._scalp_engines and admin_id in app_module._scalp_engines
        assert app_module._scalp_engines[phil_id] is not app_module._scalp_engines[admin_id]
        passed += 1
        print(" 17. Scalp engines are isolated per user: PASS")

        r = await nobroker_client.post("/api/scalp/entry", json=scalp_body)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "error"
        passed += 1
        print(" 18. Live scalp entry refuses users without broker creds: PASS")

        r = await phil_client.get("/api/user/profile")
        assert r.status_code == 200, r.text
        profile = r.json()
        assert profile["status"] == "ok"
        assert profile["user"]["username"] == "phil"
        assert profile["broker"]["configured"] is True
        assert profile["broker"]["source"] == "user"
        assert profile["broker"]["client_id"] == "phil-client"
        assert profile["broker"]["access_token_saved"] is True
        assert profile["broker"]["pin_saved"] is True
        assert profile["broker"]["totp_saved"] is True
        assert profile["broker"]["auto_refresh_ready"] is True
        passed += 1
        print(" 19. User profile exposes safe broker metadata: PASS")

        r = await admin_client.get("/api/user/profile")
        assert r.status_code == 200, r.text
        admin_profile = r.json()
        assert admin_profile["broker"]["source"] == "global"
        assert admin_profile["broker"]["configured"] is False
        passed += 1
        print(" 20. Admin profile reflects global broker fallback: PASS")

        r = await nobroker_client.put(
            "/api/user/broker",
            json={"client_id": "beta-client", "access_token": "beta-token-12345678901234567890"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["broker"]["configured"] is True
        r = await nobroker_client.post("/api/broker/check")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "connected"
        assert r.json()["source"] == "user"
        r = await nobroker_client.delete("/api/user/broker")
        assert r.status_code == 200, r.text
        assert r.json()["broker"]["configured"] is False
        r = await nobroker_client.post("/api/broker/check")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "not_configured"
        passed += 1
        print(" 21. User broker self-service save and clear works: PASS")

        r = await phil_client.put(
            "/api/user/broker",
            json={"client_id": "phil-client", "access_token": "phil-token-override-12345678901234567890"},
        )
        assert r.status_code == 409, r.text
        r = await phil_client.delete("/api/user/broker")
        assert r.status_code == 409, r.text
        passed += 1
        print(" 22. Active live workflows lock broker credential edits: PASS")

        r = await admin_client.get("/api/admin/engines")
        assert r.status_code == 200, r.text
        rows = {int(row["user_id"]): row for row in r.json()["users"]}
        assert rows[phil_id]["live_running"] == 1
        assert rows[phil_id]["scalp_open_trades"] == 1
        assert rows[admin_id]["scalp_open_trades"] == 1
        passed += 1
        print(" 23. Admin engine summary is user-scoped: PASS")

        r = await phil_client.post("/api/refresh-token")
        assert r.status_code == 200, r.text
        refreshed_payload = r.json()
        assert refreshed_payload["status"] == "ok"
        assert refreshed_payload["source"] == "user"
        refreshed_user = await db.get_user_by_id(phil_id)
        assert refreshed_user["dhan_access_token"] == "phil-client-fresh-token-12345678901234567890"
        passed += 1
        print(" 24. User refresh endpoint persists a refreshed token: PASS")

        await db.update_user(
            nobroker_id,
            dhan_client_id="stale-client",
            dhan_access_token="stale-token-12345678901234567890",
        )
        r = await nobroker_client.post("/api/broker/check")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "auth_error"
        assert r.json()["market_data_ok"] is False
        assert r.json()["auto_refresh_ready"] is False
        passed += 1
        print(" 25. Broker check detects market-data auth failures: PASS")

    print(f"\n{'=' * 40}")
    print(f"  Results: {passed} passed, 0 failed")
    print(f"{'=' * 40}")

    if TEST_DB.exists():
        TEST_DB.unlink()
    if TEST_USER_DATA.exists():
        shutil.rmtree(TEST_USER_DATA)


if __name__ == "__main__":
    asyncio.run(main())
