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

    def test_the_snapshot_says_WHY_a_running_book_did_nothing(self):
        """Otherwise "running, last poll not yet" reads as broken.

        Outside 09:15–15:30 the loop makes no broker call, so a mother named in
        the evening sits at zero fills and no rupees until morning. last_poll
        stays None on a skip, so the panel cannot work it out from timestamps —
        the reason has to travel in the snapshot.
        """
        adapter = _Adapter(_series())
        host = _host(adapter)
        self.assertIsNone(host.snapshot()["skipped"], "nothing polled yet, nothing to explain")

        asyncio.run(host.poll(now=datetime(2026, 3, 3, 4, 0)))
        self.assertEqual(host.snapshot()["skipped"], "outside NSE cash session")

        asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        self.assertIsNone(host.snapshot()["skipped"], "a real poll must clear the reason")

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


class FetchWindowTests(unittest.TestCase):
    """The window is sized by demand.

    A flat 260-day request is what broke this in production: Dhan's intraday
    endpoint is not built for a range that long, and every other caller in this
    repo asks for a fortnight.
    """

    def test_an_empty_book_asks_for_a_short_window(self):
        adapter = _Adapter(_series())
        now = datetime(2026, 3, 3, 11, 0)
        asyncio.run(_host(adapter).poll(now=now))

        _, _, from_date, _ = adapter.calls[0]
        self.assertEqual((now.date() - from_date).days, 30)

    def test_the_window_reaches_back_to_the_oldest_campaign(self):
        """Short of the oldest mother would truncate its replay and halt it."""
        bars_source = _series()
        host = _host(_Adapter(bars_source))
        now = datetime(2026, 6, 1, 11, 0)
        asyncio.run(host.start_named_mother(bars_source[26].timestamp, now=now))
        host.adapter.calls.clear()

        asyncio.run(host.poll(now=now))
        earliest = min(from_date for _, _, from_date, _ in host.adapter.calls)
        self.assertLessEqual(earliest, bars_source[26].timestamp.date())

    def test_a_long_window_is_split_rather_than_sent_whole(self):
        bars_source = _series()
        host = _host(_Adapter(bars_source))
        now = datetime(2026, 7, 31, 11, 0)  # a Friday, ~5 months past the mother
        asyncio.run(host.start_named_mother(bars_source[26].timestamp, now=now))
        host.adapter.calls.clear()

        asyncio.run(host.poll(now=now))
        spans = [(to_date - from_date).days for _, _, from_date, to_date in host.adapter.calls]
        self.assertGreater(len(host.adapter.calls), 1, "a long range must be split")
        self.assertTrue(all(s <= 60 for s in spans), f"every slice must stay small: {spans}")

    def test_split_slices_do_not_duplicate_a_bar(self):
        """The seams overlap, so bars must be de-duplicated by timestamp."""
        bars_source = _series()
        host = _host(_Adapter(bars_source))
        now = datetime(2026, 8, 1, 11, 0)
        asyncio.run(host.start_named_mother(bars_source[26].timestamp, now=now))

        candles = asyncio.run(host._fetch("15m", now=now))
        stamps = [c.timestamp for c in candles]
        self.assertEqual(len(stamps), len(set(stamps)))
        self.assertEqual(stamps, sorted(stamps))


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
    """The scanner path. It is opt-in now, so these ask for it explicitly."""

    def test_a_mother_is_found_and_a_campaign_starts(self):
        host = _host(_Adapter(_series()), auto_scan=True)
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))

        self.assertGreater(report.mothers_seen, 0)
        self.assertTrue(report.campaigns_started)
        self.assertTrue(report.changed)

    def test_polling_twice_does_not_restart_the_same_campaign(self):
        host = _host(_Adapter(_series()), auto_scan=True)
        first = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        second = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 1)))

        self.assertTrue(first.campaigns_started)
        self.assertEqual(second.campaigns_started, [])
        self.assertEqual(len(host.book.campaigns), len(first.campaigns_started))

    def test_a_fill_is_reported_once_and_only_once(self):
        host = _host(_Adapter(_series()), auto_scan=True)
        first = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        second = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 1)))

        self.assertTrue(first.fills)
        self.assertEqual(second.fills, [], "a re-poll must not re-report a decision already recorded")

    def test_snapshot_describes_every_campaign(self):
        host = _host(_Adapter(_series()), auto_scan=True)
        asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))
        snap = host.snapshot()

        self.assertGreater(snap["campaigns"], 0)

        self.assertEqual(snap["symbol"], "banknifty")
        self.assertEqual(len(snap["campaign_rows"]), snap["campaigns"])
        self.assertIsNotNone(snap["last_poll"])
        self.assertTrue(all("mother" in row for row in snap["campaign_rows"]))

    def test_report_serialises_for_the_wire(self):
        host = _host(_Adapter(_series()), auto_scan=True)
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


class AutoScanIsOptInTests(unittest.TestCase):
    """Naming the mother is the mode; the scanner is what a backtest must do."""

    def test_the_scanner_is_off_by_default(self):
        host = _host(_Adapter(_series()))
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))

        self.assertFalse(host.auto_scan)
        self.assertEqual(report.mothers_seen, 0)
        self.assertEqual(report.campaigns_started, [])
        self.assertEqual(len(host.book.campaigns), 0)

    def test_the_scanner_still_works_when_asked_for(self):
        """The measured numbers came from it, so a like-for-like run needs it."""
        host = _host(_Adapter(_series()), auto_scan=True)
        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 0)))

        self.assertGreater(report.mothers_seen, 0)
        self.assertTrue(report.campaigns_started)

    def test_the_snapshot_says_which_mode_it_is_in(self):
        self.assertFalse(_host(_Adapter(_series())).snapshot()["auto_scan"])
        self.assertTrue(_host(_Adapter(_series()), auto_scan=True).snapshot()["auto_scan"])


class NamedMotherFetchTests(unittest.TestCase):
    def test_it_takes_the_high_and_low_from_the_market_bar(self):
        candles = _series()
        host = _host(_Adapter(candles))
        target = candles[26].timestamp  # the pivot in the fixture

        campaign = asyncio.run(host.start_named_mother(target, now=datetime(2026, 3, 3, 11, 0)))

        self.assertEqual(campaign.mother.timestamp, target)
        self.assertEqual(campaign.mother.high, candles[26].high)
        self.assertEqual(campaign.mother.low, candles[26].low)
        self.assertEqual(campaign.source, "manual")

    def test_a_timestamp_with_no_candle_is_an_error_not_an_invented_bar(self):
        host = _host(_Adapter(_series()))
        with self.assertRaises(LookupError) as caught:
            asyncio.run(host.start_named_mother(datetime(2021, 1, 4, 10, 0), now=datetime(2026, 3, 3, 11, 0)))
        self.assertIn("no closed", str(caught.exception))

    def test_a_named_mother_is_advanced_by_the_ordinary_poll(self):
        candles = _series()
        host = _host(_Adapter(candles))
        asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 11, 0)))

        report = asyncio.run(host.poll(now=datetime(2026, 3, 3, 11, 1)))
        self.assertTrue(report.fills, "the named mother must trade like any other campaign")
        self.assertEqual(host.snapshot()["campaign_rows"][0]["source"], "manual")


class NamedMotherIsDrawableImmediatelyTests(unittest.TestCase):
    """Naming a mother is asking "does the engine see what I see" — so it draws."""

    def test_the_chart_is_ready_the_moment_the_mother_is_accepted(self):
        candles = _series()
        host = _host(_Adapter(candles))
        campaign = asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 11, 0)))

        chart = host.book.campaign_chart(campaign)
        self.assertEqual(chart["status"], "ok")
        self.assertTrue(chart["candles"])

    def test_it_draws_even_outside_the_session(self):
        """The poll is gated; drawing must not be — that was the whole bug."""
        candles = _series()
        host = _host(_Adapter(candles))
        # 20:30 on a weekday: no poll would ever run at this hour.
        campaign = asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 20, 30)))

        self.assertEqual(host.book.campaign_chart(campaign)["status"], "ok")

    def test_naming_a_mother_records_no_trade(self):
        candles = _series()
        host = _host(_Adapter(candles))
        campaign = asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 20, 30)))

        self.assertEqual(campaign.fills, [])
        self.assertEqual(host.snapshot()["open_quantity"], 0)


class EnsureDrawableTests(unittest.TestCase):
    """A campaign adopted before any poll must still produce a chart."""

    def test_it_builds_a_replay_when_there_is_none(self):
        candles = _series()
        host = _host(_Adapter(candles))
        campaign = asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 11, 0)))
        # Simulate a campaign that predates the preview (e.g. adopted by an
        # older build, or restored from saved state).
        campaign.last_result = None
        campaign.last_bars = []

        asyncio.run(host.ensure_drawable(campaign, now=datetime(2026, 3, 3, 20, 30)))
        self.assertEqual(host.book.campaign_chart(campaign)["status"], "ok")

    def test_it_does_not_refetch_when_a_replay_is_already_there(self):
        candles = _series()
        host = _host(_Adapter(candles))
        campaign = asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 11, 0)))
        host.adapter.calls.clear()

        asyncio.run(host.ensure_drawable(campaign, now=datetime(2026, 3, 3, 11, 1)))
        self.assertEqual(host.adapter.calls, [], "a drawable campaign must not spend broker calls")

    def test_building_a_chart_records_no_trade(self):
        candles = _series()
        host = _host(_Adapter(candles))
        campaign = asyncio.run(host.start_named_mother(candles[26].timestamp, now=datetime(2026, 3, 3, 11, 0)))
        campaign.last_result = None

        asyncio.run(host.ensure_drawable(campaign, now=datetime(2026, 3, 3, 20, 30)))
        self.assertEqual(campaign.fills, [])
        self.assertEqual(campaign.unpriced, 0)
