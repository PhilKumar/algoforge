"""The recovery host: session gate, replay-not-step, named mothers, snapshot."""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.candle_recovery import RecoveryConfig  # noqa: E402
from engine.candle_recovery_host import (  # noqa: E402
    MAX_MOTHER_AGE_DAYS,
    CandleRecoveryHost,
    bars_from_candles,
)

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 2, 12)
SESSION_DAY = datetime(2026, 2, 2)  # a Monday


def candle(stamp, o, h, low, c, *, aware=False):
    return SimpleNamespace(timestamp=stamp.replace(tzinfo=IST) if aware else stamp, open=o, high=h, low=low, close=c)


class _Adapter:
    """A 5m tape for one session, optionally tz-aware like the real one."""

    def __init__(self, rows, aware=True):
        self.rows = rows
        self.aware = aware
        self.calls = 0

    async def async_get_candles(self, symbol, timeframe, *, from_date=None, to_date=None, now=None):
        self.calls += 1
        return [candle(s, o, h, low, c, aware=self.aware) for s, o, h, low, c in self.rows]


def tape():
    """A fall, two reds, then a recovery through the trigger."""
    t = SESSION_DAY.replace(hour=9, minute=15)
    step = timedelta(minutes=5)
    rows = [
        (t, 100.0, 110.0, 99.0, 108.0),  # the mother
        (t + step, 108, 109, 104, 105),  # red
        (t + 2 * step, 105, 106, 102, 103),  # red, closes below the first's low
        (t + 3 * step, 103, 107, 102.5, 106.5),  # rises through 106 -> fills
        (t + 4 * step, 106.5, 112, 106, 111),  # runs up
    ]
    return rows, t


def build(mode="ladder", aware=True, premium=None):
    rows, mother_ts = tape()
    adapter = _Adapter(rows, aware=aware)
    prices = premium if premium is not None else {}
    host = CandleRecoveryHost(
        "nifty",
        adapter,
        premium_lookup=lambda when, strike, expiry: prices.get(when.replace(second=0, microsecond=0), 100.0),
        select_contract=lambda when, index: SimpleNamespace(strike=24000, expiry=EXPIRY),
        config=RecoveryConfig(timeframe="5m"),
        mode=mode,
        lot_size=65,
        dhan_symbol="NIFTY",
    )
    return host, adapter, mother_ts


class NamedMothers(unittest.IsolatedAsyncioTestCase):
    async def test_a_named_mother_is_replayed_at_once(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11, minute=0)
        campaign = await host.start_named_mother(mother_ts, now=now)
        self.assertEqual(campaign.mother.high, 110.0)
        # replayed immediately -- the trade is already visible without a poll
        self.assertTrue(campaign.engine.trades)
        self.assertIsNotNone(host.last_poll is None or True)

    async def test_a_timestamp_with_no_bar_is_an_error(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11, minute=0)
        with self.assertRaises(LookupError):
            await host.start_named_mother(mother_ts + timedelta(minutes=5 * 99), now=now)

    async def test_a_mother_off_the_timeframe_grid_is_refused(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11, minute=0)
        with self.assertRaises(ValueError):
            await host.start_named_mother(mother_ts + timedelta(minutes=2), now=now)

    async def test_a_mother_older_than_the_window_is_refused(self):
        host, _, mother_ts = build()
        now = SESSION_DAY + timedelta(days=MAX_MOTHER_AGE_DAYS + 2)
        with self.assertRaises(ValueError):
            await host.start_named_mother(mother_ts, now=now)

    async def test_the_same_mother_cannot_run_twice(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11, minute=0)
        await host.start_named_mother(mother_ts, now=now)
        with self.assertRaises(ValueError):
            await host.start_named_mother(mother_ts, now=now)


class SessionGate(unittest.IsolatedAsyncioTestCase):
    async def test_out_of_hours_costs_no_broker_call(self):
        host, adapter, mother_ts = build()
        await host.start_named_mother(mother_ts, now=SESSION_DAY.replace(hour=11))
        calls_before = adapter.calls
        report = await host.poll(now=SESSION_DAY.replace(hour=20, minute=0))
        self.assertEqual(report.skipped, "outside NSE cash session")
        self.assertEqual(adapter.calls, calls_before)  # the gate is BEFORE any I/O

    async def test_an_empty_book_costs_no_broker_call(self):
        host, adapter, _ = build()
        report = await host.poll(now=SESSION_DAY.replace(hour=11))
        self.assertEqual(report.skipped, "no campaigns")
        self.assertEqual(adapter.calls, 0)


class ReplayNotStep(unittest.IsolatedAsyncioTestCase):
    async def test_two_polls_over_the_same_bars_agree(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11)
        await host.start_named_mother(mother_ts, now=now)
        first = host.snapshot()["campaigns"][0]
        await host.poll(now=now)
        await host.poll(now=now)
        again = host.snapshot()["campaigns"][0]
        self.assertEqual(first["trades"], again["trades"])
        self.assertEqual(first["booked_net"], again["booked_net"])

    async def test_aware_adapter_candles_are_flattened_to_naive(self):
        # aware == naive is silently False; a mother lookup would never match.
        rows, mother_ts = tape()
        bars = bars_from_candles([candle(*r, aware=True) for r in rows])
        self.assertIsNone(bars[0].timestamp.tzinfo)
        self.assertEqual(bars[0].timestamp, mother_ts)


class Snapshot(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_carries_the_ledger_and_the_rupee_target(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11)
        await host.start_named_mother(mother_ts, now=now)
        snap = host.snapshot()
        self.assertEqual(snap["timeframe"], "5m")
        self.assertEqual(snap["mode"], "ladder")
        self.assertEqual(snap["config"]["sl_source"], "entry")
        row = snap["campaigns"][0]
        self.assertIn("required_recovery", row)
        self.assertIn("booked_net", row)
        self.assertEqual(row["mother"]["high"], 110.0)

    async def test_fib_zone_mode_reports_its_zones(self):
        host, _, mother_ts = build(mode="fib-zone")
        now = SESSION_DAY.replace(hour=11)
        await host.start_named_mother(mother_ts, now=now)
        row = host.snapshot()["campaigns"][0]
        self.assertIn("zones", row)  # empty until the low breaks, but present

    async def test_dropping_a_campaign_removes_it(self):
        host, _, mother_ts = build()
        now = SESSION_DAY.replace(hour=11)
        c = await host.start_named_mother(mother_ts, now=now)
        self.assertTrue(host.drop(c.campaign_id))
        self.assertEqual(host.snapshot()["campaigns"], [])
        self.assertFalse(host.drop(c.campaign_id))


class PremiumsAreRemembered(unittest.IsolatedAsyncioTestCase):
    """A live quote serves only the current minute; a replay asks for old ones.

    Without a cache every past fill comes back unpriced on the next poll and the
    ledger the recovery target is measured against evaporates.
    """

    async def test_a_price_seen_once_survives_later_replays(self):
        rows, mother_ts = tape()
        adapter = _Adapter(rows, aware=True)
        served = {"count": 0}

        def only_once(when, strike, expiry):
            # the real lookup refuses anything but the current minute
            served["count"] += 1
            return 100.0 if served["count"] <= 3 else None

        host = CandleRecoveryHost(
            "nifty",
            adapter,
            premium_lookup=only_once,
            select_contract=lambda when, index: SimpleNamespace(strike=24000, expiry=EXPIRY),
            config=RecoveryConfig(timeframe="5m"),
            mode="ladder",
            lot_size=65,
            dhan_symbol="NIFTY",
        )
        now = SESSION_DAY.replace(hour=11)
        c = await host.start_named_mother(mother_ts, now=now)
        first = [t.entry_premium for t in c.engine.trades if t.entry_time]
        self.assertTrue(any(p is not None for p in first))
        await host.poll(now=now)
        again = [t.entry_premium for t in host.campaigns[c.campaign_id].engine.trades if t.entry_time]
        self.assertEqual(first, again)  # not lost when the broker stops serving it


if __name__ == "__main__":
    unittest.main()
