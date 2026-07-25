import unittest
from datetime import date, datetime, timedelta

from engine.cascade_options import (
    CascadeOptionsAdapter,
    IndexCandle,
    NiftyIndexCascadeGeometry,
    PaperOnlyViolation,
)


def ts(offset: int) -> datetime:
    return datetime(2026, 7, 20, 9, 15) + timedelta(minutes=5 * offset)


class _ScripMaster:
    @classmethod
    def get_expiries(cls, symbol):
        assert symbol == "NIFTY"
        return ["2026-07-21", "2026-07-28", "2026-08-04"]

    @classmethod
    def get_lot_size(cls, symbol, expiry):
        assert (symbol, expiry) == ("NIFTY", "2026-07-28")
        return 65

    @classmethod
    def lookup(cls, symbol, strike, expiry, option_type):
        assert (symbol, strike, expiry, option_type) == ("NIFTY", 24800, "2026-07-28", "CE")
        return "123456"


class _Dhan:
    def get_funds(self):
        return {"availableBalance": 100000}


class IndexGeometryPortTests(unittest.TestCase):
    def test_first_verified_cryptoforge_fib_is_preserved(self):
        # The first verified CryptoForge fixture, only translated from epoch
        # timestamps into datetimes.  These anchors must remain exact as this
        # option engine never receives premium data in its geometry layer.
        mother = IndexCandle(ts(0), 65020.00, 65107.99, 65002.00, 65051.98)
        candles = [
            IndexCandle(ts(1), 65051.98, 65051.98, 64804.76, 64919.31),
            IndexCandle(ts(2), 64919.31, 64923.67, 64852.01, 64876.01),
            IndexCandle(ts(3), 64876.01, 64878.01, 64792.00, 64800.01),
            IndexCandle(ts(4), 64800.00, 64938.00, 64790.01, 64904.00),
            IndexCandle(ts(5), 64904.00, 64928.00, 64822.24, 64822.24),
            IndexCandle(ts(6), 64822.24, 64822.24, 64639.00, 64665.99),
        ]
        engine = NiftyIndexCascadeGeometry(mother)
        campaign = engine.feed(candles)
        self.assertEqual(len(campaign.legs), 1)
        leg = campaign.legs[0]
        self.assertAlmostEqual(leg.touch_high, 64928.00)
        self.assertAlmostEqual(leg.low, 64790.01)
        self.assertAlmostEqual(leg.fib.level_price(2), 64652.02, places=2)
        self.assertAlmostEqual(leg.fib.level_price(4), 64376.04, places=2)
        self.assertAlmostEqual(leg.fib.level_price(8), 63824.08, places=2)
        self.assertAlmostEqual(campaign.trendlines[0].anchor2_price, 64904.00)

    def test_mother_break_is_strictly_above_not_equal(self):
        mother = IndexCandle(ts(0), 100, 110, 90, 100)
        engine = NiftyIndexCascadeGeometry(mother)
        engine.on_candle(IndexCandle(ts(1), 100, 110, 109.5, 98))
        self.assertNotEqual(engine.campaign.state, "MOTHER_BROKEN")
        engine.on_candle(IndexCandle(ts(2), 98, 110.01, 96, 97))
        self.assertEqual(engine.campaign.state, "MOTHER_BROKEN")


class PaperAdapterTests(unittest.TestCase):
    def test_phase_one_cannot_be_created_live(self):
        with self.assertRaises(PaperOnlyViolation):
            CascadeOptionsAdapter(_Dhan(), scrip_master=_ScripMaster, paper_only=False)

    def test_campaign_contract_is_fixed_next_week_ce_and_paper_order_never_calls_dhan(self):
        adapter = CascadeOptionsAdapter(_Dhan(), scrip_master=_ScripMaster)
        contract = adapter.select_campaign_contract(
            mother_spot=24900,
            selected_at=datetime(2026, 7, 20, 10, 0),
        )
        self.assertEqual(contract.expiry, date(2026, 7, 28))
        self.assertEqual(contract.strike, 24800)
        self.assertEqual(contract.lot_size, 65)
        order = adapter.place_order(contract, side="BUY", quantity=65)
        self.assertEqual(order.status, "PAPER")
        self.assertEqual(order.product_type, "CARRYFORWARD")
        self.assertEqual(adapter.get_order(order.order_id), order)
        self.assertFalse(adapter.dte_allows_new_rungs(contract, datetime(2026, 7, 27, 12, 0)))


if __name__ == "__main__":
    unittest.main()
