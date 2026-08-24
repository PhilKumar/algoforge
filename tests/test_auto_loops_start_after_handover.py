"""The auto mothers have to survive a deploy.

Phil, 2026-08-24, on a session where the Fib Boundary ladder should have
bought three times and bought nothing: "Why no entry was taken?"

A deploying worker starts while the OLD port is still active, so
`_engine_restore_owner_is_active_instance()` is False and the entire startup
block is skipped -- and that block was the only place the three auto loops
were created. cd-deploy then flips the port file and calls
POST /api/restore-engines, which brought back the live, paper and auxiliary
ENGINES and started none of the loops. So after every deploy the Fib Boundary
mother, the Candle Entry mother and Gap Carry were dead until some later
restart happened to begin active.

Evidence it was really dead on 24 Aug: zero [FIB AUTO] lines in the day's
log, `fib_boundary_auto:1` still holding only the settings written on 20 Aug
(no `last_day`, no `state`), and `fib_boundary_open:1` an empty campaign list
last written at 01:06. Replaying the day's real bars through the real ladder
with the real expiry chain produces three buys: 11:15, 11:35 and 13:35.
"""

import asyncio
import base64
import os
import sys
import unittest

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-auto-loops.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-auto-loops-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class AutoLoopsStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._real = {}
        for name in ("_run_fib_boundary_auto_loop", "_run_candle_entry_auto_loop", "_run_gap_carry_auto_loop"):
            self._real[name] = getattr(app_module, name)

            async def never_ending(_n=name):
                await asyncio.Event().wait()

            setattr(app_module, name, never_ending)
        for task in app_module._auto_loop_tasks.values():
            task.cancel()
        app_module._auto_loop_tasks.clear()

    async def asyncTearDown(self):
        for task in app_module._auto_loop_tasks.values():
            task.cancel()
        app_module._auto_loop_tasks.clear()
        for name, fn in self._real.items():
            setattr(app_module, name, fn)

    async def test_all_three_mothers_are_started(self):
        started = app_module._ensure_auto_loops_running()
        self.assertEqual(sorted(started), ["candle-entry", "fib-boundary", "gap-carry"])
        await asyncio.sleep(0)
        self.assertTrue(all(not t.done() for t in app_module._auto_loop_tasks.values()))

    async def test_calling_it_again_does_not_double_them(self):
        app_module._ensure_auto_loops_running()
        await asyncio.sleep(0)
        again = app_module._ensure_auto_loops_running()
        self.assertEqual(again, [], "a second call must not start a second ladder on the same symbol")
        self.assertEqual(len(app_module._auto_loop_tasks), 3)

    async def test_a_dead_loop_is_restarted(self):
        app_module._ensure_auto_loops_running()
        await asyncio.sleep(0)
        app_module._auto_loop_tasks["fib-boundary"].cancel()
        await asyncio.sleep(0)
        self.assertEqual(app_module._ensure_auto_loops_running(), ["fib-boundary"])

    def test_the_handover_route_starts_them(self):
        """The only path a DEPLOYED worker takes to active."""
        import inspect

        source = inspect.getsource(app_module.restore_engines_after_handover)
        self.assertIn("_ensure_auto_loops_running", source)
        self.assertIn("_wake_journal_chart_loop", source)


if __name__ == "__main__":
    unittest.main()
