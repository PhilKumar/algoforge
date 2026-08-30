"""Live stays honest while only Fib Boundary owns a real order path.

The 2026-08-30 readiness audit found that flipping the one execution flag
would have armed Fib Boundary with real orders AND let Candle Entry, Gap
Carry and Supertrend answer 200 to mode="live" while silently trading paper.
It also found the live executor sending every order at Dhan's INTRADAY
default (a carry campaign would be squared off at ~15:20 behind the engine's
back), a broker error on entry being retried blind on the next tick, and a
resting target left working while the market exit for the same legs went out.
These tests hold each of those doors shut.
"""

import base64
import json
import os
import sys
import unittest
from datetime import date, datetime
from types import SimpleNamespace

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-live-gate-test.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-live-gate-test-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402

import app as app_module  # noqa: E402
import engine.fib_touch_ladder as ladder_mod  # noqa: E402
from engine.fib_touch_ladder import (  # noqa: E402
    FibTouchConfig,
    FibTouchLadder,
    LiveExecutor,
    TouchFill,
    TouchRung,
)


class TradeModeNeverRidesOnAnotherStrategysFlag(unittest.TestCase):
    """These three refuse "live" however open the FIB ladder's flag is.

    Their refusal once rode on FIB_TOUCH_LIVE_EXECUTION_ENABLED, which belongs
    to a different strategy: the day it flipped for the fib ladder, these
    would have accepted "live" and traded paper behind a 200. Since
    2026-08-30 they have live paths of their own and a gate of their own, and
    the invariant worth pinning is that the two gates stay SEPARATE.
    """

    def setUp(self):
        self._fib = app_module._FIB_TOUCH_LIVE_EXECUTION_ENABLED
        self._shared = app_module._OPTIONS_LIVE_EXECUTION_ENABLED
        app_module._FIB_TOUCH_LIVE_EXECUTION_ENABLED = True
        app_module._OPTIONS_LIVE_EXECUTION_ENABLED = False

    def tearDown(self):
        app_module._FIB_TOUCH_LIVE_EXECUTION_ENABLED = self._fib
        app_module._OPTIONS_LIVE_EXECUTION_ENABLED = self._shared

    def _helpers(self):
        return (
            app_module._candle_entry_trade_mode,
            app_module._gap_carry_trade_mode,
            app_module._supertrend_trade_mode,
        )

    def test_the_fib_flag_alone_opens_none_of_them(self):
        for helper in self._helpers():
            with self.subTest(helper=helper.__name__):
                self.assertEqual(helper("paper"), "paper")
                with self.assertRaises(HTTPException) as caught:
                    helper("live")
                self.assertEqual(caught.exception.status_code, 503)
                self.assertIn("built but disabled", caught.exception.detail)
                with self.assertRaises(HTTPException) as caught:
                    helper("margin")
                self.assertEqual(caught.exception.status_code, 400)

    def test_their_own_gate_is_what_opens_them(self):
        app_module._OPTIONS_LIVE_EXECUTION_ENABLED = True
        for helper in self._helpers():
            with self.subTest(helper=helper.__name__):
                self.assertEqual(helper("live"), "live")


class _RecordingBroker:
    """Counts and captures what would have reached Dhan."""

    def __init__(self, fail_orders: bool = False):
        self.calls = []
        self.fail_orders = fail_orders

    def place_option_order(self, **kwargs):
        self.calls.append(("place", kwargs))
        if self.fail_orders:
            raise RuntimeError("Dhan timed out; order fate unknown")
        return {"orderId": f"D{len(self.calls)}"}

    def verify_order_fill(self, order_id, max_wait_sec=20, poll_interval=1.5):
        return {
            "order_id": order_id,
            "status": "FILLED",
            "filled_qty": 65,
            "avg_price": 226.4,
        }

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return {"orderId": order_id}


class LiveExecutorCarriesOneProductType(unittest.TestCase):
    """Every leg goes to Dhan under the campaign's own product, never the
    INTRADAY default -- a carry campaign booked MIS is squared off at ~15:20
    behind the engine's back, and a SELL under a different product than its
    BUY opens a short instead of netting."""

    def setUp(self):
        self._flag = ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = True

    def tearDown(self):
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = self._flag

    def test_margin_default_reaches_every_order(self):
        broker = _RecordingBroker()
        executor = LiveExecutor(broker, "NIFTY", armed=True)
        executor.buy(
            when=datetime(2026, 8, 28, 11, 30),
            strike=24_000.0,
            expiry=date(2026, 9, 1),
            option_type="CE",
            quantity=65,
            lots=1,
            premium=225.0,
        )
        executor.sell_all(
            when=datetime(2026, 8, 28, 14, 0),
            legs=[{"strike": 24_000.0, "expiry": "2026-09-01", "option_type": "CE", "quantity": 65}],
        )
        executor.rest_sell(
            when=datetime(2026, 8, 28, 11, 31),
            strike=24_000.0,
            expiry="2026-09-01",
            option_type="CE",
            quantity=65,
            price=250.0,
        )
        products = [kwargs["product_type"] for verb, kwargs in broker.calls if verb == "place"]
        self.assertEqual(products, ["MARGIN", "MARGIN", "MARGIN"])

    def test_intraday_campaigns_say_so(self):
        broker = _RecordingBroker()
        executor = LiveExecutor(broker, "NIFTY", armed=True, product_type="INTRADAY")
        executor.buy(
            when=datetime(2026, 8, 28, 11, 30),
            strike=24_000.0,
            expiry=date(2026, 9, 1),
            option_type="CE",
            quantity=65,
            lots=1,
            premium=225.0,
        )
        self.assertEqual(broker.calls[0][1]["product_type"], "INTRADAY")


def _armed_ladder(executor) -> FibTouchLadder:
    ladder = FibTouchLadder(
        FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=datetime(2026, 8, 28, 10, 15),
            lot_size=65,
            strike_step=50.0,
        ),
        premium_lookup=lambda *a: 200.0,
        expiry_source=lambda on: [date(2026, 9, 1)],
        executor=executor,
    )
    return ladder


def _bar(when: datetime, price: float):
    return SimpleNamespace(timestamp=when, open=price, high=price, low=price, close=price)


class EntryHaltsOnUnknownBrokerOutcome(unittest.TestCase):
    """A broker error on the buy is NOT a refusal: the order may exist. The
    ladder disarms itself instead of resending the same rung next tick."""

    def setUp(self):
        self._flag = ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = True

    def tearDown(self):
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = self._flag

    def test_failed_send_disarms_and_never_retries(self):
        broker = _RecordingBroker(fail_orders=True)
        executor = LiveExecutor(broker, "NIFTY", armed=True)
        ladder = _armed_ladder(executor)
        ladder.rungs = [TouchRung(level=2, index_price=24_050.0, status="COLLECTED", fib_id=1)]
        ladder._buy_stop = 24_080.0
        ladder._stop_bar = None

        ladder._try_fill(_bar(datetime(2026, 8, 28, 11, 30), 24_100.0))
        self.assertEqual(ladder.status, "EXECUTION_ERROR")
        self.assertFalse(executor.armed)
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(ladder.fills, [])

        # The next trigger finds the executor disarmed: refused, not resent.
        ladder._try_fill(_bar(datetime(2026, 8, 28, 11, 31), 24_100.0))
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(ladder.status, "EXECUTION_REFUSED")


class ExitPullsRestingTargetsFirst(unittest.TestCase):
    """While a resting LIMIT and a market exit are both working on the same
    legs, a double fill is a naked short. The cancel goes out first."""

    def setUp(self):
        self._flag = ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = True

    def tearDown(self):
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = self._flag

    def test_kill_cancels_the_rest_before_selling(self):
        broker = _RecordingBroker()
        executor = LiveExecutor(broker, "NIFTY", armed=True)
        ladder = _armed_ladder(executor)
        ladder.fills = [
            TouchFill(
                buy_number=1,
                level=2,
                timestamp=datetime(2026, 8, 28, 11, 30),
                index_price=24_080.0,
                premium=225.0,
                lots=1,
                quantity=65,
                strike=24_000.0,
                expiry=date(2026, 9, 1),
                option_type="CE",
                order_id="D1",
                fib_id=1,
                covered=[],
            )
        ]
        ladder.resting_exits = [{"order_id": "R1", "strike": 24_000.0, "quantity": 65}]

        self.assertTrue(ladder.kill_and_close(_bar(datetime(2026, 8, 28, 14, 0), 24_150.0)))
        verbs = [verb for verb, _payload in broker.calls]
        self.assertEqual(verbs[0], "cancel", f"the resting target must be pulled first, got {broker.calls}")
        self.assertIn("place", verbs[1:])
        self.assertEqual(ladder.status, "KILLED")
        self.assertEqual(ladder.resting_exits, [])


class SupertrendEmptySaveKeepsTheLastCampaign(unittest.TestCase):
    """Recording "nothing is running" must not erase the final snapshot the
    kill route wrote one line earlier."""

    def setUp(self):
        self._rows = {}
        self._orig_set = app_module._db_mod.set_app_state
        self._orig_get = app_module._db_mod.get_app_state

        async def fake_set(key, value):
            self._rows[key] = value

        async def fake_get(key):
            return self._rows.get(key)

        app_module._db_mod.set_app_state = fake_set
        app_module._db_mod.get_app_state = fake_get

    def tearDown(self):
        app_module._db_mod.set_app_state = self._orig_set
        app_module._db_mod.get_app_state = self._orig_get

    def test_snapshot_survives_the_empty_registry_save(self):
        uid = 987_654
        key = app_module._supertrend_open_state_key(uid)
        self._rows[key] = json.dumps({"engine": {"status": "KILLED", "net": -1234.5}, "running": True})
        self.assertNotIn(uid, app_module._supertrend_engines)

        asyncio.run(app_module._save_supertrend_open_state(uid, force=True))

        saved = json.loads(self._rows[key])
        self.assertEqual(saved["engine"], {"status": "KILLED", "net": -1234.5})
        self.assertFalse(saved["running"])


class RecoveryRunRemembersSideAndDepth(unittest.TestCase):
    """Side and ITM depth are per-run choices; before they were persisted a
    deploy brought every PE run back as a CE run at the default depth."""

    def setUp(self):
        self._rows = {}
        self._orig_set = app_module._db_mod.set_app_state
        self._orig_get = app_module._db_mod.get_app_state

        async def fake_set(key, value):
            self._rows[key] = value

        async def fake_get(key):
            return self._rows.get(key)

        app_module._db_mod.set_app_state = fake_set
        app_module._db_mod.get_app_state = fake_get

    def tearDown(self):
        app_module._db_mod.set_app_state = self._orig_set
        app_module._db_mod.get_app_state = self._orig_get

    def test_save_writes_side_and_depth(self):
        host = SimpleNamespace(
            side="PE",
            mode="ladder",
            config=SimpleNamespace(
                timeframe="5m",
                itm_steps=4,
                lots_schedule=(1, 2),
                min_profit_inr=500.0,
                sl_source="mother",
                horizon_sessions=3,
            ),
            campaigns={},
        )
        runtime = SimpleNamespace(
            symbol="nifty",
            running=True,
            started_at=datetime(2026, 8, 28, 10, 15),
            host=host,
        )
        asyncio.run(app_module._save_recovery_state(4321, runtime))
        saved = json.loads(self._rows[app_module._recovery_state_key(4321)])
        self.assertEqual(saved["side"], "PE")
        self.assertEqual(saved["itm_steps"], 4)

    def test_restore_hands_both_back_to_the_host(self):
        uid = 4322
        self._rows[app_module._recovery_state_key(uid)] = json.dumps(
            {
                "symbol": "nifty",
                "running": True,
                "started_at": "2026-08-28T10:15:00",
                "timeframe": "5m",
                "mode": "ladder",
                "side": "PE",
                "itm_steps": 4,
                "config": {},
                "mothers": [],
            }
        )
        captured = {}
        orig_build = app_module._build_recovery_host

        def fake_build(symbol, adapter, broker, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                config=SimpleNamespace(timeframe=kwargs.get("timeframe", "5m")),
                mode=kwargs.get("mode", "ladder"),
                side=kwargs.get("side", "CE"),
                campaigns={},
            )

        app_module._build_recovery_host = fake_build
        try:

            async def run():
                runtime = await app_module._restore_recovery_run(uid, SimpleNamespace())
                self.assertIsNotNone(runtime)
                if runtime.task is not None:
                    runtime.task.cancel()

            asyncio.run(run())
        finally:
            app_module._build_recovery_host = orig_build
            app_module._recovery_engines.pop(uid, None)
        self.assertEqual(captured.get("side"), "PE")
        self.assertEqual(captured.get("itm_steps"), 4)


if __name__ == "__main__":
    unittest.main()


class BuyBooksWhatDhanTraded(unittest.TestCase):
    """The acknowledgement is not the fill: the ladder's money follows the
    broker's average traded price, and a REJECTED order -- a known nothing --
    may retry, while an unknown outcome disarms."""

    def setUp(self):
        self._flag = ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = True

    def tearDown(self):
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = self._flag

    def _ladder_with(self, broker):
        executor = LiveExecutor(broker, "NIFTY", armed=True)
        ladder = _armed_ladder(executor)
        ladder.rungs = [TouchRung(level=2, index_price=24_050.0, status="COLLECTED", fib_id=1)]
        ladder._buy_stop = 24_080.0
        ladder._stop_bar = None
        return ladder, executor

    def test_the_fill_carries_the_traded_price_not_the_estimate(self):
        ladder, _executor = self._ladder_with(_RecordingBroker())
        ladder._try_fill(_bar(datetime(2026, 8, 28, 11, 30), 24_100.0))
        self.assertEqual(len(ladder.fills), 1)
        # premium_lookup said 200; Dhan traded 226.4 -- the book holds 226.4.
        self.assertEqual(ladder.fills[0].premium, 226.4)
        self.assertEqual(ladder.fills[0].quantity, 65)

    def test_a_rejected_buy_stays_armed_and_may_retry(self):
        class _RejectingBroker(_RecordingBroker):
            def verify_order_fill(self, order_id, max_wait_sec=20, poll_interval=1.5):
                return {
                    "order_id": order_id,
                    "status": "REJECTED",
                    "filled_qty": 0,
                    "avg_price": 0.0,
                    "message": "margin shortfall",
                }

        ladder, executor = self._ladder_with(_RejectingBroker())
        ladder._try_fill(_bar(datetime(2026, 8, 28, 11, 30), 24_100.0))
        self.assertEqual(ladder.fills, [])
        self.assertEqual(ladder.status, "EXECUTION_REFUSED")
        self.assertTrue(executor.armed, "a rejection is a known nothing; it must not disarm")

    def test_a_verification_timeout_disarms(self):
        class _TimeoutBroker(_RecordingBroker):
            def verify_order_fill(self, order_id, max_wait_sec=20, poll_interval=1.5):
                return {
                    "order_id": order_id,
                    "status": "TIMEOUT",
                    "filled_qty": 0,
                    "avg_price": 0.0,
                    "message": "still pending",
                }

        ladder, executor = self._ladder_with(_TimeoutBroker())
        ladder._try_fill(_bar(datetime(2026, 8, 28, 11, 30), 24_100.0))
        self.assertEqual(ladder.fills, [])
        self.assertEqual(ladder.status, "EXECUTION_ERROR")
        self.assertFalse(executor.armed, "an unknown outcome must stop the resend loop")


def _held_ladder(broker):
    """An armed live ladder already holding one leg, ready to exit."""
    executor = LiveExecutor(broker, "NIFTY", armed=True)
    ladder = _armed_ladder(executor)
    ladder.fills = [
        TouchFill(
            buy_number=1,
            level=2,
            timestamp=datetime(2026, 8, 28, 11, 30),
            index_price=24_080.0,
            premium=225.0,
            lots=1,
            quantity=65,
            strike=24_000.0,
            expiry=date(2026, 9, 1),
            option_type="CE",
            order_id="D1",
            fib_id=1,
            covered=[],
        )
    ]
    return ladder


class ExitBooksAndFreezesHonestly(unittest.TestCase):
    """A confirmed exit books Dhan's price; an unconfirmed one freezes."""

    def setUp(self):
        self._flag = ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = True

    def tearDown(self):
        ladder_mod.FIB_TOUCH_LIVE_EXECUTION_ENABLED = self._flag

    def test_a_confirmed_kill_books_the_broker_price(self):
        ladder = _held_ladder(_RecordingBroker())
        self.assertTrue(ladder.kill_and_close(_bar(datetime(2026, 8, 28, 14, 0), 24_150.0)))
        # premium_lookup said 200; Dhan sold at 226.4 -- the round holds 226.4.
        self.assertEqual(ladder.rounds[0]["fills"][0]["exit_premium"], 226.4)
        self.assertEqual(len(ladder.fills), 1, "the legs stay visible after the kill")

    def test_an_unknown_exit_leg_freezes_until_the_next_deliberate_kill(self):
        class _UnknownExitBroker(_RecordingBroker):
            def verify_order_fill(self, order_id, max_wait_sec=20, poll_interval=1.5):
                kwargs = [k for verb, k in self.calls if verb == "place"]
                if kwargs and kwargs[-1]["transaction_type"] == "SELL":
                    return {
                        "order_id": order_id,
                        "status": "TIMEOUT",
                        "filled_qty": 0,
                        "avg_price": 0.0,
                        "message": "exchange slow",
                    }
                return super().verify_order_fill(order_id, max_wait_sec, poll_interval)

        ladder = _held_ladder(_UnknownExitBroker())
        self.assertFalse(ladder.kill_and_close(_bar(datetime(2026, 8, 28, 14, 0), 24_150.0)))
        self.assertEqual(ladder.status, "EXIT_ERROR")
        self.assertTrue(ladder._exit_unknown)
        # Frozen: the automatic exits refuse while a leg's fate is unknown.
        self.assertFalse(ladder._try_exit(_bar(datetime(2026, 8, 28, 14, 1), 24_400.0)))
        # The next kill is the deliberate human retry: it lifts the freeze.
        broker2 = _RecordingBroker()
        ladder.executor = LiveExecutor(broker2, "NIFTY", armed=True)
        self.assertTrue(ladder.kill_and_close(_bar(datetime(2026, 8, 28, 14, 5), 24_150.0)))
        self.assertEqual(ladder._exit_unknown, [])

    def test_a_traded_resting_target_settles_its_leg_before_the_market_sell(self):
        class _RestFilledBroker(_RecordingBroker):
            def cancel_order(self, order_id):
                self.calls.append(("cancel", order_id))
                raise RuntimeError("order is not in a cancellable state")

            def get_order_status(self, order_id):
                return {"orderStatus": "TRADED", "averagePrice": 251.5}

        broker = _RestFilledBroker()
        ladder = _held_ladder(broker)
        ladder.resting_exits = [
            {
                "order_id": "R1",
                "rung_key": ladder.fills[0].rung_key,
                "strike": 24_000.0,
                "expiry": "2026-09-01",
                "option_type": "CE",
                "quantity": 65,
                "price": 250.0,
            }
        ]
        self.assertTrue(ladder.kill_and_close(_bar(datetime(2026, 8, 28, 14, 0), 24_150.0)))
        # The leg sold at the rest's traded price, and NO market sell went out.
        sells = [k for verb, k in broker.calls if verb == "place" and k["transaction_type"] == "SELL"]
        self.assertEqual(sells, [], "the rest already sold the leg; a market sell would be a short")
        self.assertEqual(ladder.rounds[0]["fills"][0]["exit_premium"], 251.5)


class RestoreAsksTheBrokerFirst(unittest.TestCase):
    """After a restart, the one authority on what happened is Dhan's book."""

    def test_a_rest_that_traded_while_down_books_and_a_short_book_freezes(self):
        class _Broker:
            def get_order_status(self, order_id):
                return {"orderStatus": "TRADED", "averagePrice": 260.0}

            def get_positions(self):
                return [{"securityId": "SEC1", "netQty": 0}]

        ladder = _armed_ladder(SimpleNamespace())
        ladder.fills = _held_ladder(_Broker()).fills
        ladder.fills.append(
            TouchFill(
                buy_number=2,
                level=3,
                timestamp=datetime(2026, 8, 28, 12, 0),
                index_price=24_050.0,
                premium=180.0,
                lots=1,
                quantity=65,
                strike=23_950.0,
                expiry=date(2026, 9, 1),
                option_type="CE",
                order_id="D2",
                fib_id=1,
                covered=[],
            )
        )
        ladder.resting_exits = [
            {
                "order_id": "R9",
                "rung_key": ladder.fills[0].rung_key,
                "strike": 24_000.0,
                "expiry": "2026-09-01",
                "option_type": "CE",
                "quantity": 65,
                "price": 250.0,
            }
        ]
        from unittest.mock import patch

        with patch("broker.dhan.ScripMaster.lookup", return_value="SEC1"):
            notes = app_module._reconcile_fib_ladder(ladder, _Broker())
        # The traded rest booked its leg at 260...
        self.assertEqual(len(ladder._settled), 1)
        self.assertEqual(ladder._settled[0][1], 260.0)
        # ...and the broker holding 0 of the remaining 65 froze the exits.
        self.assertTrue(ladder._exit_unknown)
        self.assertTrue(any("FROZEN" in note for note in notes))
