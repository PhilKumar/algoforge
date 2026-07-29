"""End-to-end tests for the manual-mother fib-boundary Cascade engine."""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_fib_boundary import FibBoundaryCascade, FibBoundaryConfig  # noqa: E402
from engine.cascade_options import Candle, CascadeError, NiftyContractResolver, OptionCandle  # noqa: E402

# Next-weekly expiries around a 2026-07-29 (Wednesday) mother: 08-04 is this
# week's Tuesday (6 DTE, skipped), 08-11 is next week's (13 DTE, chosen).
EXPIRIES = [date(2026, 8, 4), date(2026, 8, 11)]


def _c(hh, mm, o, h, low, c):
    return Candle(datetime(2026, 7, 29, hh, mm), o, h, low, c)


class _PremiumBook:
    """timestamp-minute -> premium, returned as an OptionCandle open."""

    def __init__(self, table):
        self.table = {k: v for k, v in table.items()}

    def __call__(self, timestamp, contract):
        value = self.table.get((timestamp.hour, timestamp.minute))
        if value is None:
            return None
        return OptionCandle(timestamp, value, value, value, value)


class FibBoundaryGeometryTest(unittest.TestCase):
    def test_ce_boundaries_are_deep_below_the_mother(self):
        cfg = FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, option_type="CE")
        self.assertAlmostEqual(cfg.boundary_price(4), 24180.0 - 4 * 130.0)  # 23660
        self.assertAlmostEqual(cfg.boundary_price(8), 24180.0 - 8 * 130.0)  # 23140
        self.assertEqual(cfg.ordered_boundaries(), [4, 8])

    def test_pe_boundaries_mirror_above_the_low(self):
        cfg = FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, option_type="PE")
        self.assertAlmostEqual(cfg.boundary_price(4), 24050.0 + 4 * 130.0)  # 24570
        self.assertAlmostEqual(cfg.boundary_price(8), 24050.0 + 8 * 130.0)  # 25090

    def test_boundaries_follow_the_timeframe(self):
        # 1m and 5m trade only the two deepest lines.
        for tf in ("1m", "5m"):
            cfg = FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, timeframe=tf)
            self.assertEqual(cfg.ordered_boundaries(), [4, 8])
        # 15m and above add level 2, so the ladder starts one step earlier.
        for tf in ("15m", "1h"):
            cfg = FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, timeframe=tf)
            self.assertEqual(cfg.ordered_boundaries(), [2, 4, 8])

    def test_explicit_boundaries_override_the_timeframe(self):
        cfg = FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, timeframe="1h", boundaries=(4, 8))
        self.assertEqual(cfg.ordered_boundaries(), [4, 8])

    def test_config_rejects_bad_input(self):
        with self.assertRaises(CascadeError):
            FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24050.0, 24180.0)  # high <= low
        with self.assertRaises(CascadeError):
            FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, option_type="XX")
        with self.assertRaises(CascadeError):
            FibBoundaryConfig(datetime(2026, 7, 29, 9, 10), 24180.0, 24050.0, timeframe="2h")


class FibBoundaryCeCampaignTest(unittest.TestCase):
    def _run(self, strict=True):
        cfg = FibBoundaryConfig(
            datetime(2026, 7, 29, 9, 10),
            24180.0,
            24050.0,
            option_type="CE",
            timeframe="5m",
            rung_inr=75_000.0,
            strict_option_data=strict,
        )
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=65)
        premiums = _PremiumBook({(9, 25): 150.0, (9, 40): 90.0, (9, 45): 210.0})
        candles = [
            _c(9, 15, 24040, 24050, 23690, 23700),  # red, close above L4 -> streak 1
            _c(9, 20, 23700, 23710, 23600, 23640),  # red, close <= L4 (23660) -> arm L4 @ 23640
            _c(9, 25, 23640, 23680, 23630, 23670),  # green, high>=trigger -> FILL L4
            _c(9, 30, 23670, 23675, 23380, 23400),  # red, above L8 -> streak 1
            _c(9, 35, 23400, 23410, 23050, 23100),  # red, close <= L8 (23140) -> arm L8 @ 23100
            _c(9, 40, 23100, 23160, 23090, 23150),  # green, high>=trigger -> FILL L8
            _c(9, 45, 23150, 23560, 23150, 23540),  # green, high>=target -> EXIT
        ]
        return FibBoundaryCascade(cfg, resolver, premiums).run(candles)

    def test_fills_both_boundaries_then_targets_out_net_positive(self):
        result = self._run()
        self.assertEqual(result.status, "closed")
        self.assertEqual(result.exit_reason, "target")
        self.assertEqual([e.stage for e in result.entries], [4, 8])  # level 4 then level 8
        # Fixed contract: ATM-2 CE on the next-weekly expiry, same for both legs.
        strikes = {e.contract.strike for e in result.entries}
        expiries = {e.contract.expiry for e in result.entries}
        self.assertEqual(expiries, {date(2026, 8, 11)})
        self.assertEqual(len(strikes), 1)
        self.assertTrue(result.fully_priced)
        self.assertIsNotNone(result.net_pnl)
        self.assertGreater(result.net_pnl, 0)  # bought cheap deep, sold on the snap
        self.assertLess(result.net_pnl, result.realized_pnl)  # costs were charged

    def test_no_new_low_reuse_campaign_ends_on_first_target(self):
        result = self._run()
        # Both rungs filled exactly once; nothing re-armed after the target.
        fills = [e for e in result.events if e["event"] == "fill"]
        self.assertEqual(len(fills), 2)
        self.assertEqual(result.exit_timestamp, datetime(2026, 7, 29, 9, 45))


if __name__ == "__main__":
    unittest.main()
