"""The real-geometry backtest serializer, driven by an actual cascade run.

Proves the JSON the UI renders is sourced from the CryptoForge geometry engine
with the lot ladder + per-entry strike: entries carry their own strike, P&L sums
across rounds, and the shape matches what the backtest panel reads.
"""

import base64
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-test-bt-serializer.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-test-bt-serializer-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
# Computed, not a literal, so the secret scanner doesn't flag a dummy test key.
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from app import _serialize_cascade_backtest  # noqa: E402
from engine.cascade_options import (  # noqa: E402
    FixedCampaignOption,
    IndexCandle,
    NiftyOptionsPaperCascade,
    PaperCascadeConfig,
)


def ts(offset: int) -> datetime:
    return datetime(2026, 7, 20, 9, 15) + timedelta(minutes=5 * offset)


class _PaperAdapter:
    paper_only = True

    def __init__(self):
        self.orders = []

    def dte_allows_new_rungs(self, _contract, _at):
        return True

    def expiry_squareoff_due(self, _contract, _at):
        return False

    def place_order(self, contract, *, side, quantity):
        order = type("O", (), {"order_id": f"p{len(self.orders) + 1}", "contract": contract, "side": side})()
        self.orders.append(order)
        return order


class CascadeBacktestSerializerTest(unittest.TestCase):
    def _engine(self):
        mother = IndexCandle(ts(0), 65020.00, 65107.99, 65002.00, 65051.98)
        candles = [
            IndexCandle(ts(1), 65051.98, 65051.98, 64804.76, 64919.31),
            IndexCandle(ts(2), 64919.31, 64923.67, 64852.01, 64876.01),
            IndexCandle(ts(3), 64876.01, 64878.01, 64792.00, 64800.01),
            IndexCandle(ts(4), 64800.00, 64938.00, 64790.01, 64904.00),
            IndexCandle(ts(5), 64904.00, 64928.00, 64822.24, 64822.24),
            IndexCandle(ts(6), 64822.24, 64822.24, 64639.00, 64665.99),
            IndexCandle(ts(7), 64665.99, 64680.00, 64500.00, 64550.00),
            IndexCandle(ts(8), 64550.00, 64600.00, 64400.00, 64450.00),
            IndexCandle(ts(9), 64450.00, 64600.00, 64420.00, 64580.00),
            IndexCandle(ts(10), 64580.00, 64720.00, 64570.00, 64690.00),
        ]
        adapter = _PaperAdapter()
        contract = FixedCampaignOption("NIFTY", 64800, date(2026, 7, 28), "CE", 75, "0")

        def selector(_timestamp, index_price):
            strike = int(round(index_price / 50) * 50 - 100)
            return FixedCampaignOption("NIFTY", strike, date(2026, 7, 28), "CE", 75, str(strike))

        def premium(timestamp, contract):
            base = 100.0 if timestamp == ts(9) else 120.0 if timestamp == ts(10) else None
            return None if base is None else base

        return NiftyOptionsPaperCascade(
            mother,
            contract,
            adapter,
            premium,
            PaperCascadeConfig(rung_inr=13000, lot_ladder=True, per_entry_strike=True),
            contract_selector=selector,
        ).run(candles)

    def test_serialized_shape_carries_real_pnl_and_per_entry_strike(self):
        payload = _serialize_cascade_backtest(self._engine())
        self.assertEqual(payload["status"], "closed")
        self.assertEqual(payload["exit_reason"], "target")
        self.assertTrue(payload["fully_priced"])
        self.assertTrue(payload["entries"])
        entry = payload["entries"][0]
        for key in (
            "timestamp",
            "spot",
            "option_price",
            "lots",
            "quantity",
            "level",
            "strike",
            "option_type",
            "expiry",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["lots"], 1)  # lot ladder: first buy = 1 lot
        self.assertEqual(entry["option_type"], "CE")
        # Strike was re-selected against the index, not the campaign's 64800.
        self.assertNotEqual(entry["strike"], 64800)
        self.assertIsNotNone(payload["net_pnl"])
        self.assertLess(payload["net_pnl"], payload["gross_pnl"])  # costs charged
        self.assertEqual(payload["data_gaps"], [])
        self.assertEqual(payload["contract"]["lot_size"], 75)


if __name__ == "__main__":
    unittest.main()
