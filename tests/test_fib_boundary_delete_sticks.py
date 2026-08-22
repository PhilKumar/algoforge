"""Deleting the last ladder has to stick, and a saved replay has to be removable.

Phil, 2026-08-22, on an ended 19-Aug NIFTY CE monitor he had deleted more than
once: "Why this is coming again and again even if I delete..."

`_save_fib_boundary_open_state` returned early on an empty registry, so the
stored row still held the campaign that had just been deleted, and the next
restart restored it. An empty registry is a fact worth writing down.
"""

import base64
import os
import sys
import unittest

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-fib-delete.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-fib-delete-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class EmptyRegistryPersistsTests(unittest.IsolatedAsyncioTestCase):
    USER = 404

    async def asyncSetUp(self):
        self.written = {}
        self._orig_set = app_module._db_mod.set_app_state

        async def fake_set(key, value):
            self.written[key] = value

        app_module._db_mod.set_app_state = fake_set
        app_module._fib_boundary_engines.pop(self.USER, None)

    async def asyncTearDown(self):
        app_module._db_mod.set_app_state = self._orig_set
        app_module._fib_boundary_engines.pop(self.USER, None)

    async def test_deleting_the_last_ladder_writes_an_empty_row(self):
        # The registry is empty, exactly as it is one line after the delete
        # route pops the only symbol.
        await app_module._save_fib_boundary_open_state(self.USER, force=True)
        key = app_module._fib_boundary_open_state_key(self.USER)
        self.assertIn(key, self.written, "an empty registry must still be persisted")
        self.assertEqual(app_module.json.loads(self.written[key])["campaigns"], [])

    async def test_it_is_written_even_without_force(self):
        """The delete route calls this with force=True, but a later poll must
        not be able to leave the stale row behind either."""
        await app_module._save_fib_boundary_open_state(self.USER)
        self.assertIn(app_module._fib_boundary_open_state_key(self.USER), self.written)


class ReplayDeleteRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_the_delete_route_exists_and_is_a_delete(self):
        routes = [r for r in app_module.app.routes if getattr(r, "path", "") == "/api/fib-boundary/backtests/latest"]
        methods = set()
        for r in routes:
            methods |= set(getattr(r, "methods", set()) or set())
        self.assertIn("DELETE", methods, "the saved Fib Boundary replay must be removable")
        self.assertIn("GET", methods)


if __name__ == "__main__":
    unittest.main()
