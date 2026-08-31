"""The leg stop-loss must survive being detached, and say so when it does not.

Two defects found in the 2026-08-31 go-live audit of the strategy-builder
Auto path:

1. The SL orders were fired as `asyncio.create_task(_bg_sl())` with the task
   object thrown away.  asyncio keeps only a weak reference to a running task,
   so one nothing holds can be collected part-way through -- and because
   `_place_sl_order` catches its own exceptions, a stop that never reached
   Dhan produced no error, no event and no log line.  The engine went on
   believing the position was protected.

2. Nothing anywhere reported that a leg was carrying a stop percentage with
   no order resting at the broker.  `place_leg_sl` defaults to "no", so this
   is the ordinary case, not the exotic one.

These tests hold both doors shut: the task is referenced for as long as it
runs, and every way the stop can fail to land leaves the leg visibly
unprotected in `get_status()`.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.live import LiveEngine  # noqa: E402


class _Broker:
    """Answers only what _place_sl_order asks for."""

    def __init__(self, result=None, error=None, gate: asyncio.Event | None = None):
        self._result = result
        self._error = error
        self._gate = gate
        self.calls = []

    async def async_place_sl_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._gate is not None:
            await self._gate.wait()
        if self._error is not None:
            raise self._error
        return self._result


def _engine(broker, tmpdir) -> LiveEngine:
    eng = LiveEngine(dhan=broker, run_id="sl-visibility", state_dir=tmpdir)
    eng.deploy_config = {"product_type": "MIS", "place_leg_sl": "yes"}
    eng.strategy = {"run_name": "sl-visibility"}
    return eng


def _position(**over) -> dict:
    pos = {
        "leg_num": 1,
        "underlying": "NIFTY",
        "strike": 24000,
        "option_type": "PE",
        "expiry": "2026-09-03",
        "transaction_type": "BUY",
        "quantity": 260,
        "lot_size": 65,
        "lots": 4,
        "entry_premium": 250.0,
        "sl_pct": 20,
        "status": "open",
        "sl_order_id": None,
    }
    pos.update(over)
    return pos


class DetachedStopLossKeepsItsReference(unittest.TestCase):
    def test_task_is_held_while_it_runs_and_released_when_done(self):
        """A task nothing references can be collected mid-await.

        The stop is the one thing this engine detaches, so it is the one thing
        that must be held.  Checked while the broker call is still parked, not
        after it returns -- afterwards every implementation looks the same.
        """

        async def scenario():
            gate = asyncio.Event()
            broker = _Broker(result={"orderId": "SL-1"}, gate=gate)
            with __import__("tempfile").TemporaryDirectory() as tmp:
                eng = _engine(broker, tmp)
                pos = _position()

                task = eng._spawn_tracked(eng._place_sl_order(pos), "Broker stop-loss placement")
                await asyncio.sleep(0)  # let it reach the parked broker call

                self.assertIn(task, eng._background_tasks, "a detached stop-loss task must be referenced while it runs")

                gate.set()
                await task
                self.assertNotIn(task, eng._background_tasks, "a finished task must not be retained")
                self.assertEqual(pos["sl_order_id"], "SL-1")
                self.assertEqual(pos["sl_order_status"], "PLACED")

        asyncio.run(scenario())

    def test_a_raising_task_is_reported_not_swallowed(self):
        async def scenario():
            broker = _Broker(error=RuntimeError("boom"))
            with __import__("tempfile").TemporaryDirectory() as tmp:
                eng = _engine(broker, tmp)

                async def explode():
                    raise ValueError("nothing caught this")

                task = eng._spawn_tracked(explode(), "Broker stop-loss placement")
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)

                messages = " ".join(e["message"] for e in eng.event_log)
                self.assertIn("CRITICAL", messages)
                self.assertIn("Broker stop-loss placement", messages)
                self.assertIn("nothing caught this", messages)

        asyncio.run(scenario())


class AnUnplacedStopIsVisible(unittest.TestCase):
    def _run(self, broker):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            eng = _engine(broker, tmp)
            pos = _position()
            eng.positions = [pos]
            asyncio.run(eng._place_sl_order(pos))
            return eng, pos

    def test_broker_error_leaves_the_leg_unprotected_and_says_so(self):
        eng, pos = self._run(_Broker(error=RuntimeError("margin blocked")))
        self.assertEqual(pos["sl_order_status"], "FAILED")
        self.assertEqual(len(eng._unprotected_legs()), 1)
        self.assertEqual(eng.get_status()["broker_stop"]["unprotected_legs"], 1)
        messages = " ".join(e["message"] for e in eng.event_log)
        self.assertIn("UNPROTECTED", messages)

    def test_a_200_without_an_order_id_is_not_acceptance(self):
        """Dhan can answer 200 with no order id.  Nothing rests at the broker
        then, and nothing can be cancelled on the way out either."""
        eng, pos = self._run(_Broker(result={"status": "success"}))
        self.assertEqual(pos["sl_order_status"], "UNCONFIRMED")
        self.assertEqual(eng.get_status()["broker_stop"]["unprotected_legs"], 1)

    def test_a_placed_stop_is_not_flagged(self):
        eng, pos = self._run(_Broker(result={"orderId": "SL-9"}))
        self.assertEqual(pos["sl_order_status"], "PLACED")
        self.assertEqual(eng._unprotected_legs(), [])
        self.assertEqual(eng.get_status()["broker_stop"]["unprotected_legs"], 0)

    def test_a_closed_leg_is_not_counted(self):
        eng, pos = self._run(_Broker(error=RuntimeError("no")))
        pos["status"] = "closed"
        self.assertEqual(eng._unprotected_legs(), [])

    def test_status_reports_whether_a_broker_stop_was_even_requested(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            eng = _engine(_Broker(), tmp)
            eng.deploy_config["place_leg_sl"] = "no"
            eng.positions = [_position()]
            stop = eng.get_status()["broker_stop"]
            self.assertFalse(stop["requested"])
            self.assertEqual(stop["unprotected_legs"], 1, "a leg with sl_pct and no order is unprotected either way")


if __name__ == "__main__":
    unittest.main()
