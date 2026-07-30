import asyncio
import unittest
from datetime import date, datetime, timedelta

from engine.cascade_options import (
    CascadeOptionsAdapter,
    FixedCampaignOption,
    IndexCandle,
    LadderCandleEntryPaper,
    NiftyIndexCascadeGeometry,
    NiftyOptionsPaperCascade,
    OneHourCandleEntryPaper,
    OptionsAdapterError,
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


class _RecordingDhan:
    """Records what the adapter asked Dhan for, and returns no candles."""

    def __init__(self):
        self.calls = []

    def get_historical_data(
        self, security_id, exchange_segment, instrument_type, expiry_code, from_date, to_date, candle_type
    ):
        self.calls.append(
            {
                "security_id": security_id,
                "exchange_segment": exchange_segment,
                "instrument_type": instrument_type,
                "from_date": from_date,
                "to_date": to_date,
                "candle_type": candle_type,
            }
        )
        return None


class _BankNiftyScripMaster:
    """BankNifty's chain: monthly expiries, a 30-unit lot, 100-point strikes."""

    @classmethod
    def get_expiries(cls, symbol):
        assert symbol == "BANKNIFTY"
        return ["2026-07-28", "2026-08-25", "2026-09-29"]

    @classmethod
    def get_lot_size(cls, symbol, expiry):
        assert (symbol, expiry) == ("BANKNIFTY", "2026-08-25")
        return 30

    @classmethod
    def lookup(cls, symbol, strike, expiry, option_type):
        assert (symbol, strike, expiry, option_type) == ("BANKNIFTY", 57000, "2026-08-25", "CE")
        return "998877"


class MultiInstrumentAdapterTests(unittest.TestCase):
    """The adapter reaches each index by its own confirmed id, not NIFTY's."""

    def test_candles_come_from_the_named_index_security_id(self):
        dhan = _RecordingDhan()
        adapter = CascadeOptionsAdapter(dhan, scrip_master=_ScripMaster)
        asyncio.run(
            adapter.async_get_candles("BANKNIFTY", "15m", from_date=date(2026, 7, 20), to_date=date(2026, 7, 21))
        )
        call = dhan.calls[0]
        self.assertEqual(call["security_id"], "25")
        self.assertEqual(call["exchange_segment"], "IDX_I")
        self.assertEqual(call["candle_type"], "15")

    def test_sensex_is_not_fetched_through_the_live_feed_id(self):
        # The live market feed reaches SENSEX through id "1", which the history
        # API answers with a healthy series for a different index.  If this ever
        # returns "1", every SENSEX backtest is priced off the wrong market.
        dhan = _RecordingDhan()
        adapter = CascadeOptionsAdapter(dhan, scrip_master=_ScripMaster)
        asyncio.run(adapter.async_get_candles("SENSEX", "5m"))
        self.assertEqual(dhan.calls[0]["security_id"], "51")

    def test_an_unknown_index_is_refused_before_any_fetch(self):
        dhan = _RecordingDhan()
        adapter = CascadeOptionsAdapter(dhan, scrip_master=_ScripMaster)
        with self.assertRaises(OptionsAdapterError):
            asyncio.run(adapter.async_get_candles("NIFTYNEXT50", "5m"))
        self.assertEqual(dhan.calls, [])

    def test_contract_selection_follows_the_named_index_chain(self):
        adapter = CascadeOptionsAdapter(_Dhan(), scrip_master=_BankNiftyScripMaster)
        contract = adapter.select_campaign_contract(
            mother_spot=57205,
            selected_at=datetime(2026, 7, 24, 10, 0),
            strike_step=100,
            symbol="BANKNIFTY",
        )
        # Monthly chain: 28 July is only 4 days out, so the campaign rolls to the
        # August contract -- and nothing here had to be told BankNifty is monthly.
        # The gap between expiries is a month rather than a week, which is the
        # whole difference, and the same "first one far enough out" rule covers it.
        self.assertEqual(contract.underlying, "BANKNIFTY")
        self.assertEqual(contract.expiry, date(2026, 8, 25))
        self.assertEqual(contract.strike, 57000)
        self.assertEqual(contract.lot_size, 30)


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

    def test_single_shot_never_re_arms_a_closed_rung_on_a_new_low(self):
        # Same seeded-rung setup as the new-low release test, but single_shot:
        # the mother is one trade, so a fresh low must NOT release the closed
        # rung and must NOT log a restart.
        from engine.cascade_options import PaperCascadeRung

        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")
        engine = NiftyOptionsPaperCascade(
            mother, contract, adapter, lambda _t, _c: 100, PaperCascadeConfig(rung_inr=6500, single_shot=True)
        )
        rung = PaperCascadeRung(1, 2, 90, 6500, status="CLOSED")
        engine.rungs[rung.key] = rung
        engine.reuse_below = 89
        engine.on_candle(IndexCandle(ts(1), 88, 89, 88, 88.5))
        self.assertFalse(any(row["event"] == "new_low_restart" for row in engine.events))
        self.assertEqual(engine.rungs[rung.key].status, "CLOSED")

    def test_max_rounds_suppresses_the_new_low_re_arm_at_the_cap(self):
        # max_rounds keeps deeper-level averaging WITHIN a round but stops the
        # new-low restart once that many rounds have booked. With max_rounds=1
        # and one round already closed, a fresh low must NOT release the rung;
        # below the cap (no rounds yet) it still re-arms -- proving the gate is
        # round-count based, not a blanket off switch like single_shot.
        from engine.cascade_options import PaperCascadeRung

        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")

        capped = NiftyOptionsPaperCascade(
            mother, contract, adapter, lambda _t, _c: 100, PaperCascadeConfig(rung_inr=6500, max_rounds=1)
        )
        rung = PaperCascadeRung(1, 2, 90, 6500, status="CLOSED")
        capped.rungs[rung.key] = rung
        capped.reuse_below = 89
        capped.rounds.append(object())  # one round already booked -> at the cap
        capped._release_closed_rungs(IndexCandle(ts(1), 88, 89, 88, 88.5))
        self.assertEqual(capped.rungs[rung.key].status, "CLOSED")

        below = NiftyOptionsPaperCascade(
            mother, contract, adapter, lambda _t, _c: 100, PaperCascadeConfig(rung_inr=6500, max_rounds=1)
        )
        rung2 = PaperCascadeRung(1, 2, 90, 6500, status="CLOSED")
        below.rungs[rung2.key] = rung2
        below.reuse_below = 89  # no rounds booked yet -> still re-arms
        below._release_closed_rungs(IndexCandle(ts(1), 88, 89, 88, 88.5))
        self.assertNotEqual(below.rungs[rung2.key].status, "CLOSED")

    def test_max_round_premium_skips_a_deeper_leg_that_would_breach_the_cap(self):
        # One leg is already open (6,500 deployed). A deeper leg costing another
        # 6,500 would take the round to 13,000 > a 10,000 cap, so it is skipped:
        # no new fill, no broker order, the pending rung is CANCELLED, and the
        # existing position is left to ride. The FIRST entry is never capped.
        from engine.cascade_options import PaperCascadeFill, PaperCascadeRung

        adapter = _PaperAdapter()
        mother = IndexCandle(ts(0), 100, 110, 90, 105)
        contract = FixedCampaignOption("NIFTY", 100, date(2026, 7, 28), "CE", 65, "1")
        engine = NiftyOptionsPaperCascade(
            mother,
            contract,
            adapter,
            lambda _t, _c: 100,  # premium 100 -> 1 lot of 65 = 6,500 per leg
            PaperCascadeConfig(rung_inr=6500, max_round_premium_inr=10000),
        )
        engine.open_fills = [PaperCascadeFill(ts(1), 95.0, 100.0, 1, 65, ("1:2",), "paper-1", None)]
        deeper = PaperCascadeRung(1, 4, 90, 6500, status="COLLECTED")
        engine.rungs[deeper.key] = deeper
        engine.pending_rung_keys = [deeper.key]
        engine.pending_inr = 6500
        engine.pending_line = 90.0
        engine.pending_stop = 92.0
        engine.pending_stop_timestamp = ts(2)

        engine._fill_pending_stop(IndexCandle(ts(3), 91, 93, 91, 92.5))

        self.assertEqual(len(engine.open_fills), 1)  # deeper leg not taken
        self.assertEqual([o.side for o in adapter.orders], [])  # no broker order
        self.assertEqual(engine.rungs[deeper.key].status, "CANCELLED")
        self.assertTrue(any(row["event"] == "premium_cap_reached" for row in engine.events))

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


class LadderCandleEntryPaperTests(unittest.TestCase):
    """The two-red ladder wrapped as the live Candle Entry paper campaign."""

    @staticmethod
    def _minute(hour: int, minute: int) -> datetime:
        return datetime(2026, 7, 20, hour, minute)

    def _engine(self, adapter, *, signal_only=False, premium=100.0):
        # Mother low sits at 104 so the ladder's new-low gate is exercised by
        # the candles below, not defeated by a deep mother wick.
        mother = IndexCandle(self._minute(9, 15), 104, 110, 104, 105)
        contract = FixedCampaignOption("NIFTY", 24800, date(2026, 7, 28), "CE", 65, "1")
        return LadderCandleEntryPaper(
            mother,
            "1m",
            contract,
            adapter,
            (lambda _t, _c: None) if signal_only else (lambda _t, _c: premium),
            signal_only=signal_only,
        )

    def _two_rung_batches(self):
        m = self._minute
        return {
            "1m": [
                IndexCandle(m(9, 16), 105, 106, 104, 106),  # seed close
                IndexCandle(m(9, 17), 106, 106, 102, 103),  # red 1
                IndexCandle(m(9, 18), 103, 103, 100, 101),  # red 2 -> stop armed at 103
                IndexCandle(m(9, 19), 101, 104, 100, 103.5),  # recovery fills rung 1 (1 lot)
            ],
            "5m": [
                IndexCandle(m(9, 20), 101, 102, 99.5, 101.5),  # seed close
                IndexCandle(m(9, 25), 101.5, 101.5, 98, 99),  # red 1, below the 100 gate low
                IndexCandle(m(9, 30), 99, 99, 96, 97),  # red 2 -> stop armed at 99
                IndexCandle(m(9, 35), 97, 99.5, 96.5, 99),  # recovery fills rung 2 (2 lots)
            ],
        }

    def test_ladder_climbs_two_rungs_and_places_matching_paper_orders(self):
        adapter = _PaperAdapter()
        engine = self._engine(adapter)

        engine.ingest(self._two_rung_batches())

        status = engine.get_status()
        self.assertEqual([row["state"] for row in status["rungs"]], ["filled", "filled", "watching", "waiting"])
        self.assertEqual([o.side for o in adapter.orders], ["BUY", "BUY"])
        self.assertEqual([o.quantity for o in adapter.orders], [65, 130])
        self.assertEqual(status["open_fill"]["quantity"], 195)
        # avg entry (103*65 + 99*130)/195 = 100.33; target a quarter back to 110.
        self.assertAlmostEqual(status["target_index"], 102.75, places=2)

    def test_target_hit_sells_the_whole_basket_together(self):
        adapter = _PaperAdapter()
        engine = self._engine(adapter)
        engine.ingest(self._two_rung_batches())

        engine.ingest({"1m": [IndexCandle(self._minute(9, 41), 100, 103, 100, 102)]})

        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual([o.side for o in adapter.orders], ["BUY", "BUY", "SELL"])
        self.assertEqual(adapter.orders[-1].quantity, 195)
        status = engine.get_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["rounds"][0]["exit_reason"], "target")
        self.assertIsNotNone(status["rounds"][0]["net_pnl"])

    def test_ingest_is_idempotent_for_already_seen_candles(self):
        adapter = _PaperAdapter()
        engine = self._engine(adapter)
        batches = self._two_rung_batches()

        engine.ingest(batches)
        engine.ingest(batches)  # the poll loop refetches from the mother date

        self.assertEqual([o.side for o in adapter.orders], ["BUY", "BUY"])
        self.assertEqual(len(engine.ladder.fills), 2)

    def test_signal_only_replay_records_geometry_without_any_paper_order(self):
        adapter = _PaperAdapter()
        engine = self._engine(adapter, signal_only=True)

        engine.ingest(self._two_rung_batches())
        engine.finish_replay(IndexCandle(self._minute(9, 35), 97, 99.5, 96.5, 99), reached_expiry=False)

        status = engine.get_status()
        self.assertEqual(adapter.orders, [])
        self.assertTrue(status["replay_complete"])
        self.assertEqual(status["pricing_mode"], "signal_only_dhan")
        self.assertEqual(status["signal_entry"]["index_price"], 103)
        self.assertIsNone(status["rungs"][0]["fill"]["option_premium"])

    def test_kill_sells_open_basket_and_stops_the_campaign(self):
        adapter = _PaperAdapter()
        engine = self._engine(adapter)
        engine.ingest(self._two_rung_batches())

        self.assertTrue(engine.kill_and_close(IndexCandle(self._minute(9, 40), 100, 100, 100, 100)))

        self.assertEqual(engine.status, "KILLED")
        self.assertEqual([o.side for o in adapter.orders], ["BUY", "BUY", "SELL"])
        self.assertEqual(engine.open_quantity, 0)


if __name__ == "__main__":
    unittest.main()
