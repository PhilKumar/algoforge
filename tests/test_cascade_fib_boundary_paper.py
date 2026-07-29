"""Incremental (on_candle) tests for the FibBoundaryPaper campaign engine.

These prove the same manual-mother fib-boundary rules the batch backtester in
tests/test_cascade_fib_boundary.py proves, but through the live paper interface
(IndexCandle in, paper adapter + current-quote premium out) that the runtime
loop and persistence drive.
"""

import unittest
from datetime import date, datetime

from engine.cascade_options import (
    FibBoundaryPaper,
    FixedCampaignOption,
    IndexCandle,
)


def ts(hh, mm):
    return datetime(2026, 7, 29, hh, mm)


class _PremiumBook:
    """(hour, minute) -> premium, or None for a gap."""

    def __init__(self, table):
        self.table = dict(table)

    def __call__(self, timestamp, _contract):
        return self.table.get((timestamp.hour, timestamp.minute))


class _PaperAdapter:
    paper_only = True

    def __init__(self, squareoff=False):
        self.orders = []
        self._squareoff = squareoff

    def expiry_squareoff_due(self, _contract, _at):
        return self._squareoff

    def place_order(self, contract, *, side, quantity):
        order = type("PaperOrder", (), {"order_id": f"paper-{len(self.orders) + 1}"})()
        self.orders.append({"contract": contract, "side": side, "quantity": quantity})
        return order


def _ce_contract():
    return FixedCampaignOption("NIFTY", 24000, date(2026, 8, 11), "CE", 65, "111")


def _pe_contract():
    return FixedCampaignOption("NIFTY", 24200, date(2026, 8, 11), "PE", 65, "222")


def _c(hh, mm, o, h, low, c):
    return IndexCandle(ts(hh, mm), o, h, low, c)


class FibBoundaryPaperGeometryTest(unittest.TestCase):
    def _mother(self):
        return IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)

    def test_ce_boundaries_follow_the_timeframe(self):
        for tf, levels in (("1m", [4, 8]), ("5m", [4, 8]), ("15m", [2, 4, 8]), ("1h", [2, 4, 8])):
            engine = FibBoundaryPaper(self._mother(), _ce_contract(), _PaperAdapter(), lambda _t, _c: 100, timeframe=tf)
            self.assertEqual([rung.level for rung in engine.rungs], levels)
        # Deep CE line prices fall away below the mother high.
        engine = FibBoundaryPaper(self._mother(), _ce_contract(), _PaperAdapter(), lambda _t, _c: 100, timeframe="5m")
        self.assertAlmostEqual(engine.rungs[0].index_price, 24180 - 4 * 130)  # 23660
        self.assertAlmostEqual(engine.rungs[1].index_price, 24180 - 8 * 130)  # 23140

    def test_pe_boundaries_mirror_above(self):
        engine = FibBoundaryPaper(self._mother(), _pe_contract(), _PaperAdapter(), lambda _t, _c: 100, timeframe="5m")
        self.assertAlmostEqual(engine.rungs[0].index_price, 24050 + 4 * 130)  # 24570
        self.assertAlmostEqual(engine.rungs[1].index_price, 24050 + 8 * 130)  # 25090


class FibBoundaryPaperCeCampaignTest(unittest.TestCase):
    def _run(self, adapter=None):
        adapter = adapter or _PaperAdapter()
        mother = IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)
        premiums = _PremiumBook({(9, 25): 150.0, (9, 40): 90.0, (9, 45): 210.0})
        engine = FibBoundaryPaper(mother, _ce_contract(), adapter, premiums, timeframe="5m", rung_inr=75_000.0)
        candles = [
            _c(9, 15, 24040, 24050, 23690, 23700),  # red, above L4 -> streak 1
            _c(9, 20, 23700, 23710, 23600, 23640),  # red, close <= L4 (23660) -> arm L4 @ 23640
            _c(9, 25, 23640, 23680, 23630, 23670),  # high >= trigger -> FILL L4
            _c(9, 30, 23670, 23675, 23380, 23400),  # red, above L8 -> streak 1
            _c(9, 35, 23400, 23410, 23050, 23100),  # red, close <= L8 (23140) -> arm L8 @ 23100
            _c(9, 40, 23100, 23160, 23090, 23150),  # low, high >= trigger -> FILL L8
            _c(9, 45, 23150, 23560, 23150, 23540),  # high >= target -> EXIT
        ]
        for candle in candles:
            engine.on_candle(candle)
        return engine

    def test_fills_both_boundaries_then_targets_out_net_positive(self):
        engine = self._run()
        status = engine.get_status()
        self.assertEqual(engine.status, "CLOSED")
        self.assertFalse(status["running"])
        self.assertEqual([rung["status"] for rung in status["boundaries"]], ["CLOSED", "CLOSED"])
        self.assertEqual(len(engine.rounds), 1)
        round_row = engine.rounds[0]
        self.assertEqual(round_row.exit_reason, "target")
        self.assertEqual(len(round_row.fills), 2)
        self.assertGreater(round_row.net_pnl, 0)
        self.assertLess(round_row.net_pnl, round_row.gross_pnl)  # costs charged

    def test_one_and_done_ignores_candles_after_the_target(self):
        engine = self._run()
        # A later dip that would re-cross L8 must NOT re-arm or add a round.
        engine.on_candle(_c(9, 50, 23540, 23545, 22900, 22950))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(len(engine.rounds), 1)

    def test_missing_buy_quote_holds_without_inventing_a_fill(self):
        # Drop the 09:25 quote: the L4 stop cannot fill, so no order is placed.
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)
        engine = FibBoundaryPaper(mother, _ce_contract(), adapter, _PremiumBook({}), timeframe="5m")
        engine.on_candle(_c(9, 15, 24040, 24050, 23690, 23700))
        engine.on_candle(_c(9, 20, 23700, 23710, 23600, 23640))
        engine.on_candle(_c(9, 25, 23640, 23680, 23630, 23670))
        self.assertEqual(engine.status, "AWAITING_OPTION_QUOTE")
        self.assertEqual(engine.open_fills, [])
        self.assertEqual(adapter.orders, [])

    def test_round_trip_serialisation_preserves_mid_campaign_state(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)
        premiums = _PremiumBook({(9, 25): 150.0})
        engine = FibBoundaryPaper(mother, _ce_contract(), adapter, premiums, timeframe="5m")
        for candle in [
            _c(9, 15, 24040, 24050, 23690, 23700),
            _c(9, 20, 23700, 23710, 23600, 23640),
            _c(9, 25, 23640, 23680, 23630, 23670),  # L4 filled, L8 still pending
        ]:
            engine.on_candle(candle)
        self.assertEqual(engine.status, "OPEN")
        restored = FibBoundaryPaper.from_dict(engine.to_dict(), adapter=adapter, option_premium_lookup=premiums)
        self.assertEqual(restored.status, "OPEN")
        self.assertEqual([r.status for r in restored.rungs], ["FILLED", "PENDING"])
        self.assertEqual(len(restored.open_fills), 1)
        self.assertAlmostEqual(restored.target_index, engine.target_index)
        self.assertEqual(len(restored.history), len(engine.history))


class FibBoundaryPaperPeCampaignTest(unittest.TestCase):
    def test_pe_mirror_fills_above_and_targets_down(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)
        # L4 = 24570, L8 = 25090; PE arms on 2 greens beyond, fills when price
        # trades back DOWN through the armed close, targets 0.25 below the avg.
        premiums = _PremiumBook({(9, 25): 150.0, (9, 40): 90.0, (9, 45): 210.0})
        engine = FibBoundaryPaper(mother, _pe_contract(), adapter, premiums, timeframe="5m", rung_inr=75_000.0)
        candles = [
            _c(9, 15, 24100, 24610, 24090, 24600),  # green, above L4 -> streak 1
            _c(9, 20, 24600, 24630, 24590, 24620),  # green, close >= L4 -> arm L4 @ 24620
            _c(9, 25, 24620, 24650, 24610, 24630),  # low <= trigger -> FILL L4
            _c(9, 30, 24630, 25010, 24620, 25000),  # green, below L8 -> streak 1
            _c(9, 35, 25000, 25130, 24990, 25120),  # green, close >= L8 -> arm L8 @ 25120
            _c(9, 40, 25120, 25130, 25100, 25110),  # low <= trigger -> FILL L8
            _c(9, 45, 25110, 25120, 24700, 24720),  # low <= target -> EXIT
        ]
        for candle in candles:
            engine.on_candle(candle)
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(engine.rounds[0].exit_reason, "target")
        self.assertEqual(len(engine.rounds[0].fills), 2)
        self.assertGreater(engine.rounds[0].net_pnl, 0)


class FibBoundaryPaperSignalOnlyTest(unittest.TestCase):
    def test_signal_only_proves_geometry_without_premium(self):
        mother = IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)
        engine = FibBoundaryPaper(
            mother, _ce_contract(), _PaperAdapter(), lambda _t, _c: None, timeframe="5m", signal_only=True
        )
        for candle in [
            _c(9, 15, 24040, 24050, 23690, 23700),
            _c(9, 20, 23700, 23710, 23600, 23640),  # arm L4
            _c(9, 25, 23640, 23680, 23630, 23670),  # signal fill L4 (no premium)
            _c(9, 45, 23670, 23900, 23650, 23880),  # index reaches 0.25 target
        ]:
            engine.on_candle(candle)
        engine.complete_historical_replay(engine.history[-1])
        self.assertEqual(engine.status, "CLOSED")
        self.assertTrue(engine.replay_complete)
        self.assertEqual(engine.rounds, [])  # no P&L invented
        self.assertEqual(len(engine.signal_fills), 1)


class FibBoundaryPaperKillTest(unittest.TestCase):
    def test_kill_closes_an_open_basket_and_cancels_pending(self):
        adapter = _PaperAdapter()
        mother = IndexCandle(ts(9, 10), 24100, 24180, 24050, 24100)
        premiums = _PremiumBook({(9, 25): 150.0, (9, 50): 120.0})
        engine = FibBoundaryPaper(mother, _ce_contract(), adapter, premiums, timeframe="5m")
        for candle in [
            _c(9, 15, 24040, 24050, 23690, 23700),
            _c(9, 20, 23700, 23710, 23600, 23640),
            _c(9, 25, 23640, 23680, 23630, 23670),  # L4 filled
        ]:
            engine.on_candle(candle)
        self.assertEqual(engine.status, "OPEN")
        killed = engine.kill_and_close(_c(9, 50, 23670, 23680, 23660, 23670))
        self.assertTrue(killed)
        self.assertEqual(engine.status, "KILLED")
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(engine.rounds[0].exit_reason, "manual_kill")
        self.assertEqual([r.status for r in engine.rungs if r.status == "CANCELLED"][0], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
