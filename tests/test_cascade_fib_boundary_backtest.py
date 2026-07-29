"""Real-premium backtest: the batch engine priced by a stub Upstox lookup, then
flattened by the app's `_serialize_fib_backtest` into the shape the UI reads.

The live paper engine withholds P&L for an old mother because Dhan only quotes
'now'.  The backtest instead prices every leg off a historical premium source,
so these tests prove the serialized result carries real gross/cost/net P&L, the
fixed contract, and -- critically -- stays gap-honest: a missing premium bar is
a recorded gap and a withheld (None) net P&L, never a fabricated zero.
"""

import base64
import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The app import (for the serializer) needs a self-contained environment, the
# same one the route tests use.
os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-test-fib-backtest.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-test-fib-backtest-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from app import _serialize_fib_backtest  # noqa: E402
from engine.cascade_fib_boundary import FibBoundaryCascade, FibBoundaryConfig  # noqa: E402
from engine.cascade_options import Candle, NiftyContractResolver, OptionCandle  # noqa: E402

# 08-04 is this week's Tuesday (skipped); 08-11 is next week's, the one chosen.
EXPIRIES = [date(2026, 8, 4), date(2026, 8, 11)]


def _c(hh, mm, o, h, low, c):
    return Candle(datetime(2026, 7, 29, hh, mm), o, h, low, c)


class _PremiumBook:
    """(hour, minute) -> premium as an OptionCandle open, or None for a gap."""

    def __init__(self, table):
        self.table = dict(table)

    def __call__(self, timestamp, _contract):
        value = self.table.get((timestamp.hour, timestamp.minute))
        return None if value is None else OptionCandle(timestamp, value, value, value, value)


def _ce_config():
    return FibBoundaryConfig(
        datetime(2026, 7, 29, 9, 10),
        24180.0,
        24050.0,
        option_type="CE",
        timeframe="5m",
        rung_inr=75_000.0,
        lot_size=75,
        strict_option_data=False,  # match the backtest route: survey all gaps
    )


def _ce_candles():
    return [
        _c(9, 15, 24040, 24050, 23690, 23700),  # red, above L4 -> streak 1
        _c(9, 20, 23700, 23710, 23600, 23640),  # red, close <= L4 (23660) -> arm L4
        _c(9, 25, 23640, 23680, 23630, 23670),  # green, high>=trigger -> FILL L4
        _c(9, 30, 23670, 23675, 23380, 23400),  # red, above L8 -> streak 1
        _c(9, 35, 23400, 23410, 23050, 23100),  # red, close <= L8 (23140) -> arm L8
        _c(9, 40, 23100, 23160, 23090, 23150),  # green, high>=trigger -> FILL L8
        _c(9, 45, 23150, 23560, 23150, 23540),  # green, high>=target -> EXIT
    ]


class FibBoundaryBacktestSerializerTest(unittest.TestCase):
    def _run(self, premiums):
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=75)
        result = FibBoundaryCascade(_ce_config(), resolver, premiums).run(_ce_candles())
        first = result.entries[0].contract if result.entries else None
        contract = (
            None
            if first is None
            else {
                "underlying": "NIFTY",
                "strike": first.strike,
                "option_type": first.option_type,
                "expiry": first.expiry.isoformat(),
                "lot_size": 75,
            }
        )
        return _serialize_fib_backtest(result, contract=contract)

    def test_fully_priced_run_carries_real_net_pnl(self):
        payload = self._run(_PremiumBook({(9, 25): 150.0, (9, 40): 90.0, (9, 45): 210.0}))
        self.assertEqual(payload["status"], "closed")
        self.assertEqual(payload["exit_reason"], "target")
        self.assertTrue(payload["fully_priced"])
        # Two deep legs, priced with real premiums, at one fixed strike/expiry.
        self.assertEqual([e["level"] for e in payload["entries"]], [4, 8])
        self.assertEqual({e["strike"] for e in payload["entries"]}, {payload["contract"]["strike"]})
        self.assertEqual({e["expiry"] for e in payload["entries"]}, {"2026-08-11"})
        self.assertTrue(all(e["option_price"] is not None for e in payload["entries"]))
        # Bought cheap-and-deep, sold on the snap: net positive, and strictly
        # below gross because costs were charged.
        self.assertIsNotNone(payload["net_pnl"])
        self.assertGreater(payload["net_pnl"], 0)
        self.assertGreater(payload["gross_pnl"], payload["net_pnl"])
        self.assertGreater(payload["costs_total"], 0)
        self.assertEqual(payload["data_gaps"], [])

    def test_missing_premium_is_a_gap_not_a_fabricated_zero(self):
        # The L8 fill minute has no premium bar -> the leg is a recorded gap and
        # the P&L is withheld (None), never silently priced at zero.
        payload = self._run(_PremiumBook({(9, 25): 150.0, (9, 45): 210.0}))
        self.assertFalse(payload["fully_priced"])
        self.assertIsNone(payload["net_pnl"])
        self.assertTrue(payload["data_gaps"])
        self.assertTrue(any(e["option_price"] is None for e in payload["entries"]))

    def test_no_entries_serializes_cleanly(self):
        # Price never falls to L4: nothing arms, nothing fills, no contract.
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=75)
        flat = [_c(9, 15 + i, 24100, 24120, 24080, 24110) for i in range(0, 10, 5)]
        result = FibBoundaryCascade(_ce_config(), resolver, _PremiumBook({})).run(flat)
        payload = _serialize_fib_backtest(result, contract=None)
        self.assertEqual(payload["entries"], [])
        self.assertIsNone(payload["contract"])
        self.assertIsNone(payload["net_pnl"])


if __name__ == "__main__":
    unittest.main()
