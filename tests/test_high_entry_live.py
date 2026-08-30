"""High Entry's live order path: the replay decides, the book remembers.

The strategy REPLAYS its whole campaign from bars on every poll. That is right
for paper and it is the one thing that cannot be pointed at a broker unchanged
-- a second replay of the same trade must not buy it twice, and a trade that
disappears from a later replay must never be quietly sold.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from unittest.mock import patch

from engine.candle_recovery_live import RecoveryOrderBook, trade_key
from engine.options_live_executor import (
    ExecutionRefused,
    OptionsLiveExecutor,
    OptionsPaperExecutor,
    OrderRejected,
    build_executor,
)

WHEN = datetime(2026, 8, 30, 10, 15)


@dataclass
class FakeTrade:
    """What the replay hands the book."""

    trade_no: int
    armed_at: datetime
    entry_time: Optional[datetime] = WHEN
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    strike: Optional[int] = 24_400
    expiry: date = date(2026, 9, 3)
    quantity: int = 75
    entry_premium: Optional[float] = 200.0


class _Executor(OptionsPaperExecutor):
    """A paper executor that counts what it was asked to do."""

    def __init__(self):
        super().__init__(tag="TEST")
        self.buys: list = []
        self.sells: list = []
        self.released: list = []

    def buy(self, **kw):
        self.buys.append(kw)
        return super().buy(**kw)

    def sell(self, **kw):
        self.sells.append(kw)
        return super().sell(**kw)

    def cancel_bracket(self, *, order_id):
        self.released.append(str(order_id))
        return super().cancel_bracket(order_id=order_id)


def book():
    ex = _Executor()
    return RecoveryOrderBook(ex), ex


def sync(bk, trades, when=WHEN):
    return bk.sync("C1", trades, symbol="NIFTY", side="CE", when=when)


class OrderBookTests(unittest.TestCase):
    def test_an_open_trade_is_bought_once_however_often_it_replays(self):
        """The whole point. Every poll replays the same trade; the second,
        third and hundredth replay must not buy it again."""
        bk, ex = book()
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        for _ in range(5):
            sync(bk, [trade])
        self.assertEqual(len(ex.buys), 1)

    def test_the_key_survives_a_renumbered_replay(self):
        """A revised candle can renumber the list. The bar that armed the
        trade is a fact about the market, so identity hangs off that."""
        armed = datetime(2026, 8, 30, 9, 45)
        self.assertEqual(
            trade_key("C1", FakeTrade(trade_no=1, armed_at=armed)),
            trade_key("C1", FakeTrade(trade_no=1, armed_at=armed)),
        )
        self.assertNotEqual(
            trade_key("C1", FakeTrade(trade_no=1, armed_at=armed)),
            trade_key("C1", FakeTrade(trade_no=2, armed_at=armed)),
        )

    def test_a_trade_that_closed_before_the_book_ever_saw_it_is_not_bought(self):
        """History the app was down for. Buying it now would open a position
        the rules have already exited."""
        bk, ex = book()
        sync(bk, [FakeTrade(trade_no=1, armed_at=WHEN, exit_time=WHEN, exit_reason="stop")])
        self.assertEqual(ex.buys, [])

    def test_the_buy_carries_a_stop_inside_it(self):
        bk, ex = book()
        sync(bk, [FakeTrade(trade_no=1, armed_at=WHEN)])
        self.assertEqual(ex.buys[0]["stop_price"], 60.0, "70% under the 200 paid")
        record = bk.orders[trade_key("C1", FakeTrade(trade_no=1, armed_at=WHEN))]
        self.assertTrue(record["bracket_order_id"])

    def test_closing_the_trade_releases_the_bracket_before_selling(self):
        """A stop still working at Dhan would sell a position already gone."""
        bk, ex = book()
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [trade])
        bracket = bk.orders[trade_key("C1", trade)]["bracket_order_id"]
        trade.exit_time = datetime(2026, 8, 30, 11, 0)
        trade.exit_reason = "target"
        sync(bk, [trade])
        self.assertEqual(ex.released, [bracket])
        self.assertEqual(len(ex.sells), 1)
        self.assertEqual(bk.orders[trade_key("C1", trade)]["state"], "CLOSED")

    def test_a_bracket_leg_that_already_traded_books_instead_of_selling_again(self):
        class _AlreadyTraded(_Executor):
            def cancel_bracket(self, *, order_id):
                return {"order_id": str(order_id), "traded": True, "avg_price": 58.0}

        ex = _AlreadyTraded()
        bk = RecoveryOrderBook(ex)
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [trade])
        trade.exit_time = datetime(2026, 8, 30, 11, 0)
        sync(bk, [trade])
        self.assertEqual(ex.sells, [], "the stop already sold it")
        record = bk.orders[trade_key("C1", trade)]
        self.assertEqual(record["state"], "CLOSED")
        self.assertEqual(record["exit_price"], 58.0)

    def test_a_trade_that_vanishes_freezes_the_book_and_sells_nothing(self):
        """The vendor revised a candle under us. An order nobody can explain
        is not one a program should be closing."""
        bk, ex = book()
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [trade])
        outcome = sync(bk, [])  # the replay no longer reports it
        self.assertTrue(outcome["frozen"])
        self.assertTrue(bk.frozen)
        self.assertEqual(ex.sells, [], "nothing is sold on a disagreement")

    def test_a_frozen_book_opens_nothing_new(self):
        bk, ex = book()
        first = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [first])
        sync(bk, [])
        sync(bk, [FakeTrade(trade_no=2, armed_at=datetime(2026, 8, 30, 12, 0))])
        self.assertEqual(len(ex.buys), 1, "still only the first")

    def test_only_a_human_thaws_it(self):
        bk, ex = book()
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [trade])
        sync(bk, [])
        self.assertTrue(bk.frozen)
        self.assertEqual(bk.clear_freeze(WHEN), 1)
        self.assertFalse(bk.frozen)

    def test_an_unknown_exit_freezes_rather_than_guesses(self):
        class _Unknown(_Executor):
            def sell(self, **kw):
                self.sells.append(kw)
                return {"order_id": "X1", "status": "UNKNOWN", "avg_price": None}

        ex = _Unknown()
        bk = RecoveryOrderBook(ex)
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [trade])
        trade.exit_time = datetime(2026, 8, 30, 11, 0)
        sync(bk, [trade])
        self.assertTrue(bk.frozen)
        self.assertEqual(bk.orders[trade_key("C1", trade)]["state"], "EXIT_UNKNOWN")

    def test_a_refused_entry_records_no_order(self):
        """Recording an order the broker never took is how a book starts
        lying. A refusal can be retried; a phantom cannot be undone."""

        class _Refusing(_Executor):
            def buy(self, **kw):
                raise ExecutionRefused("not armed")

        bk = RecoveryOrderBook(_Refusing())
        sync(bk, [FakeTrade(trade_no=1, armed_at=WHEN)])
        self.assertEqual(bk.orders, {})
        self.assertFalse(bk.frozen)

    def test_the_book_survives_a_restart(self):
        bk, _ = book()
        trade = FakeTrade(trade_no=1, armed_at=WHEN)
        sync(bk, [trade])
        back = RecoveryOrderBook(_Executor())
        back.load(bk.to_dict())
        self.assertEqual(back.orders, bk.orders)
        # And the restored book does not re-buy what it already holds.
        sync(back, [trade])
        self.assertEqual(back.executor.buys, [])


class SharedExecutorTests(unittest.TestCase):
    """The executor four strategies will share. Closed until proven."""

    def test_live_is_refused_while_the_gate_is_shut(self):
        live = OptionsLiveExecutor(broker=object(), symbol="NIFTY", armed=True)
        with self.assertRaisesRegex(ExecutionRefused, "built but disabled"):
            live.buy(when=WHEN, strike=24_400, expiry=date(2026, 9, 3), option_type="CE", quantity=75, premium=200.0)

    def test_armed_is_still_required_once_the_gate_opens(self):
        live = OptionsLiveExecutor(broker=object(), symbol="NIFTY")
        with patch("engine.options_live_executor.OPTIONS_LIVE_EXECUTION_ENABLED", True):
            with self.assertRaisesRegex(ExecutionRefused, "not armed"):
                live.buy(
                    when=WHEN, strike=24_400, expiry=date(2026, 9, 3), option_type="CE", quantity=75, premium=200.0
                )

    def test_a_bracketed_buy_is_one_super_order_with_no_invented_target(self):
        sent = {}

        class _Broker:
            def place_super_order(self, **order):
                sent.update(order)
                return {"orderId": "SO-1"}

            def verify_order_fill(self, order_id, max_wait_sec=20):
                return {"status": "FILLED", "filled_qty": 75, "avg_price": 198.5}

        live = OptionsLiveExecutor(broker=_Broker(), symbol="NIFTY", armed=True, tag="PF_HIGH_ENTRY")
        with patch("engine.options_live_executor.OPTIONS_LIVE_EXECUTION_ENABLED", True):
            receipt = live.buy(
                when=WHEN,
                strike=24_400,
                expiry=date(2026, 9, 3),
                option_type="CE",
                quantity=75,
                premium=200.0,
                stop_price=60.0,
            )
        self.assertEqual(receipt["bracket_order_id"], "SO-1")
        self.assertEqual(receipt["traded_premium"], 198.5)
        self.assertEqual(sent["target_price"], 0.0)
        self.assertEqual(sent["stop_loss_price"], 60.0)
        self.assertEqual(sent["product_type"], "MARGIN")
        self.assertEqual(sent["tag"], "PF_HIGH_ENTRY_SO")

    def test_a_rejection_and_an_unknown_are_different_answers(self):
        class _Rejects:
            def place_option_order(self, **order):
                return {"orderId": "R-1"}

            def verify_order_fill(self, order_id, max_wait_sec=20):
                return {"status": "REJECTED", "filled_qty": 0, "message": "margin"}

        class _Silent:
            def place_option_order(self, **order):
                return {"orderId": "U-1"}

            def verify_order_fill(self, order_id, max_wait_sec=20):
                return {"status": "TIMEOUT", "filled_qty": 0}

        with patch("engine.options_live_executor.OPTIONS_LIVE_EXECUTION_ENABLED", True):
            live = OptionsLiveExecutor(broker=_Rejects(), symbol="NIFTY", armed=True)
            with self.assertRaises(OrderRejected):
                live.buy(
                    when=WHEN, strike=24_400, expiry=date(2026, 9, 3), option_type="CE", quantity=75, premium=200.0
                )
            silent = OptionsLiveExecutor(broker=_Silent(), symbol="NIFTY", armed=True)
            with self.assertRaises(RuntimeError) as caught:
                silent.buy(
                    when=WHEN, strike=24_400, expiry=date(2026, 9, 3), option_type="CE", quantity=75, premium=200.0
                )
            self.assertNotIsInstance(caught.exception, OrderRejected)

    def test_build_executor_gives_paper_unless_live_is_asked_for(self):
        self.assertIsInstance(build_executor(object(), "NIFTY", mode="paper"), OptionsPaperExecutor)
        self.assertIsInstance(build_executor(object(), "NIFTY", mode="live"), OptionsLiveExecutor)


if __name__ == "__main__":
    unittest.main()


class GapCarryLiveTests(unittest.TestCase):
    """Gap Carry holds ONE leg across one night. The seam is its arm and its
    close, and the close must refuse to book a position it could not sell."""

    def engine(self, executor):
        from datetime import time as dt_time

        from engine.gap_carry import GapCarryConfig
        from engine.gap_carry_paper import GapCarryPaper

        return GapCarryPaper(
            config=GapCarryConfig(),
            option_premium_lookup=lambda when, strike, side, expiry: 200.0,
            expiry_lookup=lambda session: date(2026, 9, 3),
            lot_size_lookup=lambda expiry: 75,
            executor=executor,
        ), dt_time

    def armed(self, executor):
        """Arm one position without driving the whole signal machinery."""
        from engine.gap_carry import SignalReading

        eng, _ = self.engine(executor)
        signal = SignalReading(
            timestamp=datetime(2026, 8, 31, 15, 10),
            close=24_500.0,
            ema=24_480.0,
            rsi=61.0,
            side="CE",
            reason="test",
        )
        eng.last_index_close = 24_500.0
        eng._arm(date(2026, 8, 31), signal)
        return eng

    def test_a_live_arm_sends_one_bracketed_buy(self):
        ex = _Executor()
        eng = self.armed(ex)
        self.assertEqual(len(ex.buys), 1)
        self.assertEqual(ex.buys[0]["stop_price"], 60.0)
        self.assertIsNotNone(eng.position)
        self.assertTrue(eng.position.order_id)
        self.assertTrue(eng.position.bracket_order_id)

    def test_a_paper_arm_sends_nothing_and_carries_no_order_id(self):
        eng = self.armed(None)
        self.assertIsNotNone(eng.position)
        self.assertIsNone(eng.position.order_id)

    def test_a_refused_entry_leaves_no_position(self):
        class _Refusing(_Executor):
            def buy(self, **kw):
                raise ExecutionRefused("not armed")

        eng = self.armed(_Refusing())
        self.assertIsNone(eng.position, "nothing is held that was never bought")

    def test_an_unknown_entry_freezes_and_holds_no_position(self):
        class _Unknown(_Executor):
            def buy(self, **kw):
                raise RuntimeError("timeout")

        eng = self.armed(_Unknown())
        self.assertIsNone(eng.position)
        self.assertTrue(eng.frozen_reason)

    def test_the_exit_books_the_price_dhan_traded(self):
        class _Priced(_Executor):
            def sell(self, **kw):
                self.sells.append(kw)
                return {"order_id": "X1", "status": "FILLED", "avg_price": 244.0}

        ex = _Priced()
        eng = self.armed(ex)
        pos = eng.position
        eng._close(pos, datetime(2026, 9, 1, 9, 20), 250.0, "clock", priced=True)
        self.assertEqual(pos.exit_premium, 244.0, "the broker's price, not the quote")
        self.assertEqual(eng.position, None)

    def test_an_unknown_exit_does_not_retire_the_position(self):
        """A book that marks a position closed while its order may still be
        working is a book that lies about real money."""

        class _Unknown(_Executor):
            def sell(self, **kw):
                self.sells.append(kw)
                return {"order_id": "X1", "status": "UNKNOWN", "avg_price": None}

        eng = self.armed(_Unknown())
        pos = eng.position
        eng._close(pos, datetime(2026, 9, 1, 9, 20), 250.0, "clock", priced=True)
        self.assertIs(eng.position, pos, "still held")
        self.assertEqual(eng.history, [])
        self.assertTrue(eng.frozen_reason)

    def test_the_bracket_is_released_before_the_leg_is_sold(self):
        ex = _Executor()
        eng = self.armed(ex)
        pos = eng.position
        bracket = pos.bracket_order_id
        eng._close(pos, datetime(2026, 9, 1, 9, 20), 250.0, "clock", priced=True)
        self.assertEqual(ex.released, [bracket])
        self.assertEqual(len(ex.sells), 1)
