"""The fib-space paper routes: paper-only, one run at a time, honest failures.

The design's whole claim rests on the live run making the SAME decisions the
backtest measured, so the route must refuse to start on terms it cannot size
rather than start on a guess.
"""

import json
import os
import shutil
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = Path("/tmp/philforge-test-fib-space-routes.db")
TEST_USER_DATA = Path("/tmp/philforge-test-fib-space-routes-data")

os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zH5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""

from fastapi import HTTPException  # noqa: E402

import app as app_module  # noqa: E402


class _Request:
    """Enough of a Request for these routes: a user id and a JSON body."""

    def __init__(self, user_id: int = 7, body: dict | None = None):
        self.state = SimpleNamespace(user_id=user_id)
        self._body = json.dumps(body or {}).encode()

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body or b"{}")


class _Contract:
    def __init__(self):
        self.strike = 57_000.0
        self.expiry = date(2026, 3, 26)
        self.lot_size = 35
        self.security_id = "12345"


class _Adapter:
    """A paper adapter that can size a campaign."""

    def __init__(self):
        self.paper_only = True

    def select_campaign_contract(self, **kwargs):
        return _Contract()

    def get_ticker(self, symbol="BANKNIFTY"):
        return {"symbol": symbol, "last_price": 57_200.0, "mark_price": 57_200.0}

    async def async_get_candles(self, *a, **k):
        return []


class FibSpacePaperRouteTests(unittest.IsolatedAsyncioTestCase):
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
        app_module._fib_space_engines.clear()
        await app_module._db_mod.init_db()

    async def asyncTearDown(self):
        for runtime in app_module._fib_space_engines.values():
            runtime.running = False
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
        app_module._fib_space_engines.clear()

    def _broker(self, adapter=None):
        """Patch the broker context and the adapter the route constructs."""
        return (
            patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, object(), "user"))),
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: adapter or _Adapter()),
        )

    async def test_status_before_anything_started(self):
        result = await app_module.fib_space_paper_status(_Request())
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["mode"], "paper")

    async def test_start_defaults_to_banknifty_and_reports_the_real_lot(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            result = await app_module.fib_space_paper_start(_Request())

        self.assertEqual(result["status"], "started")
        self.assertEqual(result["symbol"], "banknifty")
        self.assertEqual(result["mode"], "paper")
        # Lot comes from the chain (35), NOT the backtest's flat 30.
        self.assertEqual(result["lot_size"], 35)

    async def test_an_unmeasured_symbol_is_refused(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            with self.assertRaises(HTTPException) as caught:
                await app_module.fib_space_paper_start(_Request(body={"symbol": "sensex"}))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Measured symbols", caught.exception.detail)

    async def test_a_second_run_is_refused_while_one_is_going(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())
            with self.assertRaises(HTTPException) as caught:
                await app_module.fib_space_paper_start(_Request())
        self.assertEqual(caught.exception.status_code, 409)

    async def test_start_without_a_broker_is_refused(self):
        with patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, None, "user"))):
            with self.assertRaises(HTTPException) as caught:
                await app_module.fib_space_paper_start(_Request())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Connect a Dhan account", caught.exception.detail)

    async def test_a_chain_that_cannot_size_the_campaign_refuses_to_start(self):
        """Better no run than a run whose every quantity is a guess."""

        class _Unsizeable(_Adapter):
            def select_campaign_contract(self, **kwargs):
                raise RuntimeError("ScripMaster has no lot size for BANKNIFTY 2026-03-26")

        broker_patch, adapter_patch = self._broker(_Unsizeable())
        with broker_patch, adapter_patch:
            with self.assertRaises(HTTPException) as caught:
                await app_module.fib_space_paper_start(_Request())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Cannot size", caught.exception.detail)
        self.assertEqual(app_module._fib_space_engines, {})

    async def test_stop_is_idempotent_and_reports_the_symbol(self):
        self.assertEqual((await app_module.fib_space_paper_stop(_Request()))["status"], "not_running")

        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())
        stopped = await app_module.fib_space_paper_stop(_Request())

        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["symbol"], "banknifty")
        self.assertFalse(app_module._fib_space_engines[7].running)

    async def test_status_describes_a_running_book(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())
        status = await app_module.fib_space_paper_status(_Request())

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["symbol"], "banknifty")
        self.assertTrue(status["running"])
        self.assertEqual(status["book"]["entry_timeframe"], "5m")
        self.assertEqual(status["book"]["geometry_timeframe"], "15m")
        self.assertEqual(status["book"]["campaigns"], 0)

    async def test_the_run_survives_a_restart_as_persisted_state(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())

        raw = await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7))
        self.assertIsNotNone(raw)
        saved = json.loads(raw)
        self.assertEqual(saved["symbol"], "banknifty")
        self.assertTrue(saved["running"])

    async def test_the_adapter_is_always_constructed_paper_only(self):
        seen = {}

        def _capture(broker, **kwargs):
            seen.update(kwargs)
            return _Adapter()

        with (
            patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, object(), "user"))),
            patch.object(app_module, "CascadeOptionsAdapter", _capture),
        ):
            await app_module.fib_space_paper_start(_Request())

        self.assertIs(seen.get("paper_only"), True)


class ConfigReachesTheRunTests(unittest.IsolatedAsyncioTestCase):
    """The measured terms must actually arrive at the host."""

    async def asyncSetUp(self):
        app_module._fib_space_engines.clear()

    async def asyncTearDown(self):
        app_module._fib_space_engines.clear()

    async def test_banknifty_runs_with_no_cooldown_and_nifty_with_three_days(self):
        adapter = _Adapter()
        broker = object()
        with patch.object(app_module, "_cascade_premium_lookup", lambda b: lambda w, c: 100.0):
            bn = app_module._build_fib_space_host("banknifty", adapter, broker)
            nf = app_module._build_fib_space_host("nifty", adapter, broker)

        self.assertEqual(bn.book.cooldown_days, 0)
        self.assertEqual(nf.book.cooldown_days, 3)

    async def test_geometry_is_15m_and_entries_are_5m(self):
        with patch.object(app_module, "_cascade_premium_lookup", lambda b: lambda w, c: 100.0):
            host = app_module._build_fib_space_host("banknifty", _Adapter(), object())
        self.assertEqual(host.geometry_timeframe, "15m")
        self.assertEqual(host.entry_timeframe, "5m")

    async def test_the_strike_request_is_atm_minus_two_at_the_symbol_step(self):
        captured = {}

        class _Recording(_Adapter):
            def select_campaign_contract(self, **kwargs):
                captured.update(kwargs)
                return _Contract()

        with patch.object(app_module, "_cascade_premium_lookup", lambda b: lambda w, c: 100.0):
            host = app_module._build_fib_space_host("banknifty", _Recording(), object())
        host.book.select_contract(datetime(2026, 3, 3, 11, 0), 57_240.0)

        self.assertEqual(captured["ce_offset_steps"], -2)
        self.assertEqual(captured["strike_step"], 100)
        self.assertEqual(captured["option_type"], "CE")
        self.assertEqual(captured["symbol"], "BANKNIFTY")


if __name__ == "__main__":
    unittest.main()


class RestoreAfterDeployTests(unittest.IsolatedAsyncioTestCase):
    """Deploys are frequent; a run that dies on one leaves a gap in the record."""

    async def asyncSetUp(self):
        app_module._db_mod._initialized = False
        app_module._fib_space_engines.clear()
        await app_module._db_mod.init_db()

    async def asyncTearDown(self):
        for runtime in app_module._fib_space_engines.values():
            runtime.running = False
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
        app_module._fib_space_engines.clear()

    async def _save(self, payload):
        await app_module._db_mod.set_app_state(app_module._fib_space_state_key(7), json.dumps(payload))

    async def test_a_running_run_comes_back(self):
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})
        with (
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()),
            patch.object(app_module, "_cascade_premium_lookup", lambda b: lambda w, c: 100.0),
        ):
            runtime = await app_module._restore_fib_space_paper_run(7, object())

        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.symbol, "banknifty")
        self.assertEqual(runtime.started_at, datetime(2026, 3, 3, 9, 15))

    async def test_a_stopped_run_stays_stopped(self):
        await self._save({"symbol": "banknifty", "running": False, "started_at": "2026-03-03T09:15:00"})
        with patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()):
            self.assertIsNone(await app_module._restore_fib_space_paper_run(7, object()))
        self.assertEqual(app_module._fib_space_engines, {})

    async def test_an_unknown_symbol_is_not_restored(self):
        await self._save({"symbol": "sensex", "running": True, "started_at": "2026-03-03T09:15:00"})
        with patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()):
            self.assertIsNone(await app_module._restore_fib_space_paper_run(7, object()))

    async def test_no_broker_means_no_restore(self):
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})
        self.assertIsNone(await app_module._restore_fib_space_paper_run(7, None))

    async def test_an_unsizeable_chain_leaves_the_saved_state_for_next_time(self):
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})

        class _Unsizeable(_Adapter):
            def select_campaign_contract(self, **kwargs):
                raise RuntimeError("ScripMaster not loaded yet")

        with patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Unsizeable()):
            self.assertIsNone(await app_module._restore_fib_space_paper_run(7, object()))

        raw = await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7))
        self.assertTrue(json.loads(raw)["running"], "saved state must survive so the next restart can retry")

    async def test_it_does_not_double_start_an_already_running_run(self):
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})
        with (
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()),
            patch.object(app_module, "_cascade_premium_lookup", lambda b: lambda w, c: 100.0),
        ):
            first = await app_module._restore_fib_space_paper_run(7, object())
            second = await app_module._restore_fib_space_paper_run(7, object())

        self.assertIsNotNone(first)
        self.assertIsNone(second)
