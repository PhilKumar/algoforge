"""A replay in flight must be reachable, not just refused.

The backtest runs as a server-side job that outlives the HTTP request, so a
502/504 from nginx says nothing about whether it is still going — it says one
poll did not get through, which a long replay on a small box makes likely. The
page used to treat that as fatal and told the user to "open Results again
shortly"; pressing Run again then hit a bare 409 ("a backtest is already
running") with no way to reach the run that was going. Between the two, a
finished replay could be unreachable from the UI entirely.

The 409 now carries the live job's id and an attach flag so the page can join
it. These tests pin that contract, since the browser half depends on it.
"""

import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _request(user_id: int):
    """The only thing these endpoints read off a request is the middleware's
    resolved user id."""
    return types.SimpleNamespace(state=types.SimpleNamespace(user_id=user_id))


def _payload(name: str = "S1"):
    """Stands in for StrategyPayload. The endpoint stores model_dump() so an
    attaching page can render the run against the strategy that started it."""
    return types.SimpleNamespace(model_dump=lambda: {"run_name": name})


class BacktestJobAttachTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._jobs = dict(app_module._backtest_jobs)
        self._active = dict(app_module._backtest_active_by_user)
        app_module._backtest_jobs.clear()
        app_module._backtest_active_by_user.clear()

    def tearDown(self):
        app_module._backtest_jobs.clear()
        app_module._backtest_jobs.update(self._jobs)
        app_module._backtest_active_by_user.clear()
        app_module._backtest_active_by_user.update(self._active)

    async def test_a_second_start_attaches_to_the_running_job(self):
        app_module._backtest_jobs["job-1"] = {"user_id": 7, "status": "running"}
        app_module._backtest_active_by_user[7] = "job-1"

        response = await app_module.start_backtest_job(_payload(), _request(7))

        self.assertEqual(response.status_code, 409)
        body = response.body.decode()
        self.assertIn('"job_id":"job-1"', body.replace(" ", ""))
        self.assertIn('"attach":true', body.replace(" ", ""))

    async def test_the_refusal_reports_the_running_state_not_an_error(self):
        """The page shows this to the user. "error" reads as "your backtest
        broke" when the truth is "it is still going"."""
        app_module._backtest_jobs["job-1"] = {"user_id": 7, "status": "queued"}
        app_module._backtest_active_by_user[7] = "job-1"

        response = await app_module.start_backtest_job(_payload(), _request(7))
        self.assertIn('"status":"queued"', response.body.decode().replace(" ", ""))

    async def test_another_user_is_not_blocked_by_someone_elses_job(self):
        app_module._backtest_jobs["job-1"] = {"user_id": 7, "status": "running"}
        app_module._backtest_active_by_user[7] = "job-1"

        started = []

        async def fake_run(job_id, payload, request):
            started.append(job_id)

        original = app_module._run_backtest_job
        app_module._run_backtest_job = fake_run
        try:
            response = await app_module.start_backtest_job(_payload(), _request(9))
            await asyncio.sleep(0)
        finally:
            app_module._run_backtest_job = original

        self.assertEqual(response["status"], "queued")
        self.assertTrue(response["job_id"])
        self.assertEqual(started, [response["job_id"]])

    async def test_a_finished_job_frees_the_slot(self):
        """Otherwise one completed run locks the account out of ever starting
        another, which is worse than the bug it guards against."""
        app_module._backtest_jobs["job-1"] = {"user_id": 7, "status": "complete"}
        app_module._backtest_active_by_user[7] = "job-1"

        async def fake_run(job_id, payload, request):
            return None

        original = app_module._run_backtest_job
        app_module._run_backtest_job = fake_run
        try:
            response = await app_module.start_backtest_job(_payload(), _request(7))
            await asyncio.sleep(0)
        finally:
            app_module._run_backtest_job = original

        self.assertEqual(response["status"], "queued")
        self.assertNotEqual(response["job_id"], "job-1")

    async def test_progress_is_readable_only_by_its_owner(self):
        app_module._backtest_jobs["job-1"] = {"user_id": 7, "status": "running"}
        with self.assertRaises(Exception) as caught:
            await app_module.get_backtest_job("job-1", _request(8))
        self.assertEqual(getattr(caught.exception, "status_code", None), 404)

    async def test_a_completed_job_hands_back_its_result(self):
        app_module._backtest_jobs["job-1"] = {
            "user_id": 7,
            "status": "complete",
            "result": {"status": "success", "stats": {"total_trades": 3}},
        }
        payload = await app_module.get_backtest_job("job-1", _request(7))
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["result"]["stats"]["total_trades"], 3)
        self.assertNotIn("user_id", payload, "the owner id must not travel to the browser")


class PollResilienceSourceTests(unittest.TestCase):
    """The retry loop lives inside runBacktest and cannot be called headlessly,
    so pin the properties that made the run survivable."""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "philforge-app.js")
        src = open(path, encoding="utf-8").read()
        start = src.index("async function runBacktest(")
        self.body = src[start : src.index("\n  } catch", start)]

    def test_a_gateway_hiccup_does_not_kill_the_run(self):
        self.assertIn("TRANSIENT_LIMIT", self.body)
        self.assertIn("consecutiveFailures", self.body)
        self.assertIn("continue", self.body)

    def test_a_missing_job_is_believed_immediately(self):
        """404 is the one status worth trusting at once — waiting cannot bring
        a job back that the server no longer has."""
        self.assertIn("404", self.body)

    def test_a_409_attaches_instead_of_throwing(self):
        self.assertIn("409", self.body)
        self.assertIn("Rejoining", self.body)


if __name__ == "__main__":
    unittest.main()
