import unittest
from datetime import date, datetime, timedelta

from engine.cascade_options import (
    CascadeOptionsAdapter,
    FixedCampaignOption,
    IndexCandle,
    NiftyIndexCascadeGeometry,
    NiftyOptionsPaperCascade,
    OneHourCandleEntryPaper,
    PaperCascadeConfig,
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


class _PaperAdapter:
    paper_only = True

    def __init__(self):
        self.orders = []

    def dte_allows_new_rungs(self, _contract, _at):
        return True

    def expiry_squareoff_due(self, _contract, _at):
        return False

    def place_order(self, contract, *, side, quantity):
        order = type(
            "PaperOrder",
            (),
            {"order_id": f"paper-{len(self.orders) + 1}", "contract": contract, "side": side, "quantity": quantity},
        )()
        self.orders.append(order)
        return order


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


class PaperRoundTests(unittest.TestCase):
    def test_paper_round_uses_reverse_stop_then_books_net_option_pnl(self):
        mother = IndexCandle(ts(0), 65020.00, 65107.99, 65002.00, 65051.98)
        # The first six candles are the verified geometry fixture.  The later
        # four cross F1-L2, arm the two-red recovery stop, fill it, then reach
        # the index recovery target on a later candle.
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
        contract = FixedCampaignOption("NIFTY", 64800, date(2026, 7, 28), "CE", 65, "123456")

        def premium(timestamp, _contract):
            return 100.0 if timestamp == ts(9) else 120.0 if timestamp == ts(10) else None

        engine = NiftyOptionsPaperCascade(
            mother,
            contract,
            adapter,
            premium,
            PaperCascadeConfig(rung_inr=13000),
        ).run(candles)
        self.assertEqual(len(engine.rounds), 1)
        round_row = engine.rounds[0]
        self.assertEqual(round_row.fills[0].quantity, 130)
        self.assertEqual(round_row.fills[0].lots, 2)
        self.assertEqual(round_row.exit_reason, "target")
        self.assertEqual(round_row.gross_pnl, 2600.0)
        self.assertGreater(round_row.costs.total, 0)
        self.assertLess(round_row.net_pnl, round_row.gross_pnl)
        self.assertEqual([order.side for order in adapter.orders], ["BUY", "SELL"])

        restored = NiftyOptionsPaperCascade.from_dict(engine.to_dict(), adapter=adapter, option_premium_lookup=premium)
        self.assertEqual(restored.get_status()["contract"]["strike"], 64800)
        self.assertEqual(len(restored.rounds), 1)
        self.assertEqual(restored.rounds[0].net_pnl, round_row.net_pnl)

    def test_lot_ladder_sizes_the_first_fill_at_one_lot_ignoring_the_budget(self):
        # Same verified fixture, but the fib-boundary lot ladder replaces the
        # rupee budget: the first fill takes 1 lot (not the 2 that rung_inr
        # 13000 / premium 100 / 65 would buy).
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
        contract = FixedCampaignOption("NIFTY", 64800, date(2026, 7, 28), "CE", 65, "123456")

        def premium(timestamp, _contract):
            return 100.0 if timestamp == ts(9) else 120.0 if timestamp == ts(10) else None

        engine = NiftyOptionsPaperCascade(
            mother, contract, adapter, premium, PaperCascadeConfig(rung_inr=13000, lot_ladder=True)
        ).run(candles)
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(engine.rounds[0].fills[0].lots, 1)  # ladder: first buy = 1 lot
        self.assertEqual(engine.rounds[0].fills[0].quantity, 65)

    def test_per_entry_strike_reselects_the_contract_and_prices_it(self):
        # Per-entry mode picks the strike against the index at the fill, records
        # it on the fill, and prices/settles that exact contract.
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
        contract = FixedCampaignOption("NIFTY", 64800, date(2026, 7, 28), "CE", 65, "0")

        def selector(_timestamp, index_price):
            # ATM-2 at 50 steps against the fill index.
            atm = round(index_price / 50) * 50
            strike = int(atm - 100)
            return FixedCampaignOption("NIFTY", strike, date(2026, 7, 28), "CE", 65, str(strike))

        def premium(timestamp, contract):
            # Priced by strike so a wrong contract would give wrong P&L.
            base = 100.0 if timestamp == ts(9) else 120.0 if timestamp == ts(10) else None
            return None if base is None else base + (64800 - contract.strike) * 0.001

        engine = NiftyOptionsPaperCascade(
            mother,
            contract,
            adapter,
            premium,
            PaperCascadeConfig(rung_inr=13000, lot_ladder=True, per_entry_strike=True),
            contract_selector=selector,
        ).run(candles)
        self.assertEqual(len(engine.rounds), 1)
        fill = engine.rounds[0].fills[0]
        self.assertIsNotNone(fill.contract)  # per-entry strike recorded on the fill
        self.assertEqual(fill.lots, 1)
        # The fill priced the SELECTED strike, not the campaign's 64800 default.
        self.assertEqual(fill.option_premium, round(100.0 + (64800 - fill.contract.strike) * 0.001, 2))
        self.assertEqual(engine.rounds[0].exit_reason, "target")
        # Round survives a serialization round-trip with the per-fill contract.
        restored = NiftyOptionsPaperCascade.from_dict(engine.to_dict(), adapter=adapter, option_premium_lookup=premium)
        self.assertEqual(restored.rounds[0].fills[0].contract.strike, fill.contract.strike)

    def test_per_entry_strike_without_selector_is_rejected(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")
        with self.assertRaises(Exception):
            NiftyOptionsPaperCascade(
                mother, contract, adapter, lambda _t, _c: 100, PaperCascadeConfig(rung_inr=6500, per_entry_strike=True)
            )

    def test_new_low_releases_closed_rungs_for_a_fresh_paper_round(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")
        engine = NiftyOptionsPaperCascade(
            mother, contract, adapter, lambda _t, _c: 100, PaperCascadeConfig(rung_inr=6500)
        )
        # Directly seed a finished rung to exercise the exact post-round
        # new-low release rule independently of a full geometry fixture.
        from engine.cascade_options import PaperCascadeRung

        rung = PaperCascadeRung(1, 2, 90, 6500, status="CLOSED")
        engine.rungs[rung.key] = rung
        engine.reuse_below = 89
        engine.on_candle(IndexCandle(ts(1), 88, 89, 88, 88.5))
        self.assertTrue(any(row["event"] == "new_low_restart" for row in engine.events))
        self.assertNotEqual(engine.rungs[rung.key].status, "CLOSED")

    def test_kill_cancels_unfunded_paper_rungs_without_touching_a_broker(self):
        from engine.cascade_options import PaperCascadeRung

        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")
        engine = NiftyOptionsPaperCascade(
            mother, contract, adapter, lambda _t, _c: 100, PaperCascadeConfig(rung_inr=6500)
        )
        pending = PaperCascadeRung(1, 2, 95, 6500, status="PENDING")
        collected = PaperCascadeRung(1, 4, 90, 6500, status="COLLECTED")
        engine.rungs = {pending.key: pending, collected.key: collected}
        engine.pending_rung_keys = [collected.key]
        engine.pending_inr = 6500

        result = engine.kill_and_close(IndexCandle(ts(1), 102, 102, 102, 102))

        self.assertTrue(result["closed"])
        self.assertEqual(set(result["cancelled_rungs"]), {pending.key, collected.key})
        self.assertEqual(engine.status, "KILLED")
        self.assertEqual(engine.pending_inr, 0)
        self.assertEqual([row.side for row in adapter.orders], [])

    def test_one_hour_candle_entry_arms_after_two_lower_closing_reds_and_fills_on_recovery(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")
        engine = OneHourCandleEntryPaper(mother, contract, adapter, lambda _t, _c: 100)
        red_one = IndexCandle(ts(1), 105, 106, 98, 100)
        green_between = IndexCandle(ts(2), 100, 103, 99, 102)
        red_two = IndexCandle(ts(3), 102, 103, 94, 97)
        recovery = IndexCandle(ts(4), 97, 98, 96, 97.5)

        for candle in [red_one, green_between, red_two, recovery]:
            engine.on_candle(candle)

        self.assertEqual(engine.status, "OPEN")
        self.assertEqual(len(engine.qualifying_reds), 2)
        self.assertIsNotNone(engine.fill)
        self.assertEqual(engine.fill.quantity, 65)
        self.assertEqual([row.side for row in adapter.orders], ["BUY"])

    def test_one_hour_historical_replay_records_index_signal_without_option_order(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "")
        engine = OneHourCandleEntryPaper(mother, contract, adapter, lambda _t, _c: None, signal_only=True)
        candles = [
            IndexCandle(ts(1), 105, 106, 98, 100),
            IndexCandle(ts(2), 100, 101, 94, 97),
            IndexCandle(ts(3), 96, 98, 94, 97),
            IndexCandle(ts(4), 98, 101, 96, 100),
        ]

        for candle in candles:
            engine.on_candle(candle)
        engine.complete_historical_replay(candles[-1])

        status = engine.get_status()
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(status["pricing_mode"], "signal_only_dhan")
        self.assertTrue(status["replay_complete"])
        self.assertEqual(status["signal_entry"]["index_price"], 97)
        self.assertEqual([row.side for row in adapter.orders], [])


if __name__ == "__main__":
    unittest.main()
