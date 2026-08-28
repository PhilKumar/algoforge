"""The running Supertrend campaign: the parts a restart or a bad quote can break.

The engine's own smoke test proved each exit fires; these pin the behaviour that
only shows up in production -- a snapshot that must survive a restart with its
money intact, a kill that must refuse to invent a price, stamps that must stay
IST on a box running UTC, and a poll that hands over the same last candle
twenty times a minute without buying the same trend twenty times.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from engine.supertrend_entry import SupertrendConfig
from engine.supertrend_paper import (
    HOLDING,
    KILLED,
    TERMINAL,
    WATCHING,
    SupertrendPaper,
    default_expiry_lookup,
)

IST = ZoneInfo("Asia/Kolkata")


class Candle:
    __slots__ = ("timestamp", "open", "high", "low", "close")

    def __init__(self, timestamp, close):
        self.timestamp = timestamp
        self.open = close
        self.high = close + 30
        self.low = close - 30
        self.close = close


def _rising(n=40, start=100.0, step=5.0):
    rows = []
    day, hour = 3, 9
    for i in range(n):
        rows.append(Candle(datetime(2026, 8, day, hour, 15, tzinfo=IST), start + i * step))
        hour += 1
        if hour > 15:
            hour, day = 9, day + 1
    return rows


def _engine(premium=200.0, expiry=date(2026, 9, 10)):
    return SupertrendPaper(
        config=SupertrendConfig(),
        option_premium_lookup=lambda when, strike, side, exp: premium,
        expiry_lookup=lambda session: expiry,
        lot_size_lookup=lambda exp: 75,
    )


class ArmTests(unittest.TestCase):
    def test_arming_buys_at_the_money_and_records_it(self):
        engine = _engine()
        engine.last_index_close = 24012.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        self.assertEqual(engine.status, HOLDING)
        self.assertEqual(engine.position.strike, 24000)
        self.assertEqual(engine.position.entry_premium, 200.0)
        self.assertTrue(any("bought" in note for note in engine.notes))

    def test_a_missing_quote_buys_nothing_and_says_so(self):
        engine = SupertrendPaper(
            config=SupertrendConfig(),
            option_premium_lookup=lambda *a: None,
            expiry_lookup=lambda session: date(2026, 9, 10),
            lot_size_lookup=lambda exp: 75,
        )
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        self.assertIsNone(engine.position)
        self.assertTrue(any("no quote" in note for note in engine.notes))

    def test_a_missing_expiry_buys_nothing_and_says_so(self):
        engine = SupertrendPaper(
            config=SupertrendConfig(),
            option_premium_lookup=lambda *a: 200.0,
            expiry_lookup=lambda session: None,
            lot_size_lookup=lambda exp: 75,
        )
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        self.assertIsNone(engine.position)
        self.assertTrue(any("no expiry" in note for note in engine.notes))


class ExitTests(unittest.TestCase):
    def _held(self, premium=200.0):
        engine = _engine(premium)
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        return engine

    def test_the_trail_arms_then_exits_on_the_give_back(self):
        engine = self._held()
        engine.last_index_close = 24150.0
        engine.mark(datetime(2026, 8, 28, 11, 15, tzinfo=IST), premium=300.0)
        self.assertEqual(engine.position.mfe, 150.0)
        self.assertEqual(engine.position.trail_level(engine.config), 24070.0)
        engine.last_index_close = 24065.0
        engine.mark(datetime(2026, 8, 28, 12, 15, tzinfo=IST), premium=260.0)
        self.assertEqual(len(engine.history), 1)
        self.assertEqual(engine.history[-1].exit_reason, "trail")
        self.assertEqual(engine.status, WATCHING, "a trail exit does not end the campaign")

    def test_the_roll_closes_and_re_enters_at_the_money(self):
        engine = self._held()
        engine.last_index_close = 24300.0
        engine.mark(datetime(2026, 8, 28, 11, 15, tzinfo=IST), premium=380.0)
        self.assertEqual(engine.history[-1].exit_reason, "roll")
        self.assertEqual(engine.rolls, 1)
        self.assertIsNotNone(engine.position, "a roll re-enters; it is not an exit from the trend")
        self.assertEqual(engine.position.strike, 24300)
        self.assertEqual(engine.position.rolled_from, 24000)

    def test_a_roll_without_a_quote_holds_rather_than_guessing(self):
        engine = SupertrendPaper(
            config=SupertrendConfig(),
            option_premium_lookup=lambda *a: None,
            expiry_lookup=lambda session: date(2026, 9, 10),
            lot_size_lookup=lambda exp: 75,
        )
        engine.last_index_close = 24000.0
        engine.position = None
        engine.option_premium_lookup = lambda *a: 200.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        engine.option_premium_lookup = lambda *a: None
        engine.last_index_close = 24300.0
        engine.mark(datetime(2026, 8, 28, 11, 15, tzinfo=IST), premium=None)
        self.assertIsNotNone(engine.position, "no quote means hold, never a guessed roll")
        self.assertTrue(any("roll due but no quote" in note for note in engine.notes))

    def test_past_expiry_settles_at_intrinsic_and_flags_it(self):
        engine = self._held()
        engine.last_index_close = 24500.0
        self.assertTrue(engine.settle_past_expiry(datetime(2026, 9, 11, 10, 0, tzinfo=IST)))
        settled = engine.history[-1]
        self.assertFalse(settled.exit_priced, "intrinsic is a floor, never a price")
        self.assertEqual(settled.exit_premium, 500.0)

    def test_kill_refuses_without_a_real_quote(self):
        engine = self._held()
        self.assertFalse(engine.kill_and_close(0))
        self.assertTrue(engine.has_open_position, "refusing is safer than inventing an exit price")
        self.assertTrue(engine.kill_and_close(250.0))
        self.assertEqual(engine.status, KILLED)
        self.assertIn(engine.status, TERMINAL)

    def test_a_bearish_bar_closes_the_position(self):
        engine = self._held()
        falling = [Candle(datetime(2026, 8, 28, 10 + i % 6, 15, tzinfo=IST), 24000 - i * 60) for i in range(40)]
        engine.ingest({"1h": falling})
        self.assertEqual(len(engine.history), 1)
        self.assertEqual(engine.history[-1].exit_reason, "flip")


class IngestTests(unittest.TestCase):
    def test_the_same_bar_is_never_acted_on_twice(self):
        """The poll hands over the same last candle every 20 seconds."""
        engine = _engine()
        rows = _rising()
        engine.ingest({"1h": rows})
        first = len(engine.history) + (1 if engine.position else 0)
        for _ in range(5):
            engine.ingest({"1h": rows})
        after = len(engine.history) + (1 if engine.position else 0)
        self.assertEqual(first, after, "one bar, one decision")

    def test_another_timeframe_is_ignored_rather_than_blended(self):
        engine = _engine()
        engine.ingest({"15m": _rising()})
        self.assertIsNone(engine.position)

    def test_a_terminal_campaign_stops_listening(self):
        engine = _engine()
        engine._status = KILLED
        engine.ingest({"1h": _rising()})
        self.assertIsNone(engine.position)


class SnapshotTests(unittest.TestCase):
    def test_a_restart_keeps_the_money_and_the_contract(self):
        engine = _engine()
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        engine.last_index_close = 24150.0
        engine.mark(datetime(2026, 8, 28, 11, 15, tzinfo=IST), premium=300.0)
        engine.last_index_close = 24065.0
        engine.mark(datetime(2026, 8, 28, 12, 15, tzinfo=IST), premium=260.0)

        restored = SupertrendPaper.from_dict(engine.to_dict())
        self.assertEqual(len(restored.history), len(engine.history))
        self.assertEqual(restored.history[-1].net, engine.history[-1].net)
        self.assertEqual(restored.history[-1].exit_reason, engine.history[-1].exit_reason)
        self.assertEqual(restored.status, engine.status)
        self.assertEqual(restored.config.as_dict(), engine.config.as_dict())
        self.assertEqual(restored.rolls, engine.rolls)

    def test_an_open_position_survives_with_its_high_water_mark(self):
        engine = _engine()
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        engine.position.mfe = 220.0
        restored = SupertrendPaper.from_dict(engine.to_dict())
        self.assertTrue(restored.has_open_position)
        self.assertEqual(restored.position.mfe, 220.0, "losing the peak would re-arm the trail wrongly")
        self.assertEqual(restored.position.trail_level(restored.config), 24140.0)

    def test_a_config_the_snapshot_does_not_carry_falls_back_to_the_measured_rule(self):
        restored = SupertrendPaper.from_dict({"status": WATCHING, "config": {}})
        self.assertEqual(restored.config.as_dict(), SupertrendConfig().as_dict())


class StampTests(unittest.TestCase):
    def test_a_naive_stamp_is_read_as_ist_not_utc(self):
        """Production runs on a UTC box. A naive datetime read as UTC lands a
        day out, and the campaign settles against the wrong session."""
        engine = _engine()
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15), None)
        self.assertEqual(engine.position.entry_timestamp.tzinfo, IST)
        self.assertEqual(engine.position.entry_timestamp.hour, 10)

    def test_every_stored_stamp_comes_back_aware(self):
        engine = _engine()
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        restored = SupertrendPaper.from_dict(engine.to_dict())
        self.assertIsNotNone(restored.position.entry_timestamp.tzinfo)
        self.assertEqual(
            restored.position.entry_timestamp.astimezone(timezone.utc),
            engine.position.entry_timestamp.astimezone(timezone.utc),
        )


class ExpiryLookupTests(unittest.TestCase):
    def test_rank_two_takes_the_week_after_the_nearest(self):
        expiries = [date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 17)]
        lookup = default_expiry_lookup(expiries, rank=2)
        self.assertEqual(lookup(date(2026, 9, 1)), date(2026, 9, 10))

    def test_rank_one_is_the_nearest(self):
        expiries = [date(2026, 9, 3), date(2026, 9, 10)]
        self.assertEqual(default_expiry_lookup(expiries, rank=1)(date(2026, 9, 1)), date(2026, 9, 3))

    def test_running_out_of_expiries_returns_none_rather_than_the_wrong_one(self):
        lookup = default_expiry_lookup([date(2026, 9, 3)], rank=2)
        self.assertIsNone(lookup(date(2026, 9, 1)))


class StatusTests(unittest.TestCase):
    def test_the_panel_payload_carries_the_priced_split(self):
        engine = _engine()
        engine.last_index_close = 24000.0
        engine._arm(datetime(2026, 8, 28, 10, 15, tzinfo=IST), None)
        status = engine.get_status()
        for key in ("realised", "priced_net", "floored_exits", "rolls", "rule", "position", "notes", "history"):
            self.assertIn(key, status)
        self.assertEqual(status["strategy"], "supertrend")
        self.assertEqual(status["rule"]["multiplier"], 1.5)


if __name__ == "__main__":
    unittest.main()
