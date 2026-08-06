import base64
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-runtime-control.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-runtime-control-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import app as app_module  # noqa: E402


class _Request:
    def __init__(self, user_id=7, role="user"):
        self.state = SimpleNamespace(user_id=user_id, current_user={"id": user_id, "role": role})


class _Adapter:
    def __init__(self):
        self.asked = []

    def get_ticker(self, symbol):
        self.asked.append(symbol)
        return {"last_price": 24000}


class _CascadeEngine:
    def __init__(self):
        candle = app_module.IndexCandle(datetime(2026, 7, 22, 10, 0), 24000, 24000, 24000, 24000)
        self.geometry = SimpleNamespace(history=[candle])

    def kill_and_close(self, _candle):
        return {"closed": True, "cancelled_rungs": []}


class _CandleEngine:
    last_index_close = 24000

    def kill_and_close(self, _candle):
        return True


class _UnpricedCandleEngine(_CandleEngine):
    def kill_and_close(self, _candle):
        return False


class _FibEngine:
    def __init__(self):
        self.history = [app_module.IndexCandle(datetime(2026, 7, 22, 10, 0), 24000, 24000, 24000, 24000)]

    def kill_and_close(self, _candle):
        return True


class _TerminalEngine:
    def kill_and_close(self, _signal, _trade):
        return {"closed": True}


class RuntimeControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registries = [
            app_module.paper_engines,
            app_module.live_engines,
            app_module._scalp_engines,
            app_module._cascade_engines,
            app_module._candle_entry_engines,
            app_module._fib_boundary_engines,
            app_module._terminal_cascade_engines,
        ]
        self.saved = [dict(registry) for registry in self.registries]
        for registry in self.registries:
            registry.clear()

    def tearDown(self):
        for registry, saved in zip(self.registries, self.saved):
            registry.clear()
            registry.update(saved)

    async def test_pure_status_routes_do_not_restore_or_start_loops(self):
        with patch.object(app_module, "_restore_cascade_open_state", AsyncMock()) as cascade_restore:
            result = await app_module.cascade_paper_status(_Request())
        self.assertEqual(result["status"], "not_started")
        cascade_restore.assert_not_awaited()

        with patch.object(app_module, "_restore_terminal_cascade_open_state", AsyncMock()) as terminal_restore:
            result = await app_module.terminal_cascade_status(_Request())
        self.assertEqual(result["status"], "not_started")
        terminal_restore.assert_not_awaited()

        with (
            patch.object(app_module, "_restore_scalp_open_state", AsyncMock()) as scalp_restore,
            patch.object(app_module._db_mod, "get_app_state", AsyncMock(return_value=None)),
            patch.object(app_module._db_mod, "list_scalp_trades", AsyncMock(return_value=[])),
        ):
            result = await app_module.get_scalp_status(_Request())
        self.assertFalse(result["running"])
        scalp_restore.assert_not_awaited()

    async def test_control_status_includes_every_runtime_family(self):
        runtime = lambda engine: app_module._CascadeRuntime(  # noqa: E731
            engine=engine,
            adapter=_Adapter(),
            broker=SimpleNamespace(),
            last_candle_timestamp=datetime(2026, 7, 22, 10, 0),
            running=True,
        )
        app_module._cascade_engines[7] = runtime(_CascadeEngine())
        app_module._candle_entry_engines[7] = runtime(_CandleEngine())
        app_module._fib_boundary_engines[7] = {"NIFTY": runtime(_FibEngine()), "SENSEX": runtime(_FibEngine())}
        app_module._terminal_cascade_engines[7] = {
            "RELIANCE": app_module._TerminalCascadeRuntime(
                engine=_TerminalEngine(),
                broker=SimpleNamespace(),
                signal_instrument={},
                trade_instrument={},
                last_candle_timestamp=datetime(2026, 7, 22, 10, 0),
                running=True,
            )
        }
        result = await app_module.engine_control_status(_Request())
        row = result["users"][0]
        self.assertTrue(result["any_running"])
        self.assertTrue(row["cascade_running"])
        self.assertTrue(row["candle_entry_running"])
        # A COUNT now, not a flag -- two instruments, two ladders.
        self.assertEqual(row["fib_boundary_running"], 2)
        self.assertEqual(row["terminal_cascade_running"], 1)

        health = await app_module.health()
        self.assertTrue(health["runtime_running"])

    async def test_emergency_stop_covers_all_cascade_families(self):
        runtime = lambda engine: app_module._CascadeRuntime(  # noqa: E731
            engine=engine,
            adapter=_Adapter(),
            broker=SimpleNamespace(),
            last_candle_timestamp=datetime(2026, 7, 22, 10, 0),
            running=True,
        )
        app_module._cascade_engines[7] = runtime(_CascadeEngine())
        app_module._candle_entry_engines[7] = runtime(_CandleEngine())
        nifty, sensex = runtime(_FibEngine()), runtime(_FibEngine())
        app_module._fib_boundary_engines[7] = {"NIFTY": nifty, "SENSEX": sensex}
        app_module._terminal_cascade_engines[7] = {
            "RELIANCE": app_module._TerminalCascadeRuntime(
                engine=_TerminalEngine(),
                broker=SimpleNamespace(),
                signal_instrument={},
                trade_instrument={},
                last_candle_timestamp=datetime(2026, 7, 22, 10, 0),
                running=True,
            )
        }
        quote = app_module.IndexCandle(datetime(2026, 7, 22, 10, 1), 100, 100, 100, 100)
        with (
            patch.object(app_module, "_get_session_token", return_value="test"),
            patch.object(app_module, "_validate_session_async", AsyncMock(return_value={"user_id": 7})),
            patch.object(app_module, "_save_cascade_open_state", AsyncMock()),
            patch.object(app_module, "_save_candle_entry_open_state", AsyncMock()),
            patch.object(app_module, "_save_fib_boundary_open_state", AsyncMock()),
            patch.object(app_module, "_save_terminal_cascade_open_state", AsyncMock()),
            patch.object(app_module, "_terminal_cascade_quote_pair", AsyncMock(return_value=(quote, quote))),
        ):
            result = await app_module.emergency_stop(_Request(role="admin"))

        self.assertEqual(result["stopped"], 5)
        self.assertEqual(result["results"]["cascade:7"], "stopped")
        self.assertEqual(result["results"]["candle-entry:7"], "stopped")
        # Both ladders stop, each named, and each priced off ITS OWN index --
        # a SENSEX basket closed at a NIFTY print would be a fabricated exit.
        self.assertEqual(result["results"]["fib-boundary:7:NIFTY"], "stopped")
        self.assertEqual(result["results"]["fib-boundary:7:SENSEX"], "stopped")
        self.assertEqual(nifty.adapter.asked, ["NIFTY"])
        self.assertEqual(sensex.adapter.asked, ["SENSEX"])
        self.assertEqual(result["results"]["terminal-cascade:7:RELIANCE"], "stopped")

    async def test_emergency_stop_keeps_unpriced_candle_entry_monitored(self):
        runtime = app_module._CascadeRuntime(
            engine=_UnpricedCandleEngine(),
            adapter=_Adapter(),
            broker=SimpleNamespace(),
            last_candle_timestamp=datetime(2026, 7, 22, 10, 0),
            running=True,
        )
        app_module._candle_entry_engines[7] = runtime
        with (
            patch.object(app_module, "_get_session_token", return_value="test"),
            patch.object(app_module, "_validate_session_async", AsyncMock(return_value={"user_id": 7})),
            patch.object(app_module, "_save_candle_entry_open_state", AsyncMock()),
        ):
            result = await app_module.emergency_stop(_Request())
        self.assertEqual(result["stopped"], 0)
        self.assertTrue(runtime.running)
        self.assertEqual(result["results"]["candle-entry:7"], "exit_quote_unavailable_engine_left_running")

    async def test_emergency_stop_closes_paused_scalp_open_trades(self):
        eng = SimpleNamespace(
            _running=False,
            open_trades={1: SimpleNamespace(mode="paper")},
            kill_all_trades=AsyncMock(return_value={"closed": 1}),
            stop=lambda: setattr(eng, "_running", False),
        )

        async def kill_all():
            eng.open_trades.clear()
            return {"closed": 1}

        eng.kill_all_trades = AsyncMock(side_effect=kill_all)
        app_module._scalp_engines[7] = eng
        with (
            patch.object(app_module, "_get_session_token", return_value="test"),
            patch.object(app_module, "_validate_session_async", AsyncMock(return_value={"user_id": 7})),
            patch.object(app_module, "_save_scalp_open_state", AsyncMock()),
            patch.object(app_module, "_notify_scalp_ws"),
        ):
            result = await app_module.emergency_stop(_Request())
        eng.kill_all_trades.assert_awaited_once()
        self.assertEqual(result["stopped"], 1)
        self.assertNotIn(7, app_module._scalp_engines)


if __name__ == "__main__":
    unittest.main()
