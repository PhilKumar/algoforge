"""The configured book: throttle, capital, and the flows-equal-net invariant.

tools/fib_space_book.py is the only place the fib-space design's headline
numbers are produced from the repo rather than from a scratch file, so the two
things it adds on top of the sweep -- the portfolio cooldown and the capital
profile -- are pinned here.
"""

import os
import sys
import unittest
from dataclasses import dataclass
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fib_space_book import apply_cooldown, capital_profile  # noqa: E402
from tools.fib_space_premium import price_campaign  # noqa: E402
from tools.fib_space_sweep import SYMBOLS  # noqa: E402


def _at(day: int, hour: int = 9, minute: int = 15) -> datetime:
    return datetime(2026, 3, day, hour, minute)


class CooldownTests(unittest.TestCase):
    def test_zero_days_keeps_everything(self):
        rows = [(_at(d), d) for d in (2, 3, 4)]
        self.assertEqual(apply_cooldown(rows, 0), rows)

    def test_drops_starts_inside_the_window(self):
        rows = [(_at(d), d) for d in (2, 3, 6, 7, 10)]
        kept = [payload for _, payload in apply_cooldown(rows, 3)]
        self.assertEqual(kept, [2, 6, 10])

    def test_clock_runs_from_the_last_ACCEPTED_start(self):
        """Two rejected starts in a row must not shorten the wait.

        If the clock ran from the previous row instead, a cluster of
        near-duplicate mothers would walk its own tail through one day at a
        time -- exactly the doubled bet the throttle exists to stop.
        """
        rows = [(_at(1), "a"), (_at(2), "b"), (_at(3), "c"), (_at(4), "d")]
        kept = [payload for _, payload in apply_cooldown(rows, 3)]
        self.assertEqual(kept, ["a", "d"])

    def test_banknifty_has_no_cooldown_and_nifty_does(self):
        """Locked config: throttling BankNifty only drops winners."""
        self.assertEqual(SYMBOLS["nifty"]["cooldown_days"], 3)
        self.assertNotIn("cooldown_days", SYMBOLS["banknifty"])
        self.assertNotIn("cooldown_days", SYMBOLS["sensex"])


class CapitalProfileTests(unittest.TestCase):
    def test_peak_open_is_the_deepest_simultaneous_outlay(self):
        flows = [
            (_at(2), -100.0),
            (_at(3), -250.0),  # both lots live: 350 out
            (_at(4), 400.0),
            (_at(5), -50.0),
        ]
        need, peak, when = capital_profile(flows)
        self.assertAlmostEqual(peak, 350.0)
        self.assertEqual(when, _at(3))

    def test_banked_profit_funds_later_trades(self):
        """MIN CAPITAL is below PEAK OPEN once the book has banked something.

        The first trade returns 300 on 100 staked; the second stakes 250, but
        200 of that is money the book already made, so only 100 was ever needed
        from outside even though 250 was open at once.
        """
        flows = [(_at(2), -100.0), (_at(3), 300.0), (_at(4), -250.0), (_at(5), 260.0)]
        need, peak, _ = capital_profile(flows)
        self.assertAlmostEqual(need, 100.0)
        self.assertAlmostEqual(peak, 250.0)
        self.assertLess(need, peak)

    def test_out_of_order_flows_are_sorted_before_walking(self):
        ordered = [(_at(2), -100.0), (_at(3), -250.0), (_at(4), 400.0)]
        self.assertEqual(capital_profile(list(reversed(ordered))), capital_profile(ordered))

    def test_empty_book_needs_nothing(self):
        self.assertEqual(capital_profile([]), (0.0, 0.0, None))


# ---------------------------------------------------------------- fakes ----
# Just the surface price_campaign touches.


@dataclass
class _Fill:
    timestamp: datetime
    index_price: float
    quantity: int
    lots: int


@dataclass
class _Round:
    fills: list
    exit_timestamp: datetime
    exit_reason: str = "target"


@dataclass
class _Result:
    rounds: list
    mother_timestamp: datetime = _at(1)
    status: str = "closed"
    bars_held: int = 10


@dataclass
class _Contract:
    strike: float
    expiry: date


@dataclass
class _Bar:
    timestamp: datetime
    close: float
    open: float = 0.0


class _Resolver:
    def select(self, when, index_price, right, view):
        return _Contract(strike=24_000.0, expiry=date(2026, 3, 26))


class _Source:
    """A book that prints ``price`` at every minute asked for."""

    def __init__(self, price: float, exit_price: float, exit_at: datetime):
        self.price, self.exit_price, self.exit_at = price, exit_price, exit_at

    def lookup(self, when, contract):
        return _Bar(timestamp=when, close=0.0, open=self.exit_price if when >= self.exit_at else self.price)


class FlowsMatchNetTests(unittest.TestCase):
    """The capital number and the P&L number must come from one walk.

    price_campaign emits flows itself precisely so a second implementation
    cannot drift from it; this is the assertion that keeps that true.
    """

    def _price(self, rounds, source):
        return price_campaign(
            _Result(rounds=rounds),
            [],
            source,
            _Resolver(),
            view=None,
            settle_bars=[_Bar(timestamp=datetime(2026, 3, 26, 15, 15), close=24_500.0)],
            timeframe="5m",
            symbol="banknifty",
        )

    def test_flows_sum_to_net_on_a_banked_round(self):
        entry, exit_at = _at(2, 10, 0), _at(4, 11, 0)
        rounds = [_Round(fills=[_Fill(entry, 24_100.0, 30, 1)], exit_timestamp=exit_at)]
        priced = self._price(rounds, _Source(price=120.0, exit_price=180.0, exit_at=exit_at))

        self.assertEqual(priced.status, "priced")
        self.assertAlmostEqual(sum(a for _, a in priced.flows), priced.net, places=2)

    def test_flows_sum_to_net_across_a_laddered_multi_round_campaign(self):
        first, second = _at(2, 10, 0), _at(3, 10, 0)
        exit_at = _at(5, 11, 0)
        rounds = [
            _Round(fills=[_Fill(first, 24_100.0, 30, 1), _Fill(second, 23_900.0, 60, 2)], exit_timestamp=exit_at),
            _Round(fills=[_Fill(_at(6, 10, 0), 23_800.0, 30, 1)], exit_timestamp=_at(9, 11, 0)),
        ]
        priced = self._price(rounds, _Source(price=100.0, exit_price=150.0, exit_at=exit_at))

        self.assertEqual(priced.rounds, 2)
        self.assertAlmostEqual(sum(a for _, a in priced.flows), priced.net, places=2)

    def test_money_leaves_at_the_fill_and_returns_at_the_exit(self):
        entry, exit_at = _at(2, 10, 0), _at(4, 11, 0)
        rounds = [_Round(fills=[_Fill(entry, 24_100.0, 30, 1)], exit_timestamp=exit_at)]
        priced = self._price(rounds, _Source(price=120.0, exit_price=180.0, exit_at=exit_at))

        buys = [(w, a) for w, a in priced.flows if a < 0]
        self.assertIn((entry, -120.0 * 30), buys)
        self.assertIn((exit_at, 180.0 * 30), priced.flows)
        # Costs come out when the last leg closes, never before it.
        self.assertTrue(all(w >= entry for w, _ in priced.flows))

    def test_a_ladder_holds_every_lot_until_the_round_exits(self):
        """Peak open must count all three lots, not just the last one."""
        exit_at = _at(9, 11, 0)
        fills = [_Fill(_at(2 + i, 10, 0), 24_000.0 - 100 * i, 30 * (i + 1), i + 1) for i in range(3)]
        priced = self._price(
            [_Round(fills=fills, exit_timestamp=exit_at)], _Source(price=100.0, exit_price=140.0, exit_at=exit_at)
        )

        _, peak, when = capital_profile(list(priced.flows))
        self.assertAlmostEqual(peak, 100.0 * (30 + 60 + 90))
        self.assertEqual(when, fills[-1].timestamp)


if __name__ == "__main__":
    unittest.main()
