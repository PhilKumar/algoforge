"""The swing-anchored touch ladder: geometry, sizing, the cap and the exits."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from engine.fib_touch_ladder import (
    GEOMETRY_TIMEFRAMES,
    HALVING_LEVELS,
    ExecutionRefused,
    FibTouchConfig,
    FibTouchError,
    FibTouchLadder,
    LiveExecutor,
    PaperExecutor,
    atm_strike,
    find_swing_anchor,
    level_price,
    select_expiry,
)

IST_START = datetime(2026, 8, 6, 9, 15)


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


def bars(rows, start: datetime = IST_START, step_minutes: int = 1) -> list[Bar]:
    """(open, high, low, close) tuples into consecutive 1-minute candles."""
    return [Bar(start + timedelta(minutes=i * step_minutes), o, h, low, c) for i, (o, h, low, c) in enumerate(rows)]


def falling_then_bouncing() -> list[Bar]:
    """A leg up, a fall into the mother, then two greens that freeze the low.

    Index 0-2 rise (so the backward walk stops there and the high anchor is
    24,700), 3-6 fall to the mother at index 6, 7-8 are the two greens.
    """
    return bars(
        [
            (24_660, 24_680, 24_655, 24_675),  # 0 green
            (24_675, 24_700, 24_670, 24_695),  # 1 green  <- leg high 24,700
            (24_695, 24_698, 24_680, 24_685),  # 2 red
            (24_685, 24_690, 24_660, 24_662),  # 3 red
            (24_662, 24_665, 24_640, 24_642),  # 4 red
            (24_642, 24_644, 24_620, 24_622),  # 5 red
            (24_622, 24_624, 24_600, 24_602),  # 6 red   <- MOTHER, low 24,600
            (24_602, 24_612, 24_600, 24_610),  # 7 green
            (24_610, 24_620, 24_608, 24_618),  # 8 green <- involvement here
        ]
    )


class SwingAnchorTests(unittest.TestCase):
    def test_ce_anchor_is_the_leg_high_and_the_involvement_low(self):
        candles = falling_then_bouncing()
        mother = candles[6].timestamp
        anchor = find_swing_anchor(candles, mother, "CE")
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.high, 24_700.0)
        self.assertEqual(anchor.low, 24_600.0)
        self.assertEqual(anchor.span, 100.0)
        # Confirmed only on the SECOND green -- the involvement is not visible
        # before its run closes.
        self.assertEqual(anchor.confirmed_at, candles[8].timestamp)

    def test_no_anchor_until_the_involvement_closes(self):
        candles = falling_then_bouncing()
        mother = candles[6].timestamp
        # One green is not involvement.
        self.assertIsNone(find_swing_anchor(candles[:8], mother, "CE"))

    def test_pe_mirrors_the_rule(self):
        # Mirror the CE fixture: a fall into a low, a rise to the mother, then
        # two reds that freeze the high.
        candles = bars(
            [
                (24_640, 24_645, 24_620, 24_625),  # red
                (24_625, 24_630, 24_600, 24_605),  # red  <- leg low 24,600
                (24_605, 24_620, 24_602, 24_618),  # green
                (24_618, 24_640, 24_615, 24_638),  # green
                (24_638, 24_700, 24_635, 24_695),  # green <- MOTHER, high 24,700
                (24_695, 24_698, 24_688, 24_690),  # red
                (24_690, 24_692, 24_680, 24_682),  # red   <- involvement
            ]
        )
        anchor = find_swing_anchor(candles, candles[4].timestamp, "PE")
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.high, 24_700.0)
        self.assertEqual(anchor.low, 24_600.0)
        self.assertEqual(anchor.confirmed_at, candles[6].timestamp)

    def test_side_must_be_ce_or_pe(self):
        with self.assertRaises(FibTouchError):
            find_swing_anchor(falling_then_bouncing(), IST_START, "XX")


class GeometryTests(unittest.TestCase):
    def test_ce_levels_step_below_the_low(self):
        # span 100, high 24,700 -> L1 is the low, L2 one span beyond it.
        self.assertEqual(level_price("CE", 24_700, 24_600, 1), 24_600)
        self.assertEqual(level_price("CE", 24_700, 24_600, 2), 24_500)
        self.assertEqual(level_price("CE", 24_700, 24_600, 3), 24_400)
        self.assertEqual(level_price("CE", 24_700, 24_600, 16), 23_100)

    def test_pe_levels_step_above_the_high(self):
        self.assertEqual(level_price("PE", 24_700, 24_600, 2), 24_800)
        self.assertEqual(level_price("PE", 24_700, 24_600, 3), 24_900)

    def test_halving_ladder_is_phils_locked_list(self):
        self.assertEqual(HALVING_LEVELS, (2, 3, 4, 6, 8, 12, 16))

    def test_a_flat_anchor_is_refused(self):
        with self.assertRaises(FibTouchError):
            level_price("CE", 24_600, 24_600, 2)


class ExpiryTests(unittest.TestCase):
    # NIFTY's real Tuesday weeklies, from Dhan's scrip master 2026-08-05.
    WEEKLY = [date(2026, 8, 11), date(2026, 8, 18), date(2026, 8, 25), date(2026, 9, 1)]
    # BANKNIFTY/FINNIFTY/MIDCPNIFTY list only monthlies -- NSE withdrew their
    # weeklies, so the same rule has to land on the near monthly.
    MONTHLY = [date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27)]

    def test_current_week_when_it_is_far_enough_out(self):
        self.assertEqual(select_expiry(self.WEEKLY, date(2026, 8, 6), min_dte=4), date(2026, 8, 11))

    def test_rolls_to_next_week_inside_four_days(self):
        # 8 Aug -> the 11th is 3 days out, so the 18th is taken.
        self.assertEqual(select_expiry(self.WEEKLY, date(2026, 8, 8), min_dte=4), date(2026, 8, 18))

    def test_exactly_four_days_still_qualifies(self):
        self.assertEqual(select_expiry(self.WEEKLY, date(2026, 8, 7), min_dte=4), date(2026, 8, 11))

    def test_monthly_only_symbol_lands_on_the_near_monthly(self):
        self.assertEqual(select_expiry(self.MONTHLY, date(2026, 8, 6), min_dte=4), date(2026, 8, 25))
        self.assertEqual(select_expiry(self.MONTHLY, date(2026, 8, 24), min_dte=4), date(2026, 9, 29))

    def test_an_empty_chain_refuses(self):
        with self.assertRaises(FibTouchError):
            select_expiry([], date(2026, 8, 6))

    def test_atm_rounds_to_the_listed_ladder(self):
        self.assertEqual(atm_strike(24_624, 50), 24_600)
        self.assertEqual(atm_strike(24_626, 50), 24_650)
        self.assertEqual(atm_strike(57_240, 100), 57_200)  # BANKNIFTY's 100 step
        self.assertEqual(atm_strike(14_690, 25), 14_700)  # MIDCPNIFTY's 25 step


def ladder(side="CE", *, cap=75_000.0, premium=200.0, lot_size=65, levels=None, mother_index=6):
    """A ladder wired to a flat premium and NIFTY's real weekly chain."""
    candles = falling_then_bouncing()
    config = FibTouchConfig(
        symbol="NIFTY",
        side=side,
        mother_timestamp=candles[mother_index].timestamp,
        lot_size=lot_size,
        strike_step=50.0,
        levels=tuple(levels) if levels else HALVING_LEVELS,
        capital_cap_inr=cap,
    )
    seen: list = []
    return (
        FibTouchLadder(
            config,
            premium_lookup=lambda when, strike, expiry, side_: (seen.append((strike, expiry)), premium)[1],
            expiry_source=lambda on: [date(2026, 8, 11), date(2026, 8, 18), date(2026, 8, 25)],
        ),
        candles,
        seen,
    )


class LadderTests(unittest.TestCase):
    def test_nothing_trades_before_the_swing_is_confirmed(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        # The ladder is anchored high 24,700 / low 24,600, so L2 = 24,500 and
        # price never went there in this fixture.
        self.assertEqual(engine.status, "ARMED")
        self.assertEqual(engine.fills, [])
        self.assertIsNotNone(engine.anchor)

    def test_a_touch_fills_at_the_level_not_the_close(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        # Drop through L2 (24,500) with a wick; close well away from it.
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_560))
        self.assertEqual(len(engine.fills), 1)
        fill = engine.fills[0]
        self.assertEqual(fill.level, 2)
        self.assertEqual(fill.index_price, 24_500.0)  # the line, not 24,560
        self.assertEqual(fill.buy_number, 1)
        self.assertEqual(fill.lots, 1)
        self.assertEqual(fill.quantity, 65)

    def test_one_lot_per_rung_so_the_position_grows_one_two_three(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        # One candle that sweeps L2 (24,500), L3 (24,400) and L4 (24,300).
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_295, 24_310))
        self.assertEqual([f.level for f in engine.fills], [2, 3, 4])
        self.assertEqual([f.buy_number for f in engine.fills], [1, 2, 3])
        self.assertEqual([f.lots for f in engine.fills], [1, 1, 1])
        self.assertEqual(engine.open_lots, 3)  # "at L4 level, we have 3 lots sitting"

    def test_the_rupee_cap_ends_the_ladder_and_marks_the_rest_unfunded(self):
        # 200 x 65 = Rs 13,000 a lot, so Rs 30,000 funds exactly two.
        engine, candles, _ = ladder(cap=30_000.0, premium=200.0)
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_100, 24_150))
        self.assertEqual(len(engine.fills), 2)
        self.assertEqual(engine.deployed_inr, 26_000.0)
        self.assertEqual(engine.remaining_inr, 4_000.0)
        self.assertEqual(engine.status, "OPEN_CAPPED")
        unfunded = [rung.level for rung in engine.rungs if rung.status == "UNFUNDED"]
        self.assertEqual(unfunded, [4, 6, 8, 12, 16])

    def test_the_strike_follows_the_index_down_so_the_basket_holds_several(self):
        engine, candles, seen = ladder()
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_295, 24_310))
        strikes = [f.strike for f in engine.fills]
        # ATM-2 against 24,500 / 24,400 / 24,300 on a 50 ladder.
        self.assertEqual(strikes, [24_400.0, 24_300.0, 24_200.0])
        self.assertEqual(len(set(strikes)), 3)

    def test_the_target_is_a_quarter_back_toward_the_anchor(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        # One fill at 24,500; anchor high 24,700.
        self.assertAlmostEqual(engine.target_index, 24_550.0, places=2)
        # A second, deeper fill pulls the average and so the target down.
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=2), 24_510, 24_512, 24_395, 24_410))
        self.assertAlmostEqual(engine.average_index_entry, 24_450.0, places=2)
        self.assertAlmostEqual(engine.target_index, 24_512.5, places=2)

    def test_reaching_the_target_closes_the_basket_and_ends_the_campaign(self):
        engine, candles, _ = ladder(premium=200.0)
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        self.assertEqual(engine.status, "OPEN")
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_560, 24_505, 24_555))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "target")
        self.assertIsNotNone(engine.net_pnl)
        # Flat premium in and out: gross is zero and the round still pays costs.
        self.assertEqual(engine.gross_pnl, 0.0)
        assert engine.costs_total is not None
        self.assertGreater(engine.costs_total, 0)
        assert engine.net_pnl is not None
        self.assertLess(engine.net_pnl, 0)

    def test_a_closed_campaign_ignores_later_candles(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_560, 24_505, 24_555))
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_555, 24_560, 24_100, 24_110))
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(engine.status, "CLOSED")

    def test_a_missing_premium_is_a_recorded_gap_never_a_guess(self):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[6].timestamp,
            lot_size=65,
            strike_step=50.0,
        )
        engine = FibTouchLadder(
            config,
            premium_lookup=lambda *a: None,
            expiry_source=lambda on: [date(2026, 8, 11)],
        )
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        self.assertEqual(engine.fills, [])
        self.assertEqual(len(engine.data_gaps), 1)
        self.assertIn("no NIFTY", engine.data_gaps[0])

    def test_expiry_settles_at_intrinsic(self):
        engine, candles, _ = ladder(premium=200.0)
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        # 11 Aug 15:15, the expiry the chain hands back.
        expiry_bar = Bar(datetime(2026, 8, 11, 15, 15), 24_500, 24_505, 24_495, 24_500)
        engine.on_candle(expiry_bar)
        self.assertEqual(engine.status, "EXPIRED")
        self.assertEqual(engine.exit_reason, "expiry_square_off")
        # Strike 24,400 CE with the index at 24,500 is worth 100.
        self.assertEqual(engine._exit_premiums, [100.0])

    def test_status_payload_carries_what_the_console_draws(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        payload = engine.get_status()
        for key in (
            "symbol",
            "side",
            "anchor",
            "levels",
            "fills",
            "deployed_inr",
            "remaining_inr",
            "open_lots",
            "average_premium",
            "target_index",
        ):
            self.assertIn(key, payload)
        fill = payload["fills"][0]
        for key in ("buy_number", "timestamp", "index_price", "premium", "lots", "strike", "expiry", "funded_inr"):
            self.assertIn(key, fill)
        self.assertEqual(fill["funded_inr"], 13_000.0)


class ConfigTests(unittest.TestCase):
    def base(self, **overrides):
        terms = dict(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=IST_START,
            lot_size=65,
            strike_step=50.0,
        )
        terms.update(overrides)
        return FibTouchConfig(**terms)

    def test_levels_must_be_shallow_first(self):
        with self.assertRaises(FibTouchError):
            self.base(levels=(8, 4, 2))

    def test_rejects_a_bad_side(self):
        with self.assertRaises(FibTouchError):
            self.base(side="CALL")

    def test_rejects_a_non_positive_cap(self):
        with self.assertRaises(FibTouchError):
            self.base(capital_cap_inr=0)

    def test_defaults_are_phils_locked_spec(self):
        config = self.base()
        self.assertEqual(config.levels, HALVING_LEVELS)
        self.assertEqual(config.lots_per_rung, 1)
        self.assertEqual(config.capital_cap_inr, 75_000.0)
        self.assertEqual(config.target_fraction, 0.25)
        self.assertEqual(config.min_dte, 4)
        self.assertEqual(config.timeframe, "1m")
        self.assertEqual(config.itm_steps, 2)


class TimeframeTests(unittest.TestCase):
    """The mother's chart decides the geometry; touches stay on 1m."""

    def test_every_chart_a_mother_may_be_read_on(self):
        self.assertEqual(GEOMETRY_TIMEFRAMES, ("1m", "5m", "15m", "1h"))

    def test_an_unknown_geometry_timeframe_is_refused(self):
        with self.assertRaises(FibTouchError):
            FibTouchConfig(
                symbol="NIFTY",
                side="CE",
                mother_timestamp=IST_START,
                lot_size=65,
                strike_step=50.0,
                timeframe="4h",
            )

    def test_entries_stay_on_one_minute_whatever_the_mother_is(self):
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=IST_START,
            lot_size=65,
            strike_step=50.0,
            timeframe="15m",
        )
        self.assertEqual(config.timeframe, "15m")
        self.assertEqual(config.entry_timeframe, "1m")

    def test_a_slow_mother_anchors_off_the_slow_stream_and_fills_on_1m(self):
        # 15m geometry: a wide swing the 1m stream never contains.
        geometry = bars(
            [
                (24_600, 24_620, 24_590, 24_615),  # green
                (24_615, 25_000, 24_610, 24_980),  # green <- leg high 25,000
                (24_980, 24_990, 24_500, 24_520),  # red
                (24_520, 24_530, 24_000, 24_020),  # red   <- MOTHER, low 24,000
                (24_020, 24_200, 24_010, 24_180),  # green
                (24_180, 24_300, 24_170, 24_290),  # green <- involvement
            ],
            step_minutes=15,
        )
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=geometry[3].timestamp,
            lot_size=65,
            strike_step=50.0,
            timeframe="15m",
        )
        engine = FibTouchLadder(
            config,
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
        )
        for bar in geometry:
            engine.on_geometry_candle(bar)
        assert engine.anchor is not None
        self.assertEqual(engine.anchor.high, 25_000.0)
        self.assertEqual(engine.anchor.low, 24_000.0)
        # Span 1,000 -> L2 sits at 25,000 - 2,000 = 23,000.
        self.assertEqual(engine.rungs[0].index_price, 23_000.0)

        # A 1m bar BEFORE the swing was confirmed may not trade, even if it
        # touches: the anchor was not knowable when it printed.
        early = Bar(geometry[3].timestamp + timedelta(minutes=1), 24_000, 24_010, 22_990, 23_100)
        engine.on_candle(early)
        self.assertEqual(engine.fills, [])

        # After confirmation, a 1m touch fills at the level.
        late = Bar(geometry[-1].timestamp + timedelta(minutes=1), 23_100, 23_110, 22_990, 23_050)
        engine.on_candle(late)
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(engine.fills[0].index_price, 23_000.0)

    def test_a_1m_mother_needs_no_second_stream(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        # Fed only through on_candle, yet the swing is anchored.
        self.assertIsNotNone(engine.anchor)


class ExecutorTests(unittest.TestCase):
    """Paper and live share one decision path and differ in one object."""

    def test_paper_is_the_default_you_get_by_forgetting_to_choose(self):
        engine, _c, _s = ladder()
        self.assertIsInstance(engine.executor, PaperExecutor)
        self.assertEqual(engine.get_status()["mode"], "paper")
        self.assertFalse(engine.get_status()["is_live"])

    def test_a_paper_fill_carries_an_order_id(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        self.assertTrue(engine.fills[0].order_id.startswith("paper-"))

    def test_live_refuses_until_it_is_armed_and_records_no_phantom_fill(self):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[6].timestamp,
            lot_size=65,
            strike_step=50.0,
        )
        engine = FibTouchLadder(
            config,
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
            executor=LiveExecutor(broker=object(), symbol="NIFTY"),
        )
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_510))
        self.assertEqual(engine.fills, [])
        self.assertEqual(engine.status, "EXECUTION_REFUSED")
        self.assertEqual(engine.rungs[0].status, "PENDING")
        self.assertTrue(any("not sent" in gap for gap in engine.data_gaps))
        self.assertEqual(engine.get_status()["mode"], "live")
        self.assertFalse(engine.get_status()["armed"])

    def test_an_unarmed_live_executor_raises_rather_than_returning_quietly(self):
        live = LiveExecutor(broker=object(), symbol="NIFTY")
        with self.assertRaises(ExecutionRefused):
            live.buy(
                when=IST_START,
                strike=24_400,
                expiry=date(2026, 8, 11),
                option_type="CE",
                quantity=65,
                lots=1,
                premium=200.0,
            )
        with self.assertRaises(ExecutionRefused):
            live.sell_all(when=IST_START, legs=[])

    def test_arming_is_explicit_and_never_a_default(self):
        import inspect

        signature = inspect.signature(LiveExecutor.__init__)
        self.assertIs(signature.parameters["armed"].default, False)
        self.assertEqual(signature.parameters["armed"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_an_armed_live_executor_sends_a_real_order(self):
        sent = []

        class _Broker:
            def place_option_order(self, symbol, strike, expiry, option_type, *, side, quantity):
                sent.append((symbol, strike, expiry, option_type, side, quantity))
                return SimpleNamespace(order_id="DHAN-1")

        live = LiveExecutor(broker=_Broker(), symbol="NIFTY", armed=True)
        receipt = live.buy(
            when=IST_START,
            strike=24_400,
            expiry=date(2026, 8, 11),
            option_type="CE",
            quantity=65,
            lots=1,
            premium=200.0,
        )
        self.assertEqual(receipt, {"order_id": "DHAN-1", "mode": "live"})
        self.assertEqual(sent, [("NIFTY", 24_400.0, "2026-08-11", "CE", "BUY", 65)])


if __name__ == "__main__":
    unittest.main()
