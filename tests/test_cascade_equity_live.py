"""Safety tests for live cash-market Cascade execution via resting orders.

Every test here describes a way to lose money by accident. They run against a
fake broker, so they need no credentials and never touch a real account.
"""

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from broker.dhan import AmbiguousOrderSubmission
from engine.cascade_equity_live import (
    CANCELLED,
    FILLED,
    PARTIAL,
    RESTING,
    DhanRestingExecutor,
    Instrument,
    LiveExecutionHalt,
    LiveExecutionRejected,
    LiveGuardrails,
    RestingOrder,
)

IST = ZoneInfo("Asia/Kolkata")
MID_SESSION = datetime(2026, 7, 28, 11, 0, tzinfo=IST)  # a Tuesday
SCRIP = Instrument(symbol="RELIANCE", security_id="2885", exchange_segment="NSE_EQ", product_type="CNC")

# Phil's armed caps: Rs 10,00,000 per campaign, a hundredth of that per order.
CAMPAIGN_CAP = 1_000_000.0
ORDER_CAP = CAMPAIGN_CAP / 100


class FakeBroker:
    def __init__(self, *, funds=5_000_000.0, place_raises=None, cancel_raises=None):
        self.orders = []
        self.cancelled = []
        self.funds = funds
        self.place_raises = place_raises
        self.cancel_raises = cancel_raises
        self.status_by_id = {}
        self.fund_calls = 0

    def get_funds(self):
        self.fund_calls += 1
        return {"availabelBalance": self.funds}

    def place_order(self, **kwargs):
        if self.place_raises:
            raise self.place_raises
        self.orders.append(kwargs)
        order_id = f"DHAN{len(self.orders)}"
        self.status_by_id.setdefault(order_id, {"orderStatus": "PENDING", "filledQty": 0, "averagePrice": 0})
        return {"orderId": order_id}

    def cancel_order(self, order_id):
        if self.cancel_raises:
            raise self.cancel_raises
        self.cancelled.append(order_id)
        self.status_by_id[order_id] = {"orderStatus": "CANCELLED", "filledQty": 0, "averagePrice": 0}
        return {"orderStatus": "CANCELLED"}

    def get_order_status(self, order_id):
        return self.status_by_id.get(order_id, {"orderStatus": "UNKNOWN"})

    def mark(self, order_id, status, filled=0, price=0.0):
        self.status_by_id[order_id] = {
            "orderStatus": status,
            "filledQty": filled,
            "averagePrice": price,
        }


def executor(broker, **overrides):
    guards = dict(max_order_inr=ORDER_CAP, max_campaign_inr=CAMPAIGN_CAP)
    guards.update(overrides)
    return DhanRestingExecutor(broker, LiveGuardrails(**guards), clock=lambda: MID_SESSION)


class StopOrderPlacementTests(unittest.TestCase):
    def test_stop_buy_rests_as_an_exchange_side_sl_order(self):
        broker = FakeBroker()
        live = executor(broker)
        order = live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)

        submitted = broker.orders[0]
        self.assertEqual(submitted["order_type"], "SL")
        self.assertEqual(submitted["validity"], "DAY")
        self.assertEqual(submitted["transaction_type"], "BUY")
        self.assertEqual(submitted["trigger_price"], 1000.0)
        self.assertEqual(order.status, RESTING)

    def test_the_stop_limit_sits_above_its_trigger_so_it_can_actually_fill(self):
        """A limit equal to the trigger can trigger and never fill in a fast move."""
        live = executor(FakeBroker(), stop_limit_offset_pct=0.002)
        order = live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(order.trigger_price, 1000.0)
        self.assertEqual(order.limit_price, 1002.0)

    def test_caps_are_measured_on_the_limit_price_not_the_trigger(self):
        broker = FakeBroker()
        live = executor(broker, max_order_inr=10_000)
        # 10 x ~1001 is over the cap even though 10 x 1000 would just fit.
        with self.assertRaises(LiveExecutionRejected):
            live.place_stop_buy("buy:t1", SCRIP, quantity=10, trigger_price=1000.0)
        self.assertEqual(broker.orders, [])

    def test_campaign_cap_counts_what_is_already_deployed(self):
        broker = FakeBroker()
        live = executor(broker, max_order_inr=ORDER_CAP, max_campaign_inr=15_000)
        live.place_stop_buy("buy:a", SCRIP, quantity=9, trigger_price=1000.0)
        broker.mark("DHAN1", "TRADED", filled=10, price=1001.0)
        live.sync("buy:a")
        with self.assertRaises(LiveExecutionRejected):
            live.place_stop_buy("buy:b", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(len(broker.orders), 1)

    def test_mtf_is_refused_because_its_costs_are_not_modelled(self):
        broker = FakeBroker()
        with self.assertRaises(LiveExecutionRejected) as caught:
            executor(broker).place_stop_buy(
                "buy:t1",
                Instrument("RELIANCE", "2885", "NSE_EQ", product_type="MTF"),
                quantity=10,
                trigger_price=1000.0,
            )
        self.assertIn("not armed", str(caught.exception))

    def test_insufficient_funds_blocks_the_stop(self):
        broker = FakeBroker(funds=500.0)
        with self.assertRaises(LiveExecutionRejected):
            executor(broker).place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(broker.orders, [])

    def test_an_unreadable_balance_is_not_permission_to_spend(self):
        class NoFunds(FakeBroker):
            def get_funds(self):
                raise RuntimeError("fundlimit 500")

        broker = NoFunds()
        with self.assertRaises(LiveExecutionRejected):
            executor(broker).place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(broker.orders, [])

    def test_orders_outside_the_session_are_refused(self):
        broker = FakeBroker()
        after_close = DhanRestingExecutor(
            broker,
            LiveGuardrails(max_order_inr=ORDER_CAP, max_campaign_inr=CAMPAIGN_CAP),
            clock=lambda: datetime(2026, 7, 28, 16, 30, tzinfo=IST),
        )
        with self.assertRaises(LiveExecutionRejected):
            after_close.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)


class DuplicateAndRecoveryTests(unittest.TestCase):
    def test_the_same_key_never_places_a_second_order(self):
        """The poll loop re-reads candles after a restart; this is the guard."""
        broker = FakeBroker()
        live = executor(broker)
        first = live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        second = live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(first.order_id, second.order_id)

    def test_an_ambiguous_submission_halts_and_never_retries(self):
        broker = FakeBroker(place_raises=AmbiguousOrderSubmission("timeout after POST"))
        live = executor(broker)
        with self.assertRaises(LiveExecutionHalt) as caught:
            live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertIn("reconcile", str(caught.exception).lower())
        with self.assertRaises(LiveExecutionHalt):
            live.place_stop_buy("buy:t2", SCRIP, quantity=9, trigger_price=1000.0)

    def test_a_restart_mid_flight_halts_instead_of_reordering(self):
        broker = FakeBroker()
        restored = executor(broker).restore(
            {"orders": {"buy:t1": {"key": "buy:t1", "side": "BUY", "quantity": 10, "status": "SUBMITTED"}}}
        )
        self.assertTrue(restored.halted)
        self.assertIn("never confirmed", restored.halt_reason)
        with self.assertRaises(LiveExecutionHalt):
            restored.place_stop_buy("buy:t2", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(broker.orders, [])

    def test_losing_sight_of_an_order_does_not_mark_it_gone(self):
        broker = FakeBroker()
        live = executor(broker)
        live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        broker.status_by_id["DHAN1"] = {"orderStatus": "UNKNOWN"}
        self.assertEqual(live.sync("buy:t1").status, RESTING)

    def test_day_orders_from_an_earlier_session_are_flagged_for_replacement(self):
        """Day validity is the cost of a true exchange-side stop."""
        live = executor(FakeBroker())
        live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        self.assertEqual(live.orders_needing_replacement(date(2026, 7, 28)), [])
        stale = live.orders_needing_replacement(date(2026, 7, 29))
        self.assertEqual([order.key for order in stale], ["buy:t1"])


class FillReconciliationTests(unittest.TestCase):
    def test_a_full_fill_is_read_back_from_the_broker(self):
        broker = FakeBroker()
        live = executor(broker)
        live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        broker.mark("DHAN1", "TRADED", filled=10, price=1001.5)
        order = live.sync("buy:t1")
        self.assertEqual(order.status, FILLED)
        self.assertEqual(order.filled_qty, 10)
        self.assertEqual(order.avg_price, 1001.5)
        self.assertEqual(live.deployed_inr, 10015.0)

    def test_a_partial_fill_is_booked_as_the_partial_it_was(self):
        broker = FakeBroker()
        live = executor(broker)
        live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        broker.mark("DHAN1", "PART_TRADED", filled=4, price=1001.0)
        order = live.sync("buy:t1")
        self.assertEqual(order.status, PARTIAL)
        self.assertEqual(order.filled_qty, 4)
        self.assertEqual(live.deployed_inr, 4004.0)

    def test_deployed_capital_is_not_double_counted_across_syncs(self):
        broker = FakeBroker()
        live = executor(broker)
        live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        broker.mark("DHAN1", "PART_TRADED", filled=4, price=1000.0)
        live.sync("buy:t1")
        broker.mark("DHAN1", "TRADED", filled=10, price=1000.0)
        live.sync("buy:t1")
        live.sync("buy:t1")
        self.assertEqual(live.deployed_inr, 10000.0)


class TargetReplacementTests(unittest.TestCase):
    """Each new buy lowers the average entry, so the target order is replaced."""

    def _with_resting_target(self):
        broker = FakeBroker()
        live = executor(broker)
        live.place_limit_sell("sell:1", SCRIP, quantity=9, limit_price=1100.0)
        return broker, live

    def test_replacement_cancels_the_old_order_then_rests_a_new_one(self):
        broker, live = self._with_resting_target()
        new = live.replace_limit_sell("sell:1", "sell:2", SCRIP, quantity=15, limit_price=1080.0)
        self.assertEqual(broker.cancelled, ["DHAN1"])
        self.assertEqual(live.orders["sell:1"].status, CANCELLED)
        self.assertEqual(new.quantity, 15)
        self.assertEqual(new.limit_price, 1080.0)
        self.assertEqual(broker.orders[-1]["order_type"], "LIMIT")

    def test_a_target_that_fills_mid_replacement_halts_rather_than_guessing(self):
        broker, live = self._with_resting_target()
        broker.mark("DHAN1", "TRADED", filled=10, price=1100.0)
        with self.assertRaises(LiveExecutionHalt) as caught:
            live.replace_limit_sell("sell:1", "sell:2", SCRIP, quantity=15, limit_price=1080.0)
        self.assertIn("while being replaced", str(caught.exception))

    def test_a_failed_cancel_halts_before_anything_else_is_placed(self):
        broker = FakeBroker(cancel_raises=RuntimeError("network"))
        live = executor(broker)
        live.place_limit_sell("sell:1", SCRIP, quantity=9, limit_price=1100.0)
        with self.assertRaises(LiveExecutionHalt):
            live.replace_limit_sell("sell:1", "sell:2", SCRIP, quantity=15, limit_price=1080.0)
        self.assertEqual(len(broker.orders), 1)

    def test_a_failed_replacement_says_the_holding_is_unprotected(self):
        """Between cancel and place there is no exit resting. Say so loudly."""
        broker = FakeBroker()
        live = executor(broker)
        live.place_limit_sell("sell:1", SCRIP, quantity=9, limit_price=1100.0)
        broker.place_raises = RuntimeError("rejected")
        with self.assertRaises(LiveExecutionHalt) as caught:
            live.replace_limit_sell("sell:1", "sell:2", SCRIP, quantity=15, limit_price=1080.0)
        self.assertIn("no exit resting", str(caught.exception))
        self.assertTrue(live.halted)

    def test_a_sell_needs_no_funds_check(self):
        broker = FakeBroker(funds=0.0)
        live = executor(broker)
        live.place_limit_sell("sell:1", SCRIP, quantity=9, limit_price=1100.0)
        self.assertEqual(broker.fund_calls, 0)


class PersistenceTests(unittest.TestCase):
    def test_orders_round_trip_through_persistence(self):
        broker = FakeBroker()
        live = executor(broker)
        live.place_stop_buy("buy:t1", SCRIP, quantity=9, trigger_price=1000.0)
        broker.mark("DHAN1", "TRADED", filled=10, price=1001.0)
        live.sync("buy:t1")

        restored = executor(FakeBroker()).restore(live.to_dict())
        self.assertFalse(restored.halted)
        self.assertEqual(restored.deployed_inr, live.deployed_inr)
        self.assertEqual(restored.orders["buy:t1"].filled_qty, 10)
        self.assertEqual(restored.orders["buy:t1"].status, FILLED)

    def test_resting_order_serialisation_is_lossless(self):
        order = RestingOrder(
            key="buy:t1",
            side="BUY",
            quantity=10,
            limit_price=1002.0,
            trigger_price=1000.0,
            order_id="DHAN1",
            status=RESTING,
            placed_for=date(2026, 7, 28),
        )
        self.assertEqual(RestingOrder.from_dict(order.to_dict()), order)


if __name__ == "__main__":
    unittest.main()


class LiveIsNotWiredTests(unittest.TestCase):
    """Pins the fact that no route can place a live cash order.

    engine/cascade_equity_live.py is complete and tested but imported by nothing
    except this file. If someone wires it, these tests fail — which is the point:
    connecting a real order path should be a deliberate act that trips a guard,
    not something that slips in unnoticed.
    """

    def test_no_module_but_this_test_imports_the_live_executor(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        importers = []
        for path in (
            list(root.glob("*.py")) + list((root / "engine").glob("*.py")) + list((root / "tools").glob("*.py"))
        ):
            if path.name == "cascade_equity_live.py":
                continue
            # An IMPORT, not a mention: several modules name it in comments
            # explaining why it is not wired, and those must not trip the guard.
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(from\s+\S*cascade_equity_live|import\s+\S*cascade_equity_live)", text, re.M):
                importers.append(str(path.relative_to(root)))
        self.assertEqual(
            importers,
            [],
            f"cascade_equity_live is now imported by {importers}. Live cash orders are only "
            f"acceptable once the held-position problem is answered — see the backtest.",
        )

    def test_the_gate_says_paper_only_rather_than_locked(self):
        import app as app_module

        gate = app_module._terminal_cascade_live_gate_status()
        self.assertFalse(gate["enabled"])
        self.assertFalse(gate["armed"])
        self.assertFalse(gate["wired"])
        self.assertIn("PAPER ONLY", gate["reason"])
