"""End-to-end: the Backtest button's route returns rupee P&L, not a fake gap.

The 2026-08-01 failure in one test: a recent mother buys a still-listed
contract, so every premium comes from Dhan — whose frame is stamped with naive
timestamps while the engine replays IST-aware candles.  Before the fix the
route answered `status: data_gap`, zero legs, "missing option candle at
10:00", P&L withheld — while Dhan HAD the 10:00 bar.

This test drives the REAL route end to end — login, auth middleware, payload
validation, index normalisation (which is what makes the candles tz-aware),
contract resolution, the hybrid premium lookup, the engine, the serializer —
against a stub DhanClient serving frames shaped exactly like Dhan's.  Only
the network is fake.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import app as app_module  # noqa: E402
import config  # noqa: E402
import db as db_module  # noqa: E402
import upstox_token_manager  # noqa: E402
from data import cascade_upstox  # noqa: E402

MOTHER_AT = "2026-07-17T14:15"
MOTHER_HIGH, MOTHER_LOW = 24367.30, 24280.55  # L4 = 24,020.30 · L8 = 23,673.30

# A 15m NIFTY walk on 2026-07-20 shaped for the SWING TOUCH LADDER: a low,
# two greens that freeze it, a bounce, two reds that freeze the high, the touch
# of L2, and the snap back through the 0.25 target.
#
#   swing low 24,000 · swing high 24,100 · span 100
#   L2 = 24,100 - 2x100 = 23,900   target = 23,900 + 0.25x200 = 23,950
D = date(2026, 7, 20)


def _dt(hh, mm):
    return datetime(D.year, D.month, D.day, hh, mm)


INDEX_CANDLES = {
    datetime(2026, 7, 17, 14, 15): (24300.0, MOTHER_HIGH, MOTHER_LOW, 24290.0),
    _dt(9, 15): (24100.0, 24110.0, 24000.0, 24010.0),  # red   -> low 24,000
    _dt(9, 30): (24010.0, 24060.0, 24005.0, 24050.0),  # green
    _dt(9, 45): (24050.0, 24080.0, 24040.0, 24075.0),  # green -> LOW frozen
    _dt(10, 0): (24075.0, 24100.0, 24070.0, 24095.0),  # green -> high 24,100
    _dt(10, 15): (24095.0, 24098.0, 24080.0, 24085.0),  # red
    # Second red freezes the high AND wicks through L2 in the same bar.
    _dt(10, 30): (24085.0, 24088.0, 23890.0, 23900.0),  # red   -> HIGH frozen + L2 fill
    _dt(10, 45): (23900.0, 23960.0, 23895.0, 23955.0),  # green
    # The target runs to the MOTHER now (24,367.30), not to the swing high, so
    # it sits at 24,018.33 -- further than the old anchor-based one and out of
    # reach of the bar above. This is the bar that pays.
    _dt(11, 0): (23955.0, 24030.0, 23950.0, 24025.0),  # green -> target 24,018.33
}

# One deliberately thin minute: the L2 fill (10:30) has no bar of its own and
# is priced from the last real trade at 10:27 -- disclosed, never a gap. The
# exit minute does print, so the round settles.
OPTION_MINUTES = {_dt(9, 45): 500.0, _dt(10, 27): 300.0, _dt(10, 45): 520.0, _dt(11, 0): 545.0}


class _StubDhanClient:
    def get_historical_data(self, security_id, exchange_segment, instrument_type, *args, **kwargs):
        if instrument_type == "INDEX":
            index = pd.DatetimeIndex(sorted(INDEX_CANDLES))
            rows = [INDEX_CANDLES[m] for m in sorted(INDEX_CANDLES)]
            return pd.DataFrame(
                {
                    "open": [r[0] for r in rows],
                    "high": [r[1] for r in rows],
                    "low": [r[2] for r in rows],
                    "close": [r[3] for r in rows],
                },
                index=index,
            )
        # Every option contract shares one deliberately thin minute series,
        # stamped naive — exactly how Dhan's frame arrives.
        index = pd.DatetimeIndex(sorted(OPTION_MINUTES))
        return pd.DataFrame({"open": [OPTION_MINUTES[m] for m in sorted(OPTION_MINUTES)]}, index=index)


class _StubScripMaster:
    @classmethod
    def get_expiries(cls, _instrument):
        return ["2026-07-28", "2026-08-04", "2026-08-25"]

    @classmethod
    def lookup(cls, _instrument, strike, _expiry, _option_type):
        return f"stub-{int(strike)}"


class _NoUpstox:
    """A recent mother's contract is still listed; Upstox has nothing."""

    def __init__(self, *args, **kwargs):
        raise cascade_upstox.UpstoxAccessError("stubbed out for the e2e")


class FibBacktestRouteE2ETests(unittest.TestCase):
    @classmethod
    def _swap(cls, holder, attribute, value):
        """Patch holder.attribute and guarantee restoration even if setUpClass
        dies half-way -- a leaked patch here once failed the upstox tests."""
        original = getattr(holder, attribute)
        setattr(holder, attribute, value)
        cls.addClassCleanup(setattr, holder, attribute, original)

    PIN = "e2e-pin-123456"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls._swap(config, "DB_PATH", os.path.join(cls._tmp.name, "e2e.db"))
        cls._swap(config, "OPTION_ARCHIVE_ROOT", os.path.join(cls._tmp.name, "option-archive"))
        # AUTH_PASSWORD is frozen at app-import time from PHILFORGE_PIN, and
        # which test file imports app first varies by collection order — so
        # patch the module global, never the environment.
        cls._swap(app_module, "AUTH_PASSWORD", cls.PIN)
        # Another test may have initialised the schema at ITS path already;
        # the module flag would make init a no-op at ours.
        cls._swap(db_module, "_initialized", False)
        cls._swap(app_module, "_SKIP_STARTUP_JOBS", True)
        cls._swap(app_module, "ScripMaster", _StubScripMaster)
        cls._swap(cascade_upstox, "UpstoxPremiumSource", _NoUpstox)
        cls._swap(upstox_token_manager, "ensure_fresh_token", lambda *a, **k: None)

        stub = _StubDhanClient()

        async def _broker_context(_request):
            return {"id": 1, "username": "admin"}, stub, "e2e-stub"

        cls._swap(app_module, "_request_broker_context", _broker_context)

        # The installed httpx dropped TestClient's `app=` kwarg, so drive the
        # ASGI app directly; run the DB-init startup handler ourselves.
        asyncio.run(app_module._init_database())

    @classmethod
    def _post(cls, calls: list[tuple[str, dict]]) -> list[httpx.Response]:
        """Login, then POST each (path, json) with the session cookie kept."""

        async def _run():
            transport = httpx.ASGITransport(app=app_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://e2e.local") as client:
                login = await client.post("/api/auth/login", json={"username": "admin", "password": cls.PIN})
                assert login.status_code == 200, login.text
                return [await client.post(path, json=payload) for path, payload in calls]

        return asyncio.run(_run())

    def test_a_dhan_priced_backtest_returns_rupee_pnl(self):
        [response] = self._post(
            [
                (
                    "/api/fib-boundary/backtest",
                    {
                        "symbol": "NIFTY",
                        "mother_timestamp": MOTHER_AT,
                        "side": "CE",
                        "timeframe": "15m",
                        "capital_cap_inr": 75000,
                        "itm_steps": 2,
                    },
                )
            ]
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        # It is the SAME engine the Start button trades, not a second one.
        self.assertEqual(body["engine"], "fib_touch_ladder")
        self.assertEqual(body["pricing"], "recorded_history")

        campaign = body["campaign"]
        # The swing the ladder measured from, found by the engine -- not typed.
        # The fib the trendline rule draws: 0 = the touch high 24,100,
        # 1 = the low that broke, 24,005. Neither is the mother's own high.
        # The fib the trendline rule draws through this route: 0 = the touch
        # high 24,098, 1 = the low that broke, 24,000. Neither is the mother's
        # own high, which is the whole point of measuring off the structure.
        self.assertEqual(campaign["anchor"]["low"], 24_000.0)
        self.assertEqual(campaign["anchor"]["high"], 24_098.0)
        self.assertEqual(campaign["anchor"]["span"], 98.0)

        # The whole point: real numbers on the panel, nothing withheld.
        # The round banks and the mother PARKS rather than ending: a new
        # deepest low would trade it again from the same candle.
        self.assertEqual(campaign["status"], "WAITING_NEW_LOW")
        self.assertEqual(len(campaign["rounds"]), 1)
        self.assertEqual(campaign["rounds"][0]["exit_reason"], "target")
        # The legs live on the ROUND once it banks -- `fills` is the open
        # position, and this one is closed.
        self.assertEqual(campaign["fills"], [])
        self.assertEqual(len(campaign["rounds"][0]["fills"]), 1)
        fill = campaign["rounds"][0]["fills"][0]
        self.assertEqual(fill["level"], 2)
        self.assertEqual(fill["index_price"], 23_902.0)  # F1L2 = 24,098 - 2 x 98
        self.assertEqual(fill["premium"], 300.0)  # the 10:27 trade, 3 min back
        self.assertEqual(fill["quantity"], 65)
        self.assertEqual(campaign["data_gaps"], [])
        self.assertEqual(body["premium_failures"], [])
        self.assertAlmostEqual(campaign["gross_pnl"], (545 - 300) * 65, places=2)
        self.assertIsNotNone(campaign["net_pnl"])
        self.assertGreater(campaign["costs_total"], 0)
        # Net is gross minus real statutory cost, never the same number.
        self.assertLess(campaign["net_pnl"], campaign["gross_pnl"])

        # The fill minute had no bar of its own and was priced from a real
        # neighbouring trade -- disclosed, not silently substituted.
        notes = "\n".join(body["premium_stale_fills"])
        self.assertIn("3 min earlier", notes)

    def test_a_dead_premium_source_is_named_not_disguised_as_a_gap(self):
        broken = _StubDhanClient()

        def _boom(*_args, **_kwargs):
            raise Exception("DH-901 token expired")

        broken.get_historical_data = lambda sec, seg, kind, *a, **k: (
            _boom() if kind != "INDEX" else _StubDhanClient().get_historical_data(sec, seg, kind)
        )

        async def _broker_context(_request):
            return {"id": 1, "username": "admin"}, broken, "e2e-stub"

        previous = app_module._request_broker_context
        app_module._request_broker_context = _broker_context
        try:
            [response] = self._post(
                [
                    (
                        "/api/fib-boundary/backtest",
                        {"symbol": "NIFTY", "mother_timestamp": MOTHER_AT, "side": "CE", "timeframe": "15m"},
                    )
                ]
            )
        finally:
            app_module._request_broker_context = previous

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # A dead token is NOT a market gap. The route has to say which one
        # happened, or a broken replay reads as "the market did not trade".
        self.assertTrue(body["premium_failures"], body)
        self.assertIn("DH-901", "\n".join(body["premium_failures"]))
        # And nothing is invented: no fill, no P&L.
        self.assertEqual(body["campaign"]["fills"], [])
        self.assertIsNone(body["campaign"]["net_pnl"])


if __name__ == "__main__":
    unittest.main()
