"""Gap Carry: the rule, pinned.

The strategy was measured in a separate harness before it was built here, and
this file exists so the engine cannot quietly drift away from what was
measured. The parity itself was checked against that harness on the full
2021-2026 NIFTY history and reproduces to the rupee -- 179 trades, Rs 2,11,624,
57.0% won, PF 1.86, max drawdown Rs 32,323, four exits floored at intrinsic.
That check needs the option archives, so it cannot run in CI; what runs here is
every rule the walk depends on, on candles this file makes itself.
"""

import importlib.util
import sys
import unittest
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gap_carry", Path(__file__).resolve().parents[1] / "engine" / "gap_carry.py"
)
gap_carry = importlib.util.module_from_spec(_spec)
sys.modules["gap_carry"] = gap_carry
_spec.loader.exec_module(gap_carry)

CE, PE = gap_carry.CE, gap_carry.PE


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    close: float


def _ramp(day: date, closes: list, start=time(9, 15), step=5) -> list:
    base = datetime.combine(day, start)
    return [Bar(base + timedelta(minutes=step * i), c) for i, c in enumerate(closes)]


class StrikeDirectionTests(unittest.TestCase):
    """In the money is a LOWER strike for a call and a HIGHER one for a put."""

    def test_call_goes_down_and_put_goes_up(self):
        cfg = gap_carry.GapCarryConfig(strike_offset_steps=4, strike_step=50)
        self.assertEqual(gap_carry.strike_for(24580, CE, cfg), 24400)
        self.assertEqual(gap_carry.strike_for(24580, PE, cfg), 24800)

    def test_atm_offset_zero_is_the_round_strike(self):
        cfg = gap_carry.GapCarryConfig(strike_offset_steps=0)
        self.assertEqual(gap_carry.strike_for(24580, CE, cfg), 24600)


class SignalTests(unittest.TestCase):
    DAY = date(2026, 3, 10)

    def test_a_rising_close_above_the_ema_buys_a_call(self):
        closes = [100.0 + i for i in range(80)]  # relentless up: RSI pins near 100
        sig = gap_carry.read_signal(
            _ramp(self.DAY, closes), gap_carry.GapCarryConfig(), at=datetime.combine(self.DAY, time(15, 10))
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, CE)
        self.assertGreater(sig.close, sig.ema)

    def test_a_falling_close_below_the_ema_buys_a_put(self):
        closes = [200.0 - i for i in range(80)]
        sig = gap_carry.read_signal(
            _ramp(self.DAY, closes), gap_carry.GapCarryConfig(), at=datetime.combine(self.DAY, time(15, 10))
        )
        self.assertEqual(sig.side, PE)
        self.assertLess(sig.close, sig.ema)

    def test_a_flat_tape_asks_for_nothing(self):
        closes = [100.0 + (0.2 if i % 2 else -0.2) for i in range(80)]
        sig = gap_carry.read_signal(
            _ramp(self.DAY, closes), gap_carry.GapCarryConfig(), at=datetime.combine(self.DAY, time(15, 10))
        )
        self.assertIsNotNone(sig)
        self.assertIsNone(sig.side)
        self.assertIn("has not reached", sig.reason)

    def test_the_threshold_is_read_as_a_mirror(self):
        cfg = gap_carry.GapCarryConfig(rsi_threshold=70.0)
        self.assertEqual(cfg.rsi_floor_for_call, 70.0)
        self.assertEqual(cfg.rsi_ceiling_for_put, 30.0)

    def test_bars_after_the_entry_time_are_not_read(self):
        """The 15:10 decision cannot see 15:25. A backtest that let it would be
        trading on a candle the live loop has not got yet."""
        # 09:15 + 5m steps: index 71 IS 15:10, so the spike has to start at 72
        # or the test is asserting against a bar the rule may legitimately see.
        closes = [100.0 - i for i in range(72)] + [500.0] * 8  # a violent LATE spike
        bars = _ramp(self.DAY, closes)
        sig = gap_carry.read_signal(
            bars, gap_carry.GapCarryConfig(entry_time=time(15, 10)), at=datetime.combine(self.DAY, time(15, 10))
        )
        self.assertEqual(sig.side, PE, "the late spike must not be visible at 15:10")
        self.assertLessEqual(sig.timestamp.time(), time(15, 10))


class ReplayTests(unittest.TestCase):
    """The walk: one position a night, and every refusal accounted for."""

    DAYS = [date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11)]

    def _walk(self, *, price_at, expiry_for=None, **kw):
        skips = []
        closes = [100.0 + i for i in range(80)]
        cfg = gap_carry.GapCarryConfig(lots=2, **kw)
        pos = gap_carry.replay(
            self.DAYS,
            config=cfg,
            candles_for=lambda d: _ramp(d, closes),
            spot_at=lambda ts: 24580.0,
            price_at=price_at,
            expiry_for=expiry_for or (lambda d: date(2026, 3, 17)),
            lot_size_for=lambda e: 65,
            charges_for=lambda d, a, b, q: 40.0,
            on_skip=lambda d, why: skips.append((d, why)),
        )
        return pos, skips

    def test_a_clean_night_books_one_position(self):
        prices = {True: 300.0}
        pos, skips = self._walk(price_at=lambda ts, k, s, e: 300.0 if ts.time() == time(15, 10) else 420.0)
        self.assertEqual(len(pos), 2)  # three sessions -> two nights
        first = pos[0]
        self.assertEqual(first.side, CE)
        self.assertEqual(first.quantity, 130)  # 2 lots x 65
        self.assertTrue(first.exit_priced)
        self.assertAlmostEqual(first.gross, (420.0 - 300.0) * 130)
        self.assertAlmostEqual(first.net, (420.0 - 300.0) * 130 - 40.0)
        self.assertEqual(skips, [])

    def test_an_expiry_before_the_exit_is_refused_not_settled(self):
        pos, skips = self._walk(price_at=lambda *a: 300.0, expiry_for=lambda d: d)
        self.assertEqual(pos, [])
        self.assertTrue(all("expiry lands before the exit" in why for _d, why in skips))

    def test_an_unquoted_exit_is_floored_at_intrinsic_and_flagged(self):
        """This is the honest case, not the convenient one: the contract left the
        archive's coverage because the gap was large, so intrinsic UNDERSTATES it."""
        pos, _skips = self._walk(price_at=lambda ts, k, s, e: 300.0 if ts.time() == time(15, 10) else None)
        self.assertTrue(pos)
        for p in pos:
            self.assertFalse(p.exit_priced)
            self.assertEqual(p.exit_reason, "MORNING_EXIT_AT_INTRINSIC")
            self.assertAlmostEqual(p.exit_premium, p.intrinsic(24580.0))

    def test_a_worthless_unquoted_exit_is_dropped_not_zeroed(self):
        """Booking a missing tick as zero invents a total loss."""
        pos, skips = self._walk(
            price_at=lambda ts, k, s, e: 300.0 if ts.time() == time(15, 10) else None,
            strike_offset_steps=0,
        )
        # ATM call, spot == strike region: intrinsic is 0, so it must be dropped.
        for p in pos:
            self.assertGreater(p.exit_premium or 0, 0)
        if not pos:
            self.assertTrue(any("dropped, not zeroed" in why for _d, why in skips))

    def test_summary_keeps_the_floored_exits_visible(self):
        pos, _ = self._walk(price_at=lambda ts, k, s, e: 300.0 if ts.time() == time(15, 10) else 420.0)
        summary = gap_carry.summarise(pos)
        self.assertEqual(summary["trades"], 2)
        self.assertEqual(summary["floored_exits"], 0)
        self.assertIn("CE", summary["by_side"])
        self.assertEqual(summary["by_side"]["CE"]["trades"], 2)


class ConfigTests(unittest.TestCase):
    def test_a_chart_this_rule_was_not_measured_on_is_refused(self):
        with self.assertRaises(gap_carry.GapCarryError):
            gap_carry.GapCarryConfig(timeframe="1m").validate()

    def test_an_exit_after_the_close_is_refused(self):
        with self.assertRaises(gap_carry.GapCarryError):
            gap_carry.GapCarryConfig(exit_time=time(16, 0)).validate()

    def test_lots_are_bounded(self):
        with self.assertRaises(gap_carry.GapCarryError):
            gap_carry.GapCarryConfig(lots=99).validate()


if __name__ == "__main__":
    unittest.main()
