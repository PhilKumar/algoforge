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

# 15m NIFTY walk on 2026-07-20: two reds through L4, fill, two reds through
# L8, fill, then the snap back through the 0.25 target.
D = date(2026, 7, 20)


def _dt(hh, mm):
    return datetime(D.year, D.month, D.day, hh, mm)


INDEX_CANDLES = {
    datetime(2026, 7, 17, 14, 15): (24300.0, MOTHER_HIGH, MOTHER_LOW, 24290.0),
    _dt(9, 15): (24100.0, 24110.0, 24000.0, 24010.0),
    _dt(9, 30): (24010.0, 24015.0, 23950.0, 23960.0),
    _dt(9, 45): (23960.0, 24050.0, 23940.0, 24040.0),
    _dt(10, 0): (24040.0, 24045.0, 23650.0, 23660.0),
    _dt(10, 15): (23660.0, 23665.0, 23600.0, 23610.0),
    _dt(10, 30): (23610.0, 23640.0, 23600.0, 23630.0),
    _dt(10, 45): (23630.0, 23920.0, 23620.0, 23900.0),
}

# Two deliberately thin minutes: the L8 fill (10:30) has no bar — nearest is
# 10:27, priced from the last trade — and the exit (10:45) first prints at
# 10:47, priced from the next trade inside the exit candle.  Neither gaps.
OPTION_MINUTES = {_dt(9, 45): 500.0, _dt(10, 27): 300.0, _dt(10, 47): 520.0}


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
                        "mother_timestamp": MOTHER_AT,
                        "side": "CE",
                        "timeframe": "15m",
                        "rung_inr": 75000,
                        "itm_steps": 2,
                    },
                )
            ]
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        result = body["result"]

        # The whole point: real numbers on the panel, nothing withheld.
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(all(entry["option_price"] is not None for entry in result["entries"]))
        self.assertEqual(result["data_gaps"], [])
        self.assertEqual(result["premium_failures"], [])
        self.assertIsNotNone(result["net_pnl"])
        self.assertIsNotNone(result["gross_pnl"])
        self.assertGreater(result["costs_total"], 0)
        self.assertAlmostEqual(result["gross_pnl"], (520 - 500) * 65 + (520 - 300) * 2 * 65, places=2)

        # The L8 fill minute had no bar (priced from the 10:27 trade) and the
        # exit's first print came 2 min into its candle — both disclosed,
        # neither withholding anything.
        self.assertEqual(len(result["premium_stale_fills"]), 3)
        notes = "\n".join(result["premium_stale_fills"])
        self.assertIn("3 min earlier", notes)
        self.assertIn("2 min into the candle", notes)

        # The mother came off the stub Dhan frame, not typed numbers.
        self.assertAlmostEqual(body["mother"]["high"], MOTHER_HIGH)
        self.assertAlmostEqual(body["mother"]["low"], MOTHER_LOW)

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
                [("/api/fib-boundary/backtest", {"mother_timestamp": MOTHER_AT, "side": "CE", "timeframe": "15m"})]
            )
        finally:
            app_module._request_broker_context = previous

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["status"], "data_gap")
        self.assertTrue(result["premium_failures"], result)
        self.assertIn("DH-901", result["premium_failures"][0])


if __name__ == "__main__":
    unittest.main()
