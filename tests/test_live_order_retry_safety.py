"""An order that might be live must never be sent a second time.

Four defects from the 2026-08-31 go-live audit of the strategy-builder Auto
path, all on the road between "the broker answered" and "the engine believes
it holds a position":

B3  A verification TIMEOUT cancelled the order, reported the leg failed, and
    then `_flush_pending_order` re-fired the whole entry four seconds later.
    If that first MARKET order had in fact filled and the cancel failed, the
    account held two lots sets and the engine tracked one.
B4  `verify_order_fill` resolved the filled quantity by falling back
    filledQty -> tradedQuantity -> `quantity`.  That last key is the ORDER
    quantity, so a pending order whose payload carried no fill key reported
    filled == requested and was declared FILLED at an average price of zero.
    And `get_order_status` assumed an object where Dhan answers with an array.
B5  `_restore_live_engines` built its own broadcast closure that omitted
    `_check_trade_alerts`, so every restart ended live alerting for the day.
B6  Nothing retrieved the engine task's exception.  A driver that died left
    `engine.running` True and the panel reporting a live strategy.
"""

import asyncio
import base64
import inspect
import os
import sys
import tempfile
import unittest

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-retry-safety-test.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-retry-safety-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker.dhan as dhan_mod  # noqa: E402
from broker.dhan import DhanClient  # noqa: E402
from engine.live import LiveEngine  # noqa: E402


class _StatusStub:
    """Just enough of a DhanClient for verify_order_fill to run against."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)

    def get_order_status(self, order_id):
        return self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]


def _verify(stub, **kw):
    kw.setdefault("max_wait_sec", 0.05)
    kw.setdefault("poll_interval", 0.01)
    return DhanClient.verify_order_fill(stub, "OID-1", **kw)


class TheOrderQuantityIsNotAFill(unittest.TestCase):
    def test_a_pending_order_with_no_fill_key_is_not_filled(self):
        """The exact payload the old fallback misread: pending, carrying only
        the order quantity, and therefore reported FILLED at price zero."""
        out = _verify(_StatusStub({"orderStatus": "PENDING", "quantity": 260}))
        self.assertNotEqual(out["status"], "FILLED")
        self.assertEqual(out["filled_qty"], 0)

    def test_a_zero_fill_key_is_still_not_filled(self):
        out = _verify(_StatusStub({"orderStatus": "PENDING", "quantity": 260, "filledQty": 0}))
        self.assertNotEqual(out["status"], "FILLED")

    def test_a_real_fill_is_still_read(self):
        out = _verify(_StatusStub({"orderStatus": "TRADED", "quantity": 260, "filledQty": 260, "averagePrice": 251.5}))
        self.assertEqual(out["status"], "FILLED")
        self.assertEqual(out["filled_qty"], 260)
        self.assertAlmostEqual(out["avg_price"], 251.5)

    def test_average_traded_price_is_preferred(self):
        out = _verify(
            _StatusStub({"orderStatus": "TRADED", "quantity": 65, "filledQty": 65, "averageTradedPrice": 204.25})
        )
        self.assertAlmostEqual(out["avg_price"], 204.25)

    def test_a_partial_fill_is_reported_as_partial_not_filled(self):
        out = _verify(_StatusStub({"orderStatus": "PENDING", "quantity": 260, "filledQty": 130}))
        self.assertNotEqual(out["status"], "FILLED")
        self.assertEqual(out["filled_qty"], 130)

    def test_expired_is_terminal_rather_than_polled_to_a_timeout(self):
        out = _verify(_StatusStub({"orderStatus": "EXPIRED", "quantity": 260}), max_wait_sec=5, poll_interval=0.01)
        self.assertEqual(out["status"], "EXPIRED")


class DhanAnswersOrderByIdWithAnArray(unittest.TestCase):
    class _Client:
        """`headers` is a read-only property on DhanClient, so bind the real
        method to a plain stub carrying only what it reads."""

        base_url = "https://api.example"
        headers: dict = {}
        _allow_token_refresh = False

        def refresh_access_token(self, **kw):
            return None

    def _get_status(self, order_id="OID-1"):
        return DhanClient.get_order_status(self._Client(), order_id)

    def _patched(self, body, status_code=200):
        class _Resp:
            def __init__(self):
                self.status_code = status_code

            def json(self):
                return body

        original = dhan_mod._request_with_retry
        dhan_mod._request_with_retry = lambda *a, **k: _Resp()
        try:
            return self._get_status()
        finally:
            dhan_mod._request_with_retry = original

    def test_a_single_element_array_is_unwrapped(self):
        out = self._patched([{"orderStatus": "TRADED", "filledQty": 65}])
        self.assertEqual(out["orderStatus"], "TRADED")
        self.assertEqual(out["filledQty"], 65)

    def test_an_empty_array_does_not_explode(self):
        self.assertEqual(self._patched([])["orderStatus"], "UNKNOWN")

    def test_a_plain_object_still_works(self):
        self.assertEqual(self._patched({"orderStatus": "REJECTED"})["orderStatus"], "REJECTED")

    def test_an_empty_order_id_never_reaches_the_network(self):
        called = []
        original = dhan_mod._request_with_retry
        dhan_mod._request_with_retry = lambda *a, **k: called.append(1)
        try:
            out = self._get_status("")
        finally:
            dhan_mod._request_with_retry = original
        self.assertEqual(out["orderStatus"], "UNKNOWN")
        self.assertEqual(called, [], "an empty order id must not be turned into a GET /v2/orders/")


class _Broker:
    def __init__(self, fill, cancel_raises=False):
        self._fill = fill
        self._cancel_raises = cancel_raises
        self.cancels = []

    async def async_verify_order_fill(self, order_id, max_wait_sec=15):
        return self._fill

    async def async_cancel_order(self, order_id):
        self.cancels.append(order_id)
        if self._cancel_raises:
            raise RuntimeError("order already traded")
        return {"orderStatus": "CANCELLED"}


class OnlyACertainNothingMayBeRetried(unittest.TestCase):
    def _verify(self, fill, cancel_raises=False):
        with tempfile.TemporaryDirectory() as tmp:
            eng = LiveEngine(dhan=_Broker(fill, cancel_raises), run_id="retry", state_dir=tmp)
            eng.strategy = {"run_name": "retry"}
            return asyncio.run(eng._verify_order_execution("OID-1", 260, stage="entry", label="Leg 1", timeout_sec=1))

    def test_a_clean_rejection_is_safe(self):
        out = self._verify({"status": "REJECTED", "filled_qty": 0, "avg_price": 0.0})
        self.assertTrue(out["safe_to_retry"])

    def test_a_timeout_whose_cancel_was_accepted_is_safe(self):
        """The cancel going through proves the order was still resting."""
        out = self._verify({"status": "TIMEOUT", "filled_qty": 0, "avg_price": 0.0})
        self.assertTrue(out["safe_to_retry"])
        self.assertTrue(out["cancelled_after_failure"])

    def test_a_timeout_whose_cancel_failed_is_NOT_safe(self):
        """A cancel that will not go through is the signature of a fill.
        This is the double-position case, and the one that must never retry."""
        out = self._verify({"status": "TIMEOUT", "filled_qty": 0, "avg_price": 0.0}, cancel_raises=True)
        self.assertFalse(out["safe_to_retry"])
        self.assertIn("cancel_after_failure_error", out)

    def test_a_partial_fill_is_never_safe(self):
        out = self._verify({"status": "PENDING", "filled_qty": 130, "avg_price": 250.0})
        self.assertTrue(out["partial_fill"])
        self.assertFalse(out["safe_to_retry"])

    def test_a_rejection_that_somehow_filled_is_not_safe(self):
        out = self._verify({"status": "REJECTED", "filled_qty": 65, "avg_price": 250.0})
        self.assertFalse(out["safe_to_retry"])


class ABlockedEntryIsNotFiredAgain(unittest.TestCase):
    def test_flush_refuses_the_retry_and_clears_the_pending_order(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                eng = LiveEngine(dhan=_Broker({"status": "TIMEOUT"}), run_id="blocked", state_dir=tmp)
                eng.strategy = {"run_name": "blocked"}
                eng._pending_order = {"signal_candle_time": None, "attempts": 0, "row": None}

                calls = []

                async def _fake_enter(row, callback=None):
                    calls.append(1)
                    eng._entry_retry_blocked = True  # what an unsafe leg sets

                eng._enter_trade = _fake_enter

                ok = await eng._flush_pending_order(None)
                self.assertFalse(ok)
                self.assertEqual(len(calls), 1, "the first attempt still happens")
                self.assertIsNone(eng._pending_order, "a blocked entry must not stay armed for a retry")
                messages = " ".join(e["message"] for e in eng.event_log)
                self.assertIn("Entry retry refused", messages)

        asyncio.run(scenario())

    def test_an_ordinary_failure_still_schedules_its_one_retry(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                eng = LiveEngine(dhan=_Broker({"status": "REJECTED"}), run_id="ok-retry", state_dir=tmp)
                eng.strategy = {"run_name": "ok-retry"}
                eng._pending_order = {"signal_candle_time": None, "attempts": 0, "row": None}

                async def _fake_enter(row, callback=None):
                    return None  # fails, but nothing unsafe happened

                eng._enter_trade = _fake_enter
                await eng._flush_pending_order(None)
                self.assertIsNotNone(eng._pending_order, "a clean refusal keeps its retry")
                self.assertIsNotNone(eng._pending_order.get("retry_at"))

        asyncio.run(scenario())


class ADeadDriverIsNotAQuietOne(unittest.TestCase):
    def test_the_engine_is_marked_stopped_and_says_why(self):
        import app as app_module

        class _Engine:
            def __init__(self):
                self.running = True
                self.events = []

            def log_event(self, kind, message, data=None):
                self.events.append((kind, message))

            def _save_state(self):
                pass

        async def scenario():
            engine = _Engine()
            alerts = []
            original = app_module.alerter.alert
            app_module.alerter.alert = lambda *a, **k: alerts.append(a)
            try:

                async def dies():
                    raise RuntimeError("scrip master exploded")

                task = app_module._supervise_engine_task(asyncio.create_task(dies()), engine, run_id="dead")
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)
            finally:
                app_module.alerter.alert = original
            return engine, alerts

        engine, alerts = asyncio.run(scenario())
        self.assertFalse(engine.running, "a dead driver must not leave the panel reporting a live strategy")
        joined = " ".join(m for _, m in engine.events)
        self.assertIn("driver stopped unexpectedly", joined)
        self.assertIn("scrip master exploded", joined)
        self.assertTrue(alerts, "a dead driver should raise an alert")

    def test_a_clean_finish_is_left_alone(self):
        import app as app_module

        class _Engine:
            running = True

            def log_event(self, *a, **k):
                raise AssertionError("nothing should be logged for a clean exit")

            def _save_state(self):
                raise AssertionError("nothing should be saved for a clean exit")

        async def scenario():
            async def finishes():
                return None

            engine = _Engine()
            task = app_module._supervise_engine_task(asyncio.create_task(finishes()), engine, run_id="fine")
            await task
            await asyncio.sleep(0)
            return engine

        self.assertTrue(asyncio.run(scenario()).running)


class RestoredEnginesStillAlert(unittest.TestCase):
    def test_the_shared_broadcast_raises_trade_alerts(self):
        import app as app_module

        seen = []
        originals = (app_module._check_trade_alerts, app_module._broadcast_user_ws_json)
        app_module._check_trade_alerts = lambda *a, **k: seen.append(a)

        async def _noop_broadcast(*a, **k):
            return None

        app_module._broadcast_user_ws_json = _noop_broadcast
        try:
            asyncio.run(app_module._live_engine_broadcast(1, "run", {"type": "status"}))
        finally:
            app_module._check_trade_alerts, app_module._broadcast_user_ws_json = originals
        self.assertTrue(seen, "_live_engine_broadcast must raise trade alerts")

    def test_restore_delegates_instead_of_rebuilding_the_closure(self):
        import app as app_module

        src = inspect.getsource(app_module._restore_live_engines)
        self.assertIn("_live_engine_broadcast", src)
        self.assertNotIn(
            "_broadcast_user_ws_json",
            src,
            "restore must not hand-roll a broadcast again -- that is how _check_trade_alerts got dropped",
        )
        self.assertIn("_supervise_engine_task", src)


if __name__ == "__main__":
    unittest.main()


class AZeroIsNotAPrice(unittest.TestCase):
    """Dhan returns averageTradedPrice as 0.0 on a genuinely traded order in at
    least one book -- proven on a real order, 2026-09-01. `dict.get(k, other)`
    hands back that zero instead of falling through to the field holding the
    true price, so the preference order has to skip zeros, not absent keys."""

    def test_a_zero_traded_price_falls_through_to_the_real_one(self):
        out = _verify(
            _StatusStub(
                {
                    "orderStatus": "TRADED",
                    "quantity": 65,
                    "filledQty": 65,
                    "averageTradedPrice": 0.0,
                    "averagePrice": 251.5,
                }
            )
        )
        self.assertEqual(out["status"], "FILLED")
        self.assertAlmostEqual(out["avg_price"], 251.5, msg="a 0.0 must not win over a real price")

    def test_a_real_traded_price_is_still_preferred(self):
        out = _verify(
            _StatusStub(
                {
                    "orderStatus": "TRADED",
                    "quantity": 65,
                    "filledQty": 65,
                    "averageTradedPrice": 204.25,
                    "averagePrice": 199.0,
                }
            )
        )
        self.assertAlmostEqual(out["avg_price"], 204.25)

    def test_all_zero_stays_zero(self):
        out = _verify(_StatusStub({"orderStatus": "TRADED", "quantity": 65, "filledQty": 65, "averagePrice": 0}))
        self.assertEqual(out["avg_price"], 0.0)
