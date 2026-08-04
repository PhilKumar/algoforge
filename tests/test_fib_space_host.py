"""The poll cycle: the session gate, the lookback, and errors that stay local.

The gate is the test that matters operationally -- an out-of-hours poll once
spent the account-wide Dhan budget and produced a 4 AM 429 storm that starved
the live engine.
"""

import asyncio
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_options import IndexCandle  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig  # noqa: E402
from engine.fib_space_host import LIVE_SYMBOLS, MOTHER_PIVOT_BARS, FibSpacePaperHost  # noqa: E402
from tools.fib_space_sweep import MOTHER_PIVOT_BARS as SWEEP_PIVOT_BARS  # noqa: E402
from tools.fib_space_sweep import SYMBOLS as SWEEP_SYMBOLS  # noqa: E402


class _Contract:
    def __init__(self, strike, expiry):
        self.strike, self.expiry = strike, expiry


def _select(when, index_price):
    return _Contract(strike=round(index_price / 100.0) * 100.0, expiry=date(2026, 3, 26))


class _Adapter:
    """Serves a canned series and counts every call."""

    def __init__(self, candles):
        self.candles = candles
        self.calls = []
        self.fail = None

    async def async_get_candles(self, symbol, timeframe, *, from_date=None, to_date=None, now=None):
        self.calls.append((symbol, timeframe, from_date, to_date))
        if self.fail:
            raise RuntimeError(self.fail)
        return list(self.candles)


def _series(n=140, *, start=datetime(2026, 3, 2, 9, 15), step=15):
    """A warm-up, a run-up, a pivot, then a long fall -- enough for a mother.

    The warm-up is not padding: the scanner needs ATR(14) before it will judge
    any pivot, so a series that starts at the run-up produces no mothers at all.
    """
    out, at, price = [], start, 23_600.0
    for _ in range(20):  # ATR warm-up: quiet, and well under the pivot high
        out.append(IndexCandle(at, price, price + 20, price - 20, price + 5))
        price += 5
        at += timedelta(minutes=step)
    for _ in range(6):  # the run into the pivot
        out.append(IndexCandle(at, price, price + 30, price - 10, price + 25))
        price += 25
        at += timedelta(minutes=step)
    out.append(IndexCandle(at, price, price + 120, price - 20, price - 10))  # the mother
    price -= 10
    at += timedelta(minutes=step)
    for i in range(n):
        drop = 45 if i % 5 else -35
        out.append(IndexCandle(at, price, price + 12, price - drop - 12, price - drop))
        price -= drop
        at += timedelta(minutes=step)
    return out


def _host(adapter, **kwargs):
    return FibSpacePaperHost(
        "banknifty",
        adapter,
        premium_lookup=lambda when, contract: 150.0,
        select_contract=_select,
        config=SpaceCascadeConfig(lot_size=30),
        entry_timeframe="15m",
        geometry_timeframe="15m",
        **kwargs,
    )


class SessionGateTests(unittest.TestCase):
    def test_a_poll_outside_the_session_does_no_broker_io_at_all(self):
        adapter = _Adapter(_series())
        host = _host(adapter)
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 4, 0)))

        self.assertEqual(report.skipped, "outside NSE cash session")
        self.assertEqual(adapter.calls, [], "the gate must come BEFORE the fetch, not after")

    def test_a_poll_inside_the_session_fetches(self):
        adapter = _Adapter(_series())
        host = _host(adapter)
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))

        self.assertIsNone(report.skipped)
        self.assertTrue(adapter.calls)
        self.assertGreater(report.geometry_bars, 0)

    def test_the_weekend_is_outside_the_session(self):
        adapter = _Adapter(_series())
        host = _host(adapter)
        # 2026-03-07 is a Saturday.
        report = asyncio.run(host.poll(now=datetime(2026, 3, 7, 11, 0)))
        self.assertEqual(report.skipped, "outside NSE cash session")
        self.assertEqual(adapter.calls, [])


class FetchShapeTests(unittest.TestCase):
    def test_one_request_per_timeframe_when_they_differ(self):
        adapter = _Adapter(_series())
        host = FibSpacePaperHost(
            "banknifty",
            adapter,
            premium_lookup=lambda when, contract: 150.0,
            select_contract=_select,
            config=SpaceCascadeConfig(lot_size=30),
            entry_timeframe="5m",
            geometry_timeframe="15m",
        )
        asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        self.assertEqual([tf for _, tf, _, _ in adapter.calls], ["15m", "5m"])

    def test_only_one_request_when_the_timeframes_are_the_same(self):
        adapter = _Adapter(_series())
        asyncio.run(_host(adapter).poll(now=datetime(2026, 3, 3, 11, 0)))
        self.assertEqual(len(adapter.calls), 1)

    def test_the_lookback_reaches_past_the_campaign_horizon(self):
        """A short window would truncate an old campaign's replay under it."""
        adapter = _Adapter(_series())
        host = _host(adapter, lookback_days=260)
        now = datetime(2026, 3, 3, 11, 0)
        asyncio.run(host.poll(now=now))

        _, _, from_date, _ = adapter.calls[0]
        self.assertEqual((now.date() - from_date).days, 260)


class ErrorsStayLocalTests(unittest.TestCase):
    def test_a_broker_failure_is_reported_not_raised(self):
        adapter = _Adapter(_series())
        adapter.fail = "Dhan 429"
        report = asyncio.run(_host(adapter).poll(now=datetime(2026, 3, 3, 11, 0)))

        self.assertIn("Dhan 429", report.error)
        self.assertFalse(report.changed)

    def test_an_empty_feed_is_skipped_not_treated_as_a_market(self):
        report = asyncio.run(_host(_Adapter([])).poll(now=datetime(2026, 3, 3, 11, 0)))
        self.assertEqual(report.skipped, "no closed geometry bars yet")


class DrivingTests(unittest.TestCase):
    def test_a_mother_is_found_and_a_campaign_starts(self):
        host = _host(_Adapter(_series()))
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))

        self.assertGreater(report.mothers_seen, 0)
        self.assertTrue(report.campaigns_started)
        self.assertTrue(report.changed)

    def test_polling_twice_does_not_restart_the_same_campaign(self):
        host = _host(_Adapter(_series()))
        first = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        second = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 1)))

        self.assertTrue(first.campaigns_started)
        self.assertEqual(second.campaigns_started, [])
        self.assertEqual(len(host.book.campaigns), len(first.campaigns_started))

    def test_a_fill_is_reported_once_and_only_once(self):
        host = _host(_Adapter(_series()))
        first = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        second = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 1)))

        self.assertTrue(first.fills)
        self.assertEqual(second.fills, [], "a re-poll must not re-report a decision already recorded")

    def test_snapshot_describes_every_campaign(self):
        host = _host(_Adapter(_series()))
        asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        snap = host.snapshot()

        self.assertEqual(snap["symbol"], "banknifty")
        self.assertEqual(len(snap["campaign_rows"]), snap["campaigns"])
        self.assertIsNotNone(snap["last_poll"])
        self.assertTrue(all("mother" in row for row in snap["campaign_rows"]))

    def test_report_serialises_for_the_wire(self):
        host = _host(_Adapter(_series()))
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        payload = report.to_dict()

        self.assertIn("started", payload)
        self.assertIsInstance(payload["fills"], list)
        if payload["fills"]:
            self.assertIn("premium", payload["fills"][0])


class ConfigParityTests(unittest.TestCase):
    """A paper run on terms the backtest never measured proves nothing.

    engine/fib_space_host.py restates the config so the live process need not
    import the sweep (and its cache path). These assertions are what make that
    duplication safe.
    """

    def test_the_live_pivot_width_matches_the_backtest(self):
        self.assertEqual(MOTHER_PIVOT_BARS, SWEEP_PIVOT_BARS)

    def test_every_live_symbol_matches_its_measured_contract_terms(self):
        for symbol, live in LIVE_SYMBOLS.items():
            with self.subTest(symbol=symbol):
                measured = SWEEP_SYMBOLS[symbol]
                self.assertEqual(live["strike_step"], measured["strike_step"])
                self.assertEqual(live["monthly_only"], measured["contract"]["monthly_only"])
                self.assertEqual(live["min_dte"], measured["contract"]["min_dte"])

    def test_every_live_symbol_matches_its_measured_cooldown(self):
        for symbol, live in LIVE_SYMBOLS.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(live["cooldown_days"], SWEEP_SYMBOLS[symbol].get("cooldown_days", 0))

    def test_the_live_strike_offset_is_the_measured_atm_minus_two(self):
        from tools.fib_space_premium import resolver_view

        for symbol, live in LIVE_SYMBOLS.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(live["itm_steps"], resolver_view(symbol).itm_steps)


if __name__ == "__main__":
    unittest.main()
