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
from datetime import date, datetime, timedelta
from datetime import time as dt_time
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
    """RETIRED 2026-08-16. Fib Space was folded into Fib Boundary and its tab
    removed, so there is no screen a restored run could appear on and no button
    that could stop it -- it would just poll Dhan every session, unseen. These
    tests used to prove the opposite; they now hold the retirement in place."""

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

    async def test_a_running_run_is_not_brought_back(self):
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})
        with (
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()),
            patch.object(app_module, "_cascade_premium_lookup", lambda b: lambda w, c: 100.0),
        ):
            self.assertIsNone(await app_module._restore_fib_space_paper_run(7, object()))
        self.assertEqual(app_module._fib_space_engines, {}, "nothing may be left polling")

    async def test_the_saved_flag_is_cleared_rather_than_ignored(self):
        """Left set, it would silently wake the run again the day somebody
        restored the restore."""
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})
        with patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()):
            await app_module._restore_fib_space_paper_run(7, object())
        raw = await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7))
        self.assertFalse(json.loads(raw)["running"])

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

    async def test_a_run_started_by_hand_is_left_alone(self):
        """The routes still answer, so a run started deliberately in this
        process keeps going. Only the automatic restart is gone."""
        await self._save({"symbol": "banknifty", "running": True, "started_at": "2026-03-03T09:15:00"})
        app_module._fib_space_engines[7] = object()
        try:
            self.assertIsNone(await app_module._restore_fib_space_paper_run(7, object()))
            raw = await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7))
            self.assertTrue(json.loads(raw)["running"], "a live run's own state is not rewritten")
        finally:
            app_module._fib_space_engines.clear()


class NamedMotherRouteTests(unittest.IsolatedAsyncioTestCase):
    """Giving the engine a mother candle -- the primary way in."""

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

    async def _start(self):
        with (
            patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, object(), "user"))),
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()),
        ):
            return await app_module.fib_space_paper_start(_Request())

    def _aligned_past_15m(self):
        """A completed 15m candle open inside the last session-ish window."""
        now = datetime.now(app_module.IST).replace(tzinfo=None)
        stamp = now.replace(hour=10, minute=15, second=0, microsecond=0)
        if stamp + timedelta(minutes=15) > now:
            stamp -= timedelta(days=1)
        return stamp

    async def test_a_mother_needs_a_running_book_to_go_into(self):
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_mother(
                _Request(body={"mother_timestamp": self._aligned_past_15m().strftime("%Y-%m-%dT%H:%M")})
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Start the fib-space paper run first", caught.exception.detail)

    async def test_a_future_mother_is_refused(self):
        await self._start()
        ahead = (datetime.now(app_module.IST).replace(tzinfo=None) + timedelta(days=1)).replace(
            hour=10, minute=15, second=0, microsecond=0
        )
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_mother(
                _Request(body={"mother_timestamp": ahead.strftime("%Y-%m-%dT%H:%M")})
            )
        self.assertEqual(caught.exception.status_code, 400)

    async def test_a_mother_off_the_15m_grid_is_refused(self):
        await self._start()
        stamp = self._aligned_past_15m().replace(minute=22)
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_mother(
                _Request(body={"mother_timestamp": stamp.strftime("%Y-%m-%dT%H:%M")})
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("15m candle open", caught.exception.detail)

    async def test_a_mother_outside_the_session_is_refused(self):
        await self._start()
        stamp = self._aligned_past_15m().replace(hour=17, minute=0)
        # 17:00 on the helper's day is only in the PAST after 17:00 IST — run
        # this suite mid-afternoon and the future-check fires before the
        # session-check, failing the test until evening. Roll back a day so
        # the timestamp is always past and the refusal under test is the one
        # that answers.
        now = datetime.now(app_module.IST).replace(tzinfo=None)
        if stamp > now:
            stamp -= timedelta(days=1)
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_mother(
                _Request(body={"mother_timestamp": stamp.strftime("%Y-%m-%dT%H:%M")})
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("09:15", caught.exception.detail)

    async def test_a_mother_older_than_the_window_is_refused_with_the_reason(self):
        await self._start()
        old = self._aligned_past_15m() - timedelta(days=60)
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_mother(_Request(body={"mother_timestamp": old.strftime("%Y-%m-%dT%H:%M")}))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("cannot be quoted", caught.exception.detail)

    async def test_a_timestamp_dhan_has_no_candle_for_is_a_400_not_an_invented_bar(self):
        await self._start()
        stamp = self._aligned_past_15m()
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_mother(
                _Request(body={"mother_timestamp": stamp.strftime("%Y-%m-%dT%H:%M")})
            )
        # The stub adapter serves no candles, so this is the "no such bar" path.
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("no closed", caught.exception.detail)

    async def test_the_start_route_reports_the_scan_mode(self):
        result = await self._start()
        self.assertIn("auto_scan", result)
        self.assertFalse(result["auto_scan"], "naming mothers is the default; scanning is opt-in")

    async def test_auto_scan_can_still_be_asked_for(self):
        with (
            patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, object(), "user"))),
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()),
        ):
            result = await app_module.fib_space_paper_start(_Request(body={"auto_scan": True}))
        self.assertTrue(result["auto_scan"])
        self.assertTrue(app_module._fib_space_engines[7].host.auto_scan)

    async def test_named_mothers_are_persisted_so_a_deploy_cannot_lose_them(self):
        await self._start()
        runtime = app_module._fib_space_engines[7]

        from engine.fib_space_geometry import Bar

        bar = Bar(index=0, timestamp=datetime(2026, 3, 3, 10, 15), open=57000, high=57200, low=56900, close=56950)
        runtime.host.book.adopt_manual_mother(bar)
        await app_module._save_fib_space_state(7, runtime)

        saved = json.loads(await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7)))
        self.assertEqual(saved["manual_mothers"], ["2026-03-03T10:15:00"])
        self.assertFalse(saved["auto_scan"])


class CampaignDetailRouteTests(unittest.IsolatedAsyncioTestCase):
    """Premiums, capital and the chart, per campaign."""

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

    async def _start(self):
        with (
            patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, object(), "user"))),
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: _Adapter()),
        ):
            await app_module.fib_space_paper_start(_Request())
        return app_module._fib_space_engines[7]

    async def test_detail_without_a_run_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_campaign("anything", _Request())
        self.assertEqual(caught.exception.status_code, 409)

    async def test_an_unknown_campaign_is_a_404(self):
        await self._start()
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_campaign("banknifty:20260101T0915", _Request())
        self.assertEqual(caught.exception.status_code, 404)

    async def test_detail_reports_the_money(self):
        runtime = await self._start()
        from engine.fib_space_geometry import Bar

        bar = Bar(index=0, timestamp=datetime(2026, 3, 3, 10, 15), open=57000, high=57200, low=56900, close=56950)
        campaign = runtime.host.book.adopt_manual_mother(bar)

        result = await app_module.fib_space_paper_campaign(campaign.campaign_id, _Request())
        detail = result["campaign"]
        self.assertEqual(result["mode"], "paper")
        self.assertEqual(detail["source"], "manual")
        self.assertIn("capital_spent", detail)
        self.assertIn("capital_open", detail)
        self.assertEqual(detail["rounds"], [], "nothing bought yet")

    async def test_deleting_a_campaign_frees_its_mother_to_be_named_again(self):
        """The reason the button exists.

        A campaign recorded before the driver could read recorded candles has
        real fills and no rupees, and adopt_manual_mother rightly refuses to
        open a second campaign on the same mother — so without a delete there is
        no way to re-run it properly.
        """
        runtime = await self._start()
        from engine.fib_space_geometry import Bar

        bar = Bar(index=0, timestamp=datetime(2026, 3, 3, 10, 15), open=57000, high=57200, low=56900, close=56950)
        campaign = runtime.host.book.adopt_manual_mother(bar)
        with self.assertRaises(ValueError):
            runtime.host.book.adopt_manual_mother(bar)

        result = await app_module.fib_space_paper_campaign_delete(_Request(body={"campaign_id": campaign.campaign_id}))

        self.assertEqual(result["status"], "deleted")
        self.assertNotIn(campaign.campaign_id, runtime.host.book.campaigns)
        # The whole point: the mother is free again.
        again = runtime.host.book.adopt_manual_mother(bar)
        self.assertEqual(again.campaign_id, campaign.campaign_id)

    async def test_a_deleted_campaign_does_not_come_back_on_restart(self):
        """The named mother is persisted, so deleting must rewrite that record.

        Otherwise the next start re-adopts it and the trade the trader just
        removed reappears with nothing on screen to explain it.
        """
        runtime = await self._start()
        from engine.fib_space_geometry import Bar

        bar = Bar(index=0, timestamp=datetime(2026, 3, 3, 10, 15), open=57000, high=57200, low=56900, close=56950)
        campaign = runtime.host.book.adopt_manual_mother(bar)
        await app_module._save_fib_space_state(7, runtime)
        saved = json.loads(await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7)))
        self.assertIn("2026-03-03T10:15:00", saved["manual_mothers"])

        await app_module.fib_space_paper_campaign_delete(_Request(body={"campaign_id": campaign.campaign_id}))

        saved = json.loads(await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7)))
        self.assertEqual(saved["manual_mothers"], [])

    async def test_deleting_an_unknown_campaign_is_a_404(self):
        await self._start()
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_campaign_delete(_Request(body={"campaign_id": "banknifty:20260101T0915"}))
        self.assertEqual(caught.exception.status_code, 404)

    async def test_deleting_without_a_run_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_campaign_delete(_Request(body={"campaign_id": "anything"}))
        self.assertEqual(caught.exception.status_code, 409)

    async def test_a_chart_with_no_replay_yet_is_a_409_not_an_empty_canvas(self):
        runtime = await self._start()
        from engine.fib_space_geometry import Bar

        bar = Bar(index=0, timestamp=datetime(2026, 3, 3, 10, 15), open=57000, high=57200, low=56900, close=56950)
        campaign = runtime.host.book.adopt_manual_mother(bar)

        with self.assertRaises(HTTPException) as caught:
            await app_module.fib_space_paper_chart(campaign.campaign_id, _Request())
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("no replay yet", caught.exception.detail)


class _CandleAdapter(_Adapter):
    """A paper adapter with real 15m bars behind it, so a mother can be named."""

    def __init__(self, mother: datetime):
        super().__init__()
        self.mother = mother

    async def async_get_candles(self, symbol, timeframe, *, from_date=None, to_date=None, now=None):
        step = 15 if timeframe == "15m" else 5
        first = self.mother - timedelta(minutes=step * 40)
        out = []
        for i in range(120):
            stamp = first + timedelta(minutes=step * i)
            if not (dt_time(9, 15) <= stamp.time() <= dt_time(15, 30)):
                continue
            base = 57_000.0 - i
            out.append(
                SimpleNamespace(
                    timestamp=stamp, open=base, high=base + 30.0, low=base - 30.0, close=base - 5.0, volume=100
                )
            )
        return out


class NamedMotherSurvivesTests(unittest.IsolatedAsyncioTestCase):
    """A mother named by hand must outlive a restart and a stop/start.

    It did not. The poll loop saved only when something CHANGED -- a fill, an
    exit, a halt, an auto-scanned mother -- so with auto-scan off a named mother
    that had not filled yet was never written to disk; and the start route saved
    its brand-new EMPTY book, overwriting any mother that had been saved. Name a
    mother in the evening, come back next day, the book was empty.
    """

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
        # Yesterday's 10:00 bar: closed, inside the session, on a 15m open.
        self.mother = (datetime.now(app_module.IST).replace(tzinfo=None) - timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        self.adapter = _CandleAdapter(self.mother)

    async def asyncTearDown(self):
        for runtime in app_module._fib_space_engines.values():
            runtime.running = False
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
        app_module._fib_space_engines.clear()

    def _broker(self):
        return (
            patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 7}, object(), "user"))),
            patch.object(app_module, "CascadeOptionsAdapter", lambda *a, **k: self.adapter),
        )

    async def _saved(self):
        raw = await app_module._db_mod.get_app_state(app_module._fib_space_state_key(7))
        return json.loads(raw) if raw else {}

    async def _name_mother(self):
        return await app_module.fib_space_paper_mother(_Request(body={"mother_timestamp": self.mother.isoformat()}))

    async def test_naming_a_mother_writes_it_to_disk_immediately(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())
            await self._name_mother()

        saved = await self._saved()
        self.assertEqual(saved["manual_mothers"], [self.mother.isoformat()])

    async def test_restarting_the_run_does_not_erase_the_named_mother(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())
            await self._name_mother()
            await app_module.fib_space_paper_stop(_Request())
            app_module._fib_space_engines.clear()
            result = await app_module.fib_space_paper_start(_Request())

        self.assertEqual(result["readopted_mothers"], 1)
        saved = await self._saved()
        self.assertEqual(saved["manual_mothers"], [self.mother.isoformat()])
        runtime = app_module._fib_space_engines[7]
        self.assertEqual(len(runtime.host.book.campaigns), 1)

    async def test_a_run_on_another_symbol_does_not_inherit_the_mother(self):
        broker_patch, adapter_patch = self._broker()
        with broker_patch, adapter_patch:
            await app_module.fib_space_paper_start(_Request())
            await self._name_mother()
            await app_module.fib_space_paper_stop(_Request())
            app_module._fib_space_engines.clear()
            result = await app_module.fib_space_paper_start(_Request(body={"symbol": "nifty"}))

        self.assertEqual(result["readopted_mothers"], 0)
        self.assertEqual(len(app_module._fib_space_engines[7].host.book.campaigns), 0)
