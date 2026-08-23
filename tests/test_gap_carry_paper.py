"""Gap Carry as a campaign: the states, the clocks, and the restart.

The rule itself is pinned in test_gap_carry_engine.py. What is pinned here is
everything the page depends on around it -- that a campaign reaches the right
status, that it survives a restart with its open leg intact, that the exit is
taken late rather than skipped, and that a kill demands a real quote instead of
quietly flooring the position at intrinsic.
"""

import sys
import unittest
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.gap_carry import GapCarryConfig  # noqa: E402
from engine.gap_carry_paper import (  # noqa: E402
    CLOSED,
    EXPIRED,
    HOLDING,
    KILLED,
    TERMINAL,
    WAITING,
    GapCarryPaper,
    default_expiry_lookup,
)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


SESSION = date(2026, 3, 10)
NEXT = date(2026, 3, 11)
EXPIRY = date(2026, 3, 17)


def _day(day: date, closes: list, start=time(9, 15), step=5) -> list:
    base = datetime.combine(day, start)
    return [Candle(base + timedelta(minutes=step * i), c, c, c, c) for i, c in enumerate(closes)]


RISING = [24000.0 + i for i in range(80)]
FALLING = [24500.0 - i for i in range(80)]


def _engine(*, prices=None, expiry=EXPIRY, lot=75, **cfg) -> GapCarryPaper:
    config = GapCarryConfig(timeframe="5m", lots=cfg.pop("lots", 1), **cfg)
    priced = prices if prices is not None else (lambda *_a: 300.0)
    return GapCarryPaper(
        config=config,
        option_premium_lookup=priced if callable(priced) else (lambda *_a: priced),
        expiry_lookup=lambda _s: expiry,
        lot_size_lookup=lambda _e: lot,
    )


class EntryTests(unittest.TestCase):
    def test_a_qualifying_close_buys_and_holds(self):
        eng = _engine(lots=2)
        eng.ingest({"5m": _day(SESSION, RISING)})
        self.assertEqual(eng.status, HOLDING)
        self.assertTrue(eng.has_open_position)
        pos = eng.position
        self.assertEqual(pos.side, "CE")
        self.assertEqual(pos.lots, 2)
        self.assertEqual(pos.quantity, 150)
        self.assertLess(pos.strike, pos.entry_spot, "a call must be bought BELOW spot to be in the money")

    def test_a_falling_close_buys_a_put_above_spot(self):
        eng = _engine()
        eng.ingest({"5m": _day(SESSION, FALLING)})
        self.assertEqual(eng.position.side, "PE")
        self.assertGreater(eng.position.strike, eng.position.entry_spot)

    def test_a_flat_tape_buys_nothing_and_says_why(self):
        eng = _engine()
        eng.ingest({"5m": _day(SESSION, [24000.0 + (0.2 if i % 2 else -0.2) for i in range(80)])})
        self.assertEqual(eng.status, WAITING)
        self.assertIsNone(eng.position)
        self.assertTrue(eng.notes, "a refusal must be recorded, not silent")

    def test_candles_before_the_entry_clock_do_not_decide(self):
        eng = _engine()
        eng.ingest({"5m": _day(SESSION, RISING)[:20]})  # ends ~10:55
        self.assertEqual(eng.status, WAITING)
        self.assertIsNone(eng.position)

    def test_a_second_ingest_the_same_session_does_not_buy_twice(self):
        eng = _engine()
        rows = _day(SESSION, RISING)
        eng.ingest({"5m": rows})
        eng.ingest({"5m": rows})
        self.assertEqual(len(eng.history), 0)
        self.assertTrue(eng.has_open_position)

    def test_an_unpriceable_contract_buys_nothing_rather_than_guessing(self):
        eng = _engine(prices=lambda *_a: None)
        eng.ingest({"5m": _day(SESSION, RISING)})
        self.assertEqual(eng.status, WAITING)
        self.assertIsNone(eng.position)
        self.assertTrue(any("nothing bought" in n for n in eng.notes))

    def test_no_surviving_expiry_buys_nothing(self):
        eng = _engine(expiry=None)
        eng.ingest({"5m": _day(SESSION, RISING)})
        self.assertEqual(eng.status, WAITING)
        self.assertTrue(any("survives to the exit" in n for n in eng.notes))

    def test_another_charts_candles_are_ignored_not_blended(self):
        eng = _engine()
        eng.ingest({"15m": _day(SESSION, RISING)})
        self.assertEqual(eng.status, WAITING)


class ExitTests(unittest.TestCase):
    def _held(self, **kw):
        eng = _engine(**kw)
        eng.ingest({"5m": _day(SESSION, RISING)})
        self.assertTrue(eng.has_open_position)
        return eng

    def test_the_exit_clock_closes_the_position(self):
        eng = self._held()
        eng.option_premium_lookup = lambda *_a: 420.0
        eng.mark(datetime.combine(NEXT, time(9, 20)))
        self.assertEqual(eng.status, CLOSED)
        self.assertFalse(eng.has_open_position)
        self.assertEqual(len(eng.history), 1)
        self.assertTrue(eng.history[0].exit_priced)

    def test_a_late_restart_still_takes_the_missed_exit(self):
        """09:20 came and went while the process was down. Coming back at 09:35
        must sell, not carry an unplanned second night."""
        eng = self._held()
        eng.option_premium_lookup = lambda *_a: 410.0
        eng.mark(datetime.combine(NEXT, time(9, 35)))
        self.assertEqual(eng.status, CLOSED)

    def test_before_the_exit_clock_it_only_marks(self):
        eng = self._held()
        eng.option_premium_lookup = lambda *_a: 355.0
        mark = eng.mark(datetime.combine(SESSION, time(15, 20)))
        self.assertEqual(eng.status, HOLDING)
        self.assertTrue(eng.has_open_position)
        self.assertIsNotNone(mark)
        self.assertIn("unrealised", mark)

    def test_the_same_session_never_counts_as_the_next_one(self):
        eng = self._held()
        eng.option_premium_lookup = lambda *_a: 500.0
        eng.mark(datetime.combine(SESSION, time(9, 20)))
        self.assertEqual(eng.status, HOLDING, "09:20 on the ENTRY day is not the exit")

    def test_an_unquotable_exit_leaves_the_position_open(self):
        eng = self._held()
        eng.option_premium_lookup = lambda *_a: None
        eng.mark(datetime.combine(NEXT, time(9, 20)))
        self.assertEqual(eng.status, HOLDING, "a missing quote must not close a position")

    def test_a_contract_that_settled_first_ends_at_intrinsic_and_is_flagged(self):
        eng = self._held()
        eng.last_index_close = 24500.0
        closed = eng.settle_past_expiry(datetime.combine(EXPIRY + timedelta(days=1), time(9, 20)))
        self.assertTrue(closed)
        self.assertEqual(eng.status, EXPIRED)
        row = eng.history[0]
        self.assertFalse(row.exit_priced, "a floor is not a price and must say so")
        self.assertEqual(row.exit_reason, "EXPIRED_AT_INTRINSIC")

    def test_settle_does_nothing_before_the_expiry(self):
        eng = self._held()
        self.assertFalse(eng.settle_past_expiry(datetime.combine(NEXT, time(9, 20))))
        self.assertEqual(eng.status, HOLDING)


class KillTests(unittest.TestCase):
    def _held(self):
        eng = _engine()
        eng.ingest({"5m": _day(SESSION, RISING)})
        return eng

    def test_a_kill_needs_a_real_quote(self):
        eng = self._held()
        self.assertFalse(eng.kill_and_close(0.0))
        self.assertTrue(eng.has_open_position, "a live kill is never floored at intrinsic")
        self.assertFalse(eng.kill_and_close(None))
        self.assertTrue(eng.has_open_position)

    def test_a_kill_at_a_quote_closes_and_books_charges(self):
        eng = self._held()
        self.assertTrue(eng.kill_and_close(360.0))
        self.assertEqual(eng.status, KILLED)
        row = eng.history[0]
        self.assertTrue(row.exit_priced)
        self.assertGreaterEqual(row.charges, 0.0)
        self.assertIsNotNone(row.net)

    def test_killing_an_idle_campaign_is_terminal_not_an_error(self):
        eng = _engine()
        self.assertFalse(eng.kill_and_close(100.0))
        self.assertIn(eng.status, TERMINAL)


class PersistenceTests(unittest.TestCase):
    def test_an_open_position_survives_a_restart(self):
        eng = _engine(lots=3)
        eng.ingest({"5m": _day(SESSION, RISING)})
        eng.mark(datetime.combine(SESSION, time(15, 25)))
        restored = GapCarryPaper.from_dict(
            eng.to_dict(),
            option_premium_lookup=lambda *_a: 420.0,
            expiry_lookup=lambda _s: EXPIRY,
            lot_size_lookup=lambda _e: 75,
        )
        self.assertEqual(restored.status, HOLDING)
        self.assertTrue(restored.has_open_position)
        self.assertEqual(restored.position.strike, eng.position.strike)
        self.assertEqual(restored.position.side, eng.position.side)
        self.assertEqual(restored.position.lots, 3)
        self.assertEqual(restored.config.timeframe, "5m")
        self.assertEqual(restored.config.entry_time, time(15, 10))
        # and it can still be sold
        restored.mark(datetime.combine(NEXT, time(9, 20)))
        self.assertEqual(restored.status, CLOSED)

    def test_a_restored_campaign_does_not_re_enter_a_session_it_already_read(self):
        eng = _engine()
        rows = _day(SESSION, RISING)
        eng.ingest({"5m": rows})
        eng.kill_and_close(310.0)
        restored = GapCarryPaper.from_dict(
            eng.to_dict(),
            option_premium_lookup=lambda *_a: 300.0,
            expiry_lookup=lambda _s: EXPIRY,
            lot_size_lookup=lambda _e: 75,
        )
        restored.ingest({"5m": rows})
        self.assertEqual(len(restored.history), 1, "the session was already decided")

    def test_history_round_trips_with_its_floored_flag(self):
        eng = _engine()
        eng.ingest({"5m": _day(SESSION, RISING)})
        eng.last_index_close = 24600.0
        eng.settle_past_expiry(datetime.combine(EXPIRY + timedelta(days=1), time(9, 20)))
        restored = GapCarryPaper.from_dict(eng.to_dict())
        self.assertEqual(len(restored.history), 1)
        self.assertFalse(restored.history[0].exit_priced)
        self.assertEqual(restored.get_status()["floored_exits"], 1)

    def test_to_dict_is_json_safe(self):
        import json

        eng = _engine()
        eng.ingest({"5m": _day(SESSION, RISING)})
        json.dumps(eng.to_dict())  # must not raise on date/datetime


class StatusTests(unittest.TestCase):
    def test_status_reports_the_mirror_and_the_lots(self):
        eng = _engine(lots=2, rsi_threshold=72.0)
        st = eng.get_status()
        self.assertEqual(st["rule"]["rsi_for_call"], 72.0)
        self.assertEqual(st["rule"]["rsi_for_put"], 28.0)
        self.assertEqual(st["rule"]["lots"], 2)
        self.assertEqual(st["rule"]["entry_time"], "15:10")
        self.assertEqual(st["rule"]["exit_time"], "09:20")

    def test_stages_is_the_one_chart_the_loop_must_fetch(self):
        self.assertEqual(_engine().stages, ("5m",))

    def test_floored_exits_are_reported_separately_from_the_rest(self):
        eng = _engine()
        eng.ingest({"5m": _day(SESSION, RISING)})
        eng.last_index_close = 24600.0
        eng.settle_past_expiry(datetime.combine(EXPIRY + timedelta(days=1), time(9, 20)))
        st = eng.get_status()
        self.assertEqual(st["floored_exits"], 1)
        self.assertEqual(st["closed_trades"], 1)
        self.assertAlmostEqual(st["floored_net"], st["realised"])


class ExpiryLookupTests(unittest.TestCase):
    def test_an_expiry_settling_tonight_is_refused(self):
        lookup = default_expiry_lookup([SESSION, EXPIRY])
        self.assertEqual(lookup(SESSION), EXPIRY, "tonight's expiry cannot carry an overnight position")

    def test_nothing_far_enough_out_returns_none(self):
        self.assertIsNone(default_expiry_lookup([SESSION])(SESSION))


if __name__ == "__main__":
    unittest.main()
