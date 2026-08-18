"""The swing-anchored touch ladder: geometry, sizing, the cap and the exits."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from unittest.mock import patch

from engine.fib_touch_ladder import (
    BOUNDARY_LEVELS,
    DEEP_CARRY_RUNGS,
    DEEP_TARGET_FRACTION,
    DEEP_TARGET_FROM_LEVEL,
    GEOMETRY_TIMEFRAMES,
    HALVING_LEVELS,
    INTRADAY_CLOSE_AT,
    MAX_BUYS,
    ExecutionRefused,
    FibTouchConfig,
    FibTouchError,
    FibTouchLadder,
    LiveExecutor,
    PaperExecutor,
    atm_strike,
    find_swing_anchor,
    find_trendline,
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
    """The CE shape, as the MERGED geometry actually draws it (2026-08-15).

    The old shape stopped at bar 8, where the previous rule had both swing
    anchors frozen. The trendline+fib rule needs more than that: a fib is only
    DRAWN once its own low is broken, because the levels exist to fund the way
    down and nothing is drawn while the market rises. So the fixture carries on
    into the second leg, and that is what puts real structures on the chart.

    What it produces, verified bar by bar:
        bar  2  lowest low 24,600
        bar  3  the low LOCKS -- closes back above bar 2's close
        bar  6  the bounce tops at 24,700
        bar  9  a red closes decisively under 24,600: TL1 drawn, and with it
                FIB 1  0 = 24,698.00  1 = 24,600.00  span 98
        bar 11  a candle tags the standing line and closes back under it
        bar 12  that structure's low breaks:
                FIB 2  0 = 24,690.00  1 = 24,560.00  span 130

    Two fibs, so the ladder STACKS: F1L2 sits at 24,502.00 and F2L2 at
    24,430.00, and a walk down the rungs crosses between the two structures.

    The last bar stops at 24,505, deliberately just ABOVE the shallowest rung:
    the fixture ends with structure drawn and nothing bought, so a test that
    wants a position asks for one explicitly rather than inheriting a fill it
    never mentioned.

    The mother's high is 24,780, deliberately above the bounce -- a close past
    it ends the campaign, so a fixture meant to keep trading stays under it.
    """
    return bars(
        [
            (24_660, 24_780, 24_640, 24_642),  # 0 red   <- MOTHER, high 24,780
            (24_642, 24_644, 24_620, 24_622),  # 1 red
            (24_622, 24_624, 24_600, 24_602),  # 2 red   <- lowest low 24,600
            (24_602, 24_612, 24_600, 24_610),  # 3 green <- low LOCKS
            (24_610, 24_620, 24_608, 24_618),  # 4 green
            (24_618, 24_650, 24_615, 24_645),  # 5 green
            (24_645, 24_700, 24_640, 24_695),  # 6 green <- bounce high 24,700
            (24_695, 24_698, 24_680, 24_682),  # 7 red
            (24_682, 24_684, 24_670, 24_672),  # 8 red
            (24_672, 24_674, 24_560, 24_570),  # 9  red  <- TL1 + FIB 1 drawn
            (24_570, 24_640, 24_565, 24_635),  # 10 green
            (24_635, 24_690, 24_630, 24_640),  # 11 green <- tags the line
            (24_640, 24_642, 24_505, 24_510),  # 12 red  <- FIB 2 drawn
        ]
    )


class SwingAnchorTests(unittest.TestCase):
    def test_both_anchors_come_from_the_swing_after_the_mother(self):
        candles = falling_then_bouncing()
        anchor = find_swing_anchor(candles, candles[0].timestamp, "CE")
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.low, 24_600.0)
        self.assertEqual(anchor.high, 24_700.0)
        self.assertEqual(anchor.span, 100.0)
        # Both anchors sit AFTER the mother -- neither is its own high or low.
        self.assertGreater(anchor.low_timestamp, candles[0].timestamp)
        self.assertGreater(anchor.high_timestamp, candles[0].timestamp)
        # Confirmed only when the bounce ends on the second red.
        self.assertEqual(anchor.confirmed_at, candles[8].timestamp)

    def test_the_mother_own_high_is_never_the_fib_top(self):
        # Phil's correction: a mother that is itself the highest bar must NOT
        # hand its high to the fib.
        candles = bars(
            [
                (24_690, 24_800, 24_685, 24_688),  # 0 red <- MOTHER, high 24,800
                (24_688, 24_690, 24_600, 24_602),  # 1 red <- low 24,600
                (24_602, 24_612, 24_600, 24_610),  # 2 green
                (24_610, 24_620, 24_608, 24_618),  # 3 green <- LOW frozen
                (24_618, 24_700, 24_615, 24_695),  # 4 green <- high 24,700
                (24_695, 24_698, 24_680, 24_682),  # 5 red
                (24_682, 24_684, 24_670, 24_672),  # 6 red   <- HIGH frozen
            ]
        )
        anchor = find_swing_anchor(candles, candles[0].timestamp, "CE")
        assert anchor is not None
        self.assertEqual(anchor.high, 24_700.0)
        self.assertNotEqual(anchor.high, 24_800.0)

    def test_no_anchor_until_the_bounce_has_ended(self):
        candles = falling_then_bouncing()
        mother = candles[0].timestamp
        # The low has frozen but the high has not: one red is not involvement.
        self.assertIsNone(find_swing_anchor(candles[:8], mother, "CE"))
        # Not even the low is frozen this early.
        self.assertIsNone(find_swing_anchor(candles[:4], mother, "CE"))

    def test_pe_mirrors_the_rule(self):
        # Mirror the CE fixture: a fall into a low, a rise to the mother, then
        # two reds that freeze the high.
        candles = bars(
            [
                (24_640, 24_660, 24_635, 24_658),  # 0 green <- MOTHER
                (24_658, 24_680, 24_655, 24_675),  # 1 green
                (24_675, 24_700, 24_670, 24_695),  # 2 green <- highest high 24,700
                (24_695, 24_698, 24_688, 24_690),  # 3 red
                (24_690, 24_692, 24_680, 24_682),  # 4 red   <- HIGH frozen
                (24_682, 24_684, 24_650, 24_652),  # 5 red
                (24_652, 24_654, 24_600, 24_602),  # 6 red   <- lowest low 24,600
                (24_602, 24_612, 24_600, 24_610),  # 7 green
                (24_610, 24_620, 24_608, 24_618),  # 8 green <- LOW frozen
            ]
        )
        anchor = find_swing_anchor(candles, candles[0].timestamp, "PE")
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.high, 24_700.0)
        self.assertEqual(anchor.low, 24_600.0)
        self.assertEqual(anchor.confirmed_at, candles[8].timestamp)

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


def take_the_turn(engine, start, level, *, minutes=1, sweep_from=None):
    """Collect `level`, then TURN, which is what actually buys.

    Phil's rule of 2026-08-16: a touch no longer buys, it collects; the buy
    waits for two red closes below the collected line and then a rise back
    through the first red's close. Every test that used to drop one candle
    through a level and expect a fill goes through here instead.

    Three bars, in the order the rule reads:
        1. trades through the level and closes red under it   -> collected
        2. a second red under it                              -> stop armed
        3. rises back through that stop                       -> ONE buy

    Returns the bar that filled, so a caller can keep stepping time from it.
    """
    step = timedelta(seconds=minutes)
    # `sweep_from` makes the collecting candle a WIDE one, the way a real fall
    # crosses several rungs in a single bar.
    top = float(sweep_from) if sweep_from is not None else level + 10
    first = Bar(start + step, top - 2, top, level - 7, level - 2)
    second = Bar(start + 2 * step, level - 2, level - 1, level - 12, level - 8)
    rise = Bar(start + 3 * step, level - 8, level + 6, level - 9, level + 4)
    for bar in (first, second, rise):
        engine.on_candle(bar)
    return rise


def ladder(
    side="CE",
    *,
    cap=75_000.0,
    premium=200.0,
    lot_size=65,
    levels=None,
    mother_index=0,
    rearm=True,
    intraday=False,
    buy_mode="levels",
    **config_overrides,
):
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
        rearm_on_new_low=rearm,
        # Phil's 2026-08-16 session switch. The shared fixture runs NORMAL, so
        # the expiry and multi-day tests keep meaning what they say; the
        # intraday rule is exercised by IntradayCloseTests.
        intraday_close=intraday,
        buy_mode=buy_mode,
        **config_overrides,
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

    def test_a_touch_only_collects_it_does_not_buy(self):
        """Phil, 2026-08-16. A level reached is money set aside, not spent."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        # Trade through F1L2 (24,502) and close under it.
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_610, 24_612, 24_495, 24_499))
        self.assertEqual(engine.fills, [], "a touch buys nothing on its own")
        collected = [r.key for r in engine.rungs if r.status == "COLLECTED"]
        self.assertEqual(collected, ["F1L2"])

    def test_the_buy_comes_on_the_turn_and_fills_at_the_stop(self):
        """Two reds under the collected line arm a buy-stop at the first red's
        close, and only a rise back through it buys -- at that stop, which is
        where the market actually was."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertEqual(len(engine.fills), 1)
        fill = engine.fills[0]
        self.assertEqual(fill.rung_key, "F1L2")
        self.assertEqual(fill.index_price, 24_500.0, "the stop -- the first red's close")
        self.assertEqual(fill.buy_number, 1)
        self.assertEqual(fill.lots, 1)
        self.assertEqual(fill.quantity, 65)

    def test_a_knife_that_never_turns_is_never_bought(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        # Straight down through five rungs, red all the way, no recovery.
        for i, low in enumerate((24_495, 24_450, 24_400, 24_350, 24_300), start=1):
            engine.on_candle(Bar(base + timedelta(minutes=i), low + 40, low + 45, low, low + 5))
        self.assertEqual(engine.fills, [], "nothing is bought into a fall that keeps falling")
        self.assertTrue([r for r in engine.rungs if r.status == "COLLECTED"])

    def test_one_sweep_is_ONE_buy_however_many_levels_it_crossed(self):
        """Phil, 2026-08-16, on six lots firing in one opening minute: "it has
        to buy only one... not all". One lot, and it names the deepest level the
        fall reached while saying what else it covered."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_295, sweep_from=24_612)
        self.assertEqual(len(engine.fills), 1)
        fill = engine.fills[0]
        self.assertEqual(fill.lots, 1)
        self.assertEqual(engine.open_lots, 1)
        # Five rungs off BOTH fibs went into that single buy.
        self.assertEqual(sorted(fill.covered), sorted(["F1L2", "F2L2", "F1L3", "F1L4", "F2L3"]))
        self.assertEqual(fill.rung_key, "F2L3", "recorded against the deepest one reached")
        # And every one of them is spent -- none is left waiting to be bought.
        self.assertEqual([r.key for r in engine.rungs if r.status == "COLLECTED"], [])

    def test_the_rupee_cap_ends_the_ladder_and_marks_the_rest_unfunded(self):
        # 200 x 65 = Rs 13,000 a lot, and one buy is one lot, so Rs 20,000
        # funds exactly one of them.
        engine, candles, _ = ladder(cap=20_000.0, premium=200.0)
        for bar in candles:
            engine.on_candle(bar)
        last = take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(engine.deployed_inr, 13_000.0)
        take_the_turn(engine, last.timestamp, 24_430)
        self.assertEqual(len(engine.fills), 1, "the cap stopped the second buy")
        self.assertEqual(engine.status, "OPEN_CAPPED")
        # Every rung the cap stopped is marked -- priced and drawn but
        # unfunded, never silently missing.
        self.assertTrue([rung.key for rung in engine.rungs if rung.status == "UNFUNDED"])

    def test_the_strike_follows_the_index_down_across_buys(self):
        engine, candles, seen = ladder()
        for bar in candles:
            engine.on_candle(bar)
        last = take_the_turn(engine, candles[-1].timestamp, 24_502)
        take_the_turn(engine, last.timestamp, 24_295, sweep_from=24_500)
        strikes = [f.strike for f in engine.fills]
        # ATM-2 re-resolved at the price each buy actually happened at, so a
        # deeper turn holds a lower strike.
        self.assertEqual(len(engine.fills), 2)
        self.assertTrue(all(a >= b for a, b in zip(strikes, strikes[1:])), "strikes only walk down")
        self.assertLess(strikes[1], strikes[0])

    def test_the_target_is_a_quarter_back_toward_the_MOTHER(self):
        """Phil, 2026-08-15: the target runs to the mother, not to the swing
        the ladder is measured on. With fibs stacking there is no single swing
        left to aim at, and the mother is the one price fixed for the campaign."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        last = take_the_turn(engine, candles[-1].timestamp, 24_502)
        # One buy, taken at the stop 24,500; mother high 24,780.
        self.assertAlmostEqual(engine.average_index_entry, 24_500.0, places=2)
        self.assertAlmostEqual(engine.target_index, 24_500.0 + 0.25 * (24_780.0 - 24_500.0), places=2)
        self.assertAlmostEqual(engine.target_index, 24_570.00, places=2)
        # A deeper buy pulls the average down, and the target with it.
        take_the_turn(engine, last.timestamp, 24_430)
        self.assertLess(engine.average_index_entry, 24_500.0)
        self.assertLess(engine.target_index, 24_570.00)
        self.assertAlmostEqual(
            engine.target_index,
            engine.average_index_entry + engine.target_fraction * (24_780.0 - engine.average_index_entry),
            places=6,
        )

    def test_reaching_the_target_closes_the_basket_and_ends_the_campaign(self):
        engine, candles, _ = ladder(premium=200.0)
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        self.assertEqual(engine.status, "OPEN")
        # One fill at 24,502, mother 24,780 -> the target is 24,571.50.
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        # The round is banked and the mother PARKS -- it does not end, because a
        # new deepest low would trade it again.
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(engine.rounds[0]["exit_reason"], "target")
        self.assertIsNotNone(engine.net_pnl)
        # Flat premium in and out: gross is zero and the round still pays costs.
        self.assertEqual(engine.gross_pnl, 0.0)
        assert engine.costs_total is not None
        self.assertGreater(engine.costs_total, 0)
        assert engine.net_pnl is not None
        self.assertLess(engine.net_pnl, 0)

    def test_a_banked_round_rearms_on_a_new_deepest_low(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        # A deep fall AFTER the close must buy nothing, however many rungs it
        # crosses -- and with fibs stacking there are plenty left resting.
        # That fall goes BELOW the campaign's deepest low, so it re-arms the
        # same mother -- and the bar that wakes it may not also buy.
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_575, 24_580, 24_100, 24_110))
        self.assertEqual(engine.fills, [], "the banked round's legs are history")
        self.assertEqual(engine.status, "ARMED")
        self.assertEqual(len(engine.rounds), 1)

    def test_a_missing_premium_is_a_recorded_gap_never_a_guess(self):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        engine = FibTouchLadder(
            config,
            premium_lookup=lambda *a: None,
            expiry_source=lambda on: [date(2026, 8, 11)],
        )
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertEqual(engine.fills, [])
        self.assertEqual(len(engine.data_gaps), 1)
        self.assertIn("no NIFTY", engine.data_gaps[0])

    def test_expiry_settles_at_intrinsic(self):
        engine, candles, _ = ladder(premium=200.0)
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
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
        take_the_turn(engine, candles[-1].timestamp, 24_502)
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

    def test_entries_follow_the_timeframe_he_selected(self):
        """Phil, 2026-08-15: "Why watched at 1m.. Make it as what I select."
        An unset entry timeframe now means FOLLOW THE MOTHER."""
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=IST_START,
            lot_size=65,
            strike_step=50.0,
            timeframe="15m",
        )
        self.assertEqual(config.timeframe, "15m")
        self.assertEqual(config.entry_bar_timeframe, "15m")

    def test_an_explicit_entry_timeframe_still_wins(self):
        """The backtest route replays 1m entries under a slower mother on
        purpose, so naming one has to keep overriding."""
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=IST_START,
            lot_size=65,
            strike_step=50.0,
            timeframe="15m",
            entry_timeframe="1m",
        )
        self.assertEqual(config.entry_bar_timeframe, "1m")

    def test_a_slow_mother_draws_on_the_slow_stream_and_fills_on_1m(self):
        """Geometry on the mother's chart, touches on a faster one.

        The split is opt-in since 2026-08-15 -- an unset entry timeframe now
        follows the mother -- so this names 1m explicitly, which is what the
        backtest route does.
        """
        geometry = bars(
            [
                (24_900, 24_920, 24_880, 24_890),  # 0 red   <- MOTHER, high 24,920
                (24_890, 24_900, 24_000, 24_020),  # 1 red   <- low 24,000
                (24_020, 24_200, 24_010, 24_180),  # 2 green <- the low LOCKS
                (24_180, 24_300, 24_170, 24_290),  # 3 green
                (24_290, 24_600, 24_280, 24_580),  # 4 green <- bounce, under the mother
                (24_580, 24_590, 24_400, 24_420),  # 5 red
                (24_420, 24_430, 23_950, 23_980),  # 6 red   <- TL1 + FIB 1 drawn
                (23_980, 24_300, 23_970, 24_280),  # 7 green
                (24_280, 24_520, 24_270, 24_300),  # 8 green <- tags the line
                (24_300, 24_310, 23_900, 23_930),  # 9 red   <- FIB 2 drawn
            ],
            step_minutes=15,
        )
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=geometry[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            timeframe="15m",
            entry_timeframe="1m",
        )
        engine = FibTouchLadder(
            config,
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
        )
        for bar in geometry:
            engine.on_geometry_candle(bar)
        # Two structures on the 15m stream: 0 = 24,590 / 1 = 24,000 (span 590),
        # then 0 = 24,520 / 1 = 23,950 (span 570). The mother's own high is
        # never an anchor.
        self.assertEqual(len(engine.geometry.fibs), 2)
        assert engine.anchor is not None
        self.assertEqual(engine.anchor.high, 24_520.0)
        self.assertNotEqual(engine.anchor.high, 24_920.0)  # not the mother's own high
        self.assertEqual(engine.rungs[0].key, "F1L2")
        self.assertEqual(engine.rungs[0].index_price, 23_410.0)  # 24,590 - 2 x 590

        # A 1m bar from BEFORE the fib was drawn may not trade it, even if it
        # touches: the structure did not exist when that bar printed.
        early = Bar(geometry[2].timestamp + timedelta(minutes=1), 24_000, 24_010, 23_000, 23_100)
        engine.on_candle(early)
        self.assertEqual(engine.fills, [], "a level cannot fill before its fib was drawn")

        # A 1m bar AFTER it can collect -- and one turn buys once, covering
        # both fibs' L2 (F1L2 23,410 and F2L2 23,380).
        take_the_turn(engine, geometry[-1].timestamp, 23_380, sweep_from=23_510)
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(sorted(engine.fills[0].covered), ["F1L2", "F2L2"])

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
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertTrue(engine.fills[0].order_id.startswith("paper-"))

    def test_live_refuses_until_it_is_armed_and_records_no_phantom_fill(self):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        engine = FibTouchLadder(
            config,
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
            executor=LiveExecutor(broker=object(), symbol="NIFTY"),
        )
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertEqual(engine.fills, [])
        self.assertEqual(engine.status, "EXECUTION_REFUSED")
        self.assertEqual(engine.rungs[0].status, "COLLECTED", "the fall reached it; only the send failed")
        self.assertTrue(any("not sent" in gap for gap in engine.data_gaps))
        self.assertEqual(engine.get_status()["mode"], "live")
        self.assertFalse(engine.get_status()["armed"])

    def test_an_unarmed_live_executor_refuses_new_risk_but_allows_exit(self):
        sent = []

        class _Broker:
            def place_option_order(self, **order):
                sent.append(
                    (
                        order["underlying"],
                        order["strike_price"],
                        order["expiry"],
                        order["option_type"],
                        order["transaction_type"],
                        order["quantity"],
                    )
                )
                return {"orderId": "DHAN-EXIT-1"}

        live = LiveExecutor(broker=_Broker(), symbol="NIFTY")
        with patch("engine.fib_touch_ladder.FIB_TOUCH_LIVE_EXECUTION_ENABLED", True):
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
            receipt = live.sell_all(
                when=IST_START,
                legs=[
                    {
                        "strike": 24_400,
                        "expiry": "2026-08-11",
                        "option_type": "CE",
                        "quantity": 65,
                    }
                ],
            )
        self.assertEqual(receipt, {"order_id": "DHAN-EXIT-1", "mode": "live"})
        self.assertEqual(sent, [("NIFTY", 24_400.0, "2026-08-11", "CE", "SELL", 65)])

    def test_live_executor_is_safety_locked_even_when_armed(self):
        live = LiveExecutor(broker=object(), symbol="NIFTY", armed=True)
        with self.assertRaisesRegex(ExecutionRefused, "temporarily disabled"):
            live.buy(
                when=IST_START,
                strike=24_400,
                expiry=date(2026, 8, 11),
                option_type="CE",
                quantity=65,
                lots=1,
                premium=200.0,
            )

    def test_manual_kill_sends_live_exit_before_marking_the_campaign_killed(self):
        sent = []

        class _Broker:
            def place_option_order(self, **order):
                sent.append(
                    (
                        order["underlying"],
                        order["strike_price"],
                        order["expiry"],
                        order["option_type"],
                        order["transaction_type"],
                        order["quantity"],
                    )
                )
                return {"orderId": f"DHAN-{order['transaction_type']}-1"}

        candles = falling_then_bouncing()
        executor = LiveExecutor(broker=_Broker(), symbol="NIFTY", armed=True)
        engine = FibTouchLadder(
            FibTouchConfig(
                symbol="NIFTY",
                side="CE",
                mother_timestamp=candles[0].timestamp,
                lot_size=65,
                strike_step=50.0,
            ),
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
            executor=executor,
        )
        with patch("engine.fib_touch_ladder.FIB_TOUCH_LIVE_EXECUTION_ENABLED", True):
            for bar in candles:
                engine.on_candle(bar)
            take_the_turn(engine, candles[-1].timestamp, 24_502)
            self.assertEqual(sent[0][4], "BUY")

            # A restart disarms new entries; it must never disarm an exit.
            executor.armed = False
            killed = engine.kill_and_close(
                Bar(candles[-1].timestamp + timedelta(minutes=2), 24_510, 24_512, 24_500, 24_505)
            )
        self.assertTrue(killed)
        self.assertEqual(engine.status, "KILLED")
        self.assertEqual([row[4] for row in sent], ["BUY", "SELL"])
        # Phil, 2026-08-17, after Kill: "This ladder still reports holdings; it
        # cannot be deleted." The basket was sold and booked; the legs are kept
        # only so the console can show what was bought. They are not open.
        self.assertFalse(engine.holds_position)
        self.assertEqual(engine.open_lots, 0)
        self.assertEqual(engine.get_status()["open_lots"], 0)
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(len(engine.fills), 1, "the legs stay visible")
        self.assertIsNotNone(engine.average_premium, "and the average entry stays drawable")

    def test_arming_is_explicit_and_never_a_default(self):
        import inspect

        signature = inspect.signature(LiveExecutor.__init__)
        self.assertIs(signature.parameters["armed"].default, False)
        self.assertEqual(signature.parameters["armed"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_an_armed_live_executor_sends_a_real_order(self):
        sent = []

        class _Broker:
            def place_option_order(self, **order):
                sent.append(
                    (
                        order["underlying"],
                        order["strike_price"],
                        order["expiry"],
                        order["option_type"],
                        order["transaction_type"],
                        order["quantity"],
                    )
                )
                return {"orderId": "DHAN-1"}

        live = LiveExecutor(broker=_Broker(), symbol="NIFTY", armed=True)
        with patch("engine.fib_touch_ladder.FIB_TOUCH_LIVE_EXECUTION_ENABLED", True):
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


class TrendlineTests(unittest.TestCase):
    """Phil's rule: mother HIGH -> the top red candle's OPEN, before the swing low."""

    def line(self, side="CE"):
        candles = falling_then_bouncing()
        anchor = find_swing_anchor(candles, candles[0].timestamp, side)
        assert anchor is not None
        return find_trendline(candles, candles[0].timestamp, side, anchor), candles, anchor

    def test_a_ce_line_starts_at_the_mother_high(self):
        line, candles, _anchor = self.line()
        self.assertIsNotNone(line)
        assert line is not None
        self.assertEqual(line.start_timestamp, candles[0].timestamp)
        self.assertEqual(line.start_price, 24_780.0)  # the mother's HIGH, not its low

    def test_the_anchor_is_the_top_red_candle_BEFORE_the_swing_low(self):
        line, candles, anchor = self.line()
        assert line is not None
        # Strictly between the mother and the swing low -- an earlier version
        # searched AFTER it, which is the bug Phil caught off his own chart.
        self.assertGreater(line.anchor_timestamp, candles[0].timestamp)
        self.assertLessEqual(line.anchor_timestamp, anchor.low_timestamp)
        bar = next(row for row in candles if row.timestamp == line.anchor_timestamp)
        self.assertLess(bar.close, bar.open)  # red
        self.assertEqual(line.anchor_price, bar.open)

    def test_the_top_red_wins_not_the_nearest_one(self):
        line, candles, _anchor = self.line()
        assert line is not None
        # Bars 1 and 2 are both red before the low; bar 1 opens higher (24,642
        # vs 24,622), so it is the "top red candle".
        self.assertEqual(line.anchor_timestamp, candles[1].timestamp)
        self.assertEqual(line.anchor_price, 24_642.0)

    def test_a_pe_line_starts_at_the_mother_low_and_mirrors(self):
        candles = bars(
            [
                (24_640, 24_660, 24_635, 24_658),  # 0 green <- MOTHER, low 24,635
                (24_658, 24_680, 24_655, 24_675),  # 1 green <- lowest green open
                (24_675, 24_700, 24_670, 24_695),  # 2 green <- swing high 24,700
                (24_695, 24_698, 24_688, 24_690),  # 3 red
                (24_690, 24_692, 24_680, 24_682),  # 4 red   <- HIGH frozen
                (24_682, 24_684, 24_650, 24_652),  # 5 red
                (24_652, 24_654, 24_600, 24_602),  # 6 red
                (24_602, 24_612, 24_600, 24_610),  # 7 green
                (24_610, 24_620, 24_608, 24_618),  # 8 green <- LOW frozen
            ]
        )
        anchor = find_swing_anchor(candles, candles[0].timestamp, "PE")
        assert anchor is not None
        line = find_trendline(candles, candles[0].timestamp, "PE", anchor)
        self.assertIsNotNone(line)
        assert line is not None
        self.assertEqual(line.start_price, 24_635.0)  # the mother's LOW
        self.assertEqual(line.anchor_price, 24_658.0)  # lowest green open before the high

    def test_no_candidate_before_the_swing_means_no_line(self):
        # A mother whose very next bar prints the low leaves no red candle in
        # between, so there is nothing to anchor on and nothing is drawn.
        candles = falling_then_bouncing()
        anchor = find_swing_anchor(candles, candles[0].timestamp, "CE")
        assert anchor is not None
        only_mother = [row for row in candles if row.timestamp <= candles[0].timestamp]
        self.assertIsNone(find_trendline(only_mother, candles[0].timestamp, "CE", anchor))

    def test_it_is_serialisable_for_the_chart(self):
        line, _c, _a = self.line()
        assert line is not None
        payload = line.as_dict()
        for key in ("start_timestamp", "start_price", "anchor_timestamp", "anchor_price"):
            self.assertIn(key, payload)


class _StubBroker:
    """Accepts orders so an ARMED live ladder can actually fill in a test."""

    def __init__(self):
        self.sent = []

    def place_option_order(self, **order):
        self.sent.append(
            (
                order["underlying"],
                order["strike_price"],
                order["expiry"],
                order["option_type"],
                order["transaction_type"],
                order["quantity"],
            )
        )
        return {"orderId": f"DHAN-{len(self.sent)}"}


class PersistenceTests(unittest.TestCase):
    """A ladder has to survive a deploy without resuming real order flow."""

    def open_ladder(self, executor=None):
        engine, candles, _ = ladder()
        if executor is not None:
            engine.executor = executor
        with patch("engine.fib_touch_ladder.FIB_TOUCH_LIVE_EXECUTION_ENABLED", True):
            for bar in candles:
                engine.on_candle(bar)
            take_the_turn(engine, candles[-1].timestamp, 24_502)
        return engine

    def revive(self, engine, executor=None):
        return FibTouchLadder.from_dict(
            engine.to_dict(),
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
            executor=executor,
        )

    def test_a_round_trip_keeps_what_the_ladder_decided(self):
        engine = self.open_ladder()
        back = self.revive(engine)
        self.assertEqual(back.status, engine.status)
        self.assertEqual(back.config.symbol, engine.config.symbol)
        self.assertEqual(back.config.timeframe, engine.config.timeframe)
        self.assertEqual(back.config.levels, engine.config.levels)
        assert back.anchor is not None and engine.anchor is not None
        self.assertEqual(back.anchor.high, engine.anchor.high)
        self.assertEqual(back.anchor.low, engine.anchor.low)
        self.assertEqual(back.anchor.confirmed_at, engine.anchor.confirmed_at)
        self.assertEqual([r.status for r in back.rungs], [r.status for r in engine.rungs])
        self.assertEqual(len(back.fills), len(engine.fills))
        self.assertEqual(back.deployed_inr, engine.deployed_inr)
        self.assertEqual(back.open_lots, engine.open_lots)
        self.assertEqual(back.target_index, engine.target_index)
        self.assertEqual(back.average_index_entry, engine.average_index_entry)

    def test_a_revived_ladder_does_not_rebuy_a_rung_it_already_spent(self):
        engine = self.open_ladder()
        back = self.revive(engine)
        base = engine.fills[-1].timestamp
        # The same L2 touch again: the rung is FILLED, so nothing happens.
        take_the_turn(back, base, 24_502)
        self.assertEqual(len(back.fills), 1)

    def test_a_revived_ladder_still_exits_on_its_target(self):
        engine = self.open_ladder()
        back = self.revive(engine)
        base = engine.fills[-1].timestamp
        # The target is a quarter of the way to the MOTHER (24,780) now, so a
        # bar topping at 24,560 no longer reaches it.
        back.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        self.assertEqual(back.status, "WAITING_NEW_LOW")
        self.assertEqual(back.rounds[-1]["exit_reason"], "target")

    def test_a_restored_fill_remembers_which_fib_it_came_from(self):
        """Fibs stack, so "level 2" alone does not name a rung. A ladder that
        forgets the fib collapses two different buys at two different prices
        into the same key -- which is what happened before `fib_id` was
        persisted: as_dict said F1L2 and the reload said L2."""
        engine = self.open_ladder()
        self.assertEqual(engine.fills[0].rung_key, "F1L2")
        back = self.revive(engine)
        self.assertEqual(back.fills[0].fib_id, engine.fills[0].fib_id)
        self.assertEqual(back.fills[0].rung_key, "F1L2")

    def test_the_entry_bar_guard_survives_so_it_cannot_exit_on_its_own_fill_bar(self):
        engine = self.open_ladder()
        back = self.revive(engine)
        self.assertEqual(back._last_fill_timestamp, engine._last_fill_timestamp)
        # A bar at the SAME stamp as the last fill must still not settle.
        back.on_candle(Bar(engine.fills[-1].timestamp, 24_510, 24_600, 24_505, 24_555))
        self.assertNotEqual(back.status, "CLOSED")

    def test_a_live_ladder_comes_back_UNARMED_however_it_died(self):
        # The rule that matters: a deploy restarts the process, and a restart is
        # not a person deciding to trade real money.
        armed = LiveExecutor(broker=_StubBroker(), symbol="NIFTY", armed=True)
        engine = self.open_ladder(executor=armed)
        self.assertTrue(engine.get_status()["armed"])
        back = self.revive(engine, executor=LiveExecutor(broker=_StubBroker(), symbol="NIFTY"))
        self.assertEqual(back.get_status()["mode"], "live")
        self.assertFalse(back.get_status()["armed"])

    def test_the_snapshot_never_carries_an_armed_flag_at_all(self):
        armed = LiveExecutor(broker=_StubBroker(), symbol="NIFTY", armed=True)
        raw = self.open_ladder(executor=armed).to_dict()
        self.assertEqual(raw["mode"], "live")
        self.assertNotIn("armed", raw)

    def test_a_refused_ladder_resumes_unparked_so_it_re_decides(self):
        engine = self.open_ladder()
        engine.status = "EXECUTION_REFUSED"
        self.assertEqual(self.revive(engine).status, "OPEN")
        engine.fills = []
        engine.status = "EXECUTION_REFUSED"
        self.assertEqual(self.revive(engine).status, "ARMED")

    def test_the_snapshot_is_json_and_carries_a_version(self):
        import json

        raw = self.open_ladder().to_dict()
        self.assertEqual(raw["version"], 1)
        json.loads(json.dumps(raw))  # no datetimes left un-stringified


class MotherBreakTests(unittest.TestCase):
    """Phil: "If mother candle broken, stop the trade" -- but only once the
    ladder has actually bought. Before that the setup has MOVED, not failed."""

    def test_a_mother_broken_before_any_swing_forms_ends_the_campaign(self):
        """The 2-Jun-2026 zombie. A CE mother at the bottom of an up-day is
        closed above on the very next bar -- no swing, no anchor, nothing
        bought. Until 2026-08-17 the break was only looked for AFTER an anchor
        existed, so the campaign idled at WAITING_FOR_SWING for nine sessions
        on a geometry that had already declared itself finished. Phil's
        2026-08-15 ruling (no buys + broken mother = close it, wait for his
        next MC) applies before arming exactly as it does after."""
        candles = falling_then_bouncing()
        engine, _, _ = ladder()
        engine.on_candle(candles[0])  # the mother: high 24,780
        self.assertIsNone(engine.anchor)
        # Straight up through the mother high before any bounce can form.
        engine.on_candle(Bar(candles[0].timestamp + timedelta(minutes=1), 24_700, 24_820, 24_690, 24_810))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertEqual(engine.exit_reason, "mother_broken_no_buys")
        self.assertEqual(engine.fills, [])
        # And it stays ended -- no zombie geometry keeps it alive.
        engine.on_candle(Bar(candles[0].timestamp + timedelta(minutes=2), 24_810, 24_815, 24_600, 24_605))
        self.assertEqual(engine.status, "MOTHER_BROKEN")

    def test_a_close_above_the_mother_high_ends_a_ce_campaign(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        self.assertEqual(len(engine.fills), 1)
        # 24,790 closes above the mother's 24,780 high: thesis gone.
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_700, 24_800, 24_690, 24_790))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertEqual(engine.exit_reason, "mother_broken")
        self.assertIsNotNone(engine.net_pnl)

    def test_a_wick_past_the_mother_is_not_a_break(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        # High 24,800 but the CLOSE is back under 24,780.
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_700, 24_800, 24_690, 24_770))
        self.assertNotEqual(engine.status, "MOTHER_BROKEN")

    def test_a_broken_campaign_ignores_later_candles(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_700, 24_800, 24_690, 24_790))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_790, 24_795, 24_100, 24_110))
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(engine.status, "MOTHER_BROKEN")


class MotherBrokenNoBuysTests(unittest.TestCase):
    """Phil REVERSED the rebase on 2026-08-15.

    His 2026-08-07 rule moved the mother when it broke before any buy, because
    ending there had killed 20 of 24 campaigns with nothing at risk. He has now
    taken that back: "Mother doesn't move if not buy happened, but close the
    trade with no buys.. And wait for my next MC." The engine no longer picks
    its own mother -- he does.
    """

    def unfilled(self):
        """A ladder with structure drawn but nothing bought -- which is exactly
        where the shared fixture ends."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        self.assertEqual(engine.fills, [], "the fixture must not have bought yet")
        self.assertTrue(engine.geometry.fibs, "but the geometry must have drawn")
        return engine, candles[-1].timestamp

    def test_a_break_with_nothing_bought_ends_the_campaign(self):
        engine, base = self.unfilled()
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_700, 24_800, 24_690, 24_790))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertEqual(engine.exit_reason, "mother_broken_no_buys")

    def test_it_closes_flat_rather_than_inventing_a_loss(self):
        """Nothing was bought, so nothing was lost. The books must say zero,
        not None and not a fabricated number."""
        engine, base = self.unfilled()
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_700, 24_800, 24_690, 24_790))
        self.assertEqual(engine.fills, [])
        self.assertEqual(engine.net_pnl, 0.0)
        self.assertEqual(engine.gross_pnl, 0.0)
        self.assertEqual(engine.costs_total, 0.0)

    def test_the_mother_stays_exactly_where_he_typed_it(self):
        engine, base = self.unfilled()
        typed = engine.config.mother_timestamp
        high = engine.mother_high
        for i, (o, h, low, c) in enumerate(
            [
                (24_700, 24_800, 24_690, 24_790),
                (24_790, 24_820, 24_780, 24_810),
                (24_810, 24_900, 24_800, 24_890),
            ],
            start=1,
        ):
            engine.on_candle(Bar(base + timedelta(minutes=i), o, h, low, c))
        self.assertEqual(engine.config.mother_timestamp, typed, "the mother never moves now")
        self.assertEqual(engine.mother_high, high)

    def test_a_broken_campaign_ignores_everything_after(self):
        engine, base = self.unfilled()
        engine.on_candle(Bar(base + timedelta(minutes=1), 24_700, 24_800, 24_690, 24_790))
        stamp = engine.exit_timestamp
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_790, 24_950, 24_780, 24_940))
        self.assertEqual(engine.exit_timestamp, stamp)
        self.assertEqual(engine.fills, [])

    def test_a_break_AFTER_buying_is_the_other_rule_and_still_closes_the_basket(self):
        """The distinction that matters: money out means the basket is SOLD,
        not that the campaign quietly stops with nothing booked."""
        engine, base = self.unfilled()
        # Buy F1L2 at 24,502 first.
        take_the_turn(engine, base, 24_502)
        self.assertTrue(engine.fills, "a rung must have filled")
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_700, 24_800, 24_690, 24_790))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertNotEqual(engine.exit_reason, "mother_broken_no_buys")
        self.assertIsNotNone(engine.net_pnl, "a sold basket books a number")


class TrailingStopTests(unittest.TestCase):
    """Phil: "make a trailing SL to catch the higher move as far as it goes.\""""

    def trailing(self, multiple=1.0):
        # The standard fixture's mother tops at 24,780, and a trailing move has
        # to ride well past that -- which would trip the mother break and test
        # the wrong rule. This one is identical apart from a mother tall enough
        # to stay out of the way.
        candles = bars(
            [
                (24_660, 26_000, 24_640, 24_642),  # 0 red   <- MOTHER, high 26,000
                (24_642, 24_644, 24_620, 24_622),
                (24_622, 24_624, 24_600, 24_602),  # low 24,600
                (24_602, 24_612, 24_600, 24_610),  # green
                (24_610, 24_620, 24_608, 24_618),  # green -> LOW frozen
                (24_618, 24_650, 24_615, 24_645),
                (24_645, 24_700, 24_640, 24_695),  # high 24,700
                (24_695, 24_698, 24_680, 24_682),  # red
                (24_682, 24_684, 24_670, 24_672),  # red
                (24_672, 24_674, 24_560, 24_570),  # TL1 + FIB 1 drawn
                (24_570, 24_640, 24_565, 24_635),
                (24_635, 24_690, 24_630, 24_640),  # tags the line
                (24_640, 24_642, 24_505, 24_510),  # FIB 2 drawn, nothing bought
            ]
        )
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            trailing_stop=True,
            trail_span_multiple=multiple,
        )
        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)])
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        # One buy at F1L2 (24,446). The taller mother changes the line, so this
        # fixture draws ONE fib -- 0 = 24,674, 1 = 24,560, span 114 -- and its
        # L2 sits lower than the standard fixture's. The mother tops at 26,000,
        # so the target is a quarter of the long way back up to it.
        take_the_turn(engine, base, 24_440, sweep_from=24_612)
        return engine, base

    def test_reaching_the_target_arms_the_trail_instead_of_selling(self):
        engine, base = self.trailing()
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_840, 24_505, 24_835))
        self.assertEqual(engine.status, "OPEN", "the target must no longer end the trade")
        self.assertTrue(engine.get_status()["trail_armed"])
        self.assertEqual(engine.get_status()["trail_best"], 24_840.0)

    def test_it_rides_the_move_and_keeps_raising_the_best(self):
        engine, base = self.trailing()
        for i, high in enumerate((24_840, 24_900, 24_980, 25_080), start=2):
            engine.on_candle(Bar(base + timedelta(minutes=i), high - 20, high, high - 30, high - 5))
        self.assertEqual(engine.status, "OPEN")
        self.assertEqual(engine.get_status()["trail_best"], 25_080.0)

    def test_it_leaves_when_price_gives_back_one_span(self):
        engine, base = self.trailing(multiple=1.0)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 25_080, 24_505, 25_070))
        # The rally draws a SECOND fib (0 = 24,642, 1 = 24,505, span 137), and
        # the trail measures off the newest structure -- so best 25,080 puts the
        # stop at 24,943.
        engine.on_candle(Bar(base + timedelta(minutes=3), 25_070, 25_075, 24_900, 24_930))
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        self.assertEqual(engine.rounds[-1]["exit_reason"], "trail_stop")
        self.assertEqual(engine.rounds[-1]["exit_index"], 24_943.0)

    def test_a_wick_through_the_stop_does_not_end_the_move(self):
        engine, base = self.trailing(multiple=1.0)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 25_080, 24_505, 25_070))
        # Low pierces 24,943 but the CLOSE holds above it.
        engine.on_candle(Bar(base + timedelta(minutes=3), 25_070, 25_075, 24_900, 24_990))
        self.assertEqual(engine.status, "OPEN")

    def test_a_tighter_trail_leaves_sooner(self):
        engine, base = self.trailing(multiple=0.25)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 25_080, 24_505, 25_070))
        # Best 25,080, 0.25 of the 137 span = 34.25 -> stop 25,045.75.
        engine.on_candle(Bar(base + timedelta(minutes=3), 25_070, 25_075, 25_030, 25_040))
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        self.assertEqual(engine.rounds[-1]["exit_index"], 25_045.75)

    def test_the_trail_beats_the_plain_target_on_a_move_that_keeps_going(self):
        """The whole point: a quarter-way exit leaves the rest on the table."""
        walk = [(24_510, 25_080, 24_505, 25_070), (25_070, 25_075, 24_900, 24_930)]

        plain, base = self.trailing()
        object.__setattr__(plain.config, "trailing_stop", False)
        for i, row in enumerate(walk, start=2):
            plain.on_candle(Bar(base + timedelta(minutes=i), *row))

        trailed, base2 = self.trailing()
        for i, row in enumerate(walk, start=2):
            trailed.on_candle(Bar(base2 + timedelta(minutes=i), *row))

        self.assertEqual(plain.exit_index, 24_828.5)  # the 0.25 target to the mother, off the stop
        self.assertEqual(trailed.exit_index, 24_943.0)  # rode 108.5 points further
        self.assertGreater(trailed.exit_index, plain.exit_index)


class FlatTargetTests(unittest.TestCase):
    """`deep_target=False` keeps the quarter at every depth."""

    def build(self, deep: bool):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            deep_target=deep,
        )
        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)])
        for bar in candles:
            engine.on_candle(bar)
        # Sweep L2, L3 and L4 in one bar so the deep rule would apply.
        take_the_turn(engine, candles[-1].timestamp, 24_295, sweep_from=24_612)
        return engine

    def test_deep_target_on_asks_for_half(self):
        self.assertEqual(self.build(True).target_fraction, 0.5)

    def test_deep_target_off_keeps_the_quarter(self):
        self.assertEqual(self.build(False).target_fraction, 0.25)

    def test_the_flat_target_is_LOWER_and_so_easier_to_reach(self):
        """Raising the bar when the ladder is deepest is what this measures."""
        deep, flat = self.build(True), self.build(False)
        self.assertEqual(deep.average_index_entry, flat.average_index_entry)
        self.assertLess(flat.target_index, deep.target_index)


class PartialExpiryTests(unittest.TestCase):
    """A basket holds several expiries; each leg settles on its own."""

    def two_expiry_basket(self):
        """One leg expiring 11 Aug, one 18 Aug, both bought and open.

        A ladder started TODAY can no longer reach this state -- the first buy
        locks the expiry (see :class:`ExpiryLockTests`). It survives only in a
        ladder restored from before that rule, so the per-leg settlement below
        still has to be right; releasing the lock between the two fills is how
        that pre-lock ladder is reproduced here.
        """
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        chain = [date(2026, 8, 11), date(2026, 8, 18)]
        holder: dict = {}

        def expiries(on):
            engine = holder.get("engine")
            return chain[1:] if engine is not None and engine.fills else chain

        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=expiries)
        holder["engine"] = engine

        def step(bar):
            engine.on_candle(bar)
            # Clearing the lock after every bar is the OLD engine exactly: each
            # rung re-resolved its own expiry. Nothing else can produce a
            # two-expiry basket now.
            engine.expiry_locked = None

        for bar in candles:
            step(bar)
        base = candles[-1].timestamp
        # Two fills, one per bar: F1L2 at 24,502 then F2L2 at 24,430. The
        # second bar stops above F1L3 (24,404) so the basket stays two legs --
        # with fibs stacking, a deeper sweep would take three.
        # Each buy takes its own turn: collect, two reds, then the rise.
        for _bar in (
            Bar(base + timedelta(seconds=1), 24_502 + 8, 24_502 + 10, 24_502 - 7, 24_502 - 2),
            Bar(base + timedelta(seconds=2), 24_502 - 2, 24_502 - 1, 24_502 - 12, 24_502 - 8),
            Bar(base + timedelta(seconds=3), 24_502 - 8, 24_502 + 6, 24_502 - 9, 24_502 + 4),
        ):
            step(_bar)
        for _bar in (
            Bar(base + timedelta(minutes=1) + timedelta(seconds=1), 24_430 + 8, 24_430 + 10, 24_430 - 7, 24_430 - 2),
            Bar(base + timedelta(minutes=1) + timedelta(seconds=2), 24_430 - 2, 24_430 - 1, 24_430 - 12, 24_430 - 8),
            Bar(base + timedelta(minutes=1) + timedelta(seconds=3), 24_430 - 8, 24_430 + 6, 24_430 - 9, 24_430 + 4),
        ):
            step(_bar)
        return engine

    def test_the_near_expiry_settles_and_the_far_leg_keeps_running(self):
        engine = self.two_expiry_basket()
        self.assertEqual(len(engine.fills), 2)
        self.assertEqual({f.expiry for f in engine.fills}, {date(2026, 8, 11), date(2026, 8, 18)})

        # 11 Aug 15:15: only the leg that actually expired is booked. The
        # bar sits above the pending rungs so it settles without buying.
        engine.on_candle(Bar(datetime(2026, 8, 11, 15, 15), 24_420, 24_425, 24_415, 24_420))
        self.assertEqual(engine.status, "OPEN")
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(engine.fills[0].expiry, date(2026, 8, 18))
        self.assertEqual(len(engine._settled), 1)
        self.assertIsNone(engine.net_pnl, "nothing is booked while a leg is still live")

    def test_the_campaign_ends_when_the_LAST_leg_expires(self):
        engine = self.two_expiry_basket()
        engine.on_candle(Bar(datetime(2026, 8, 11, 15, 15), 24_420, 24_425, 24_415, 24_420))
        engine.on_candle(Bar(datetime(2026, 8, 18, 15, 15), 24_420, 24_425, 24_415, 24_420))
        self.assertEqual(engine.status, "EXPIRED")
        self.assertEqual(engine.fills, [])
        self.assertIsNotNone(engine.net_pnl)

    def test_both_legs_are_in_the_final_pnl_not_just_the_last(self):
        engine = self.two_expiry_basket()
        entries = [(f.strike, f.premium, f.quantity) for f in engine.fills]
        engine.on_candle(Bar(datetime(2026, 8, 11, 15, 15), 24_420, 24_425, 24_415, 24_420))
        engine.on_candle(Bar(datetime(2026, 8, 18, 15, 15), 24_420, 24_425, 24_415, 24_420))
        # Both legs settle at 24,420 -- a price chosen to sit ABOVE every
        # rung still resting, so a settlement bar cannot also buy one.
        expected = sum((max(24_420.0 - strike, 0.0) - premium) * qty for strike, premium, qty in entries)
        self.assertAlmostEqual(engine.gross_pnl, expected, places=2)

    def test_a_leg_with_life_left_is_never_zeroed_by_an_earlier_expiry(self):
        """The bug this rule exists for: -Rs 67,209 on 25 Jun 2026."""
        engine = self.two_expiry_basket()
        far = next(f for f in engine.fills if f.expiry == date(2026, 8, 18))
        engine.on_candle(Bar(datetime(2026, 8, 11, 15, 15), 23_000, 23_005, 22_995, 23_000))
        # The near leg is worthless at 23,000, but the far one is untouched.
        self.assertIn(far, engine.fills)
        self.assertEqual(engine._settled[0][1], 0.0)
        self.assertIsNone(engine.net_pnl)


class ExpiryLockTests(unittest.TestCase):
    """One campaign, one contract series -- fixed by the first buy.

    The 24-Dec-2025 campaign bought five legs on the 30-Dec expiry, watched all
    five die, and then opened an L12 on the 6-Jan expiry: Rs 57,885, the worst
    loss in thirteen months of NIFTY history. A ladder that outlives its own
    contract must stop laddering, not roll.
    """

    def _rolling_chain(self):
        """A chain that would hand out a LATER expiry once a leg is held."""
        chain = [date(2026, 8, 11), date(2026, 8, 18)]
        holder: dict = {}

        def expiries(on):
            engine = holder.get("engine")
            return chain[1:] if engine is not None and engine.fills else chain

        return expiries, holder

    def _laddered(self, expiries, holder):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=expiries)
        holder["engine"] = engine
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        last = take_the_turn(engine, base, 24_502)
        take_the_turn(engine, last.timestamp, 24_404)
        return engine

    def test_every_leg_shares_the_first_buys_expiry(self):
        engine = self._laddered(*self._rolling_chain())
        self.assertGreater(len(engine.fills), 1, "the ladder must actually add rungs for this to prove anything")
        self.assertEqual(
            {fill.expiry for fill in engine.fills},
            {date(2026, 8, 11)},
            "a later rung followed the chain into the next expiry",
        )
        self.assertEqual(engine.expiry_locked, date(2026, 8, 11))

    def test_the_whole_campaign_ends_on_its_one_expiry(self):
        engine = self._laddered(*self._rolling_chain())
        engine.on_candle(Bar(datetime(2026, 8, 11, 15, 15), 24_420, 24_425, 24_415, 24_420))
        self.assertEqual(engine.status, "EXPIRED")
        self.assertEqual(engine.fills, [])
        self.assertIsNotNone(engine.net_pnl, "an expired campaign is booked, not left hanging")

    def test_no_new_rung_is_bought_inside_min_dte(self):
        """The L12-two-days-out leg that made the Rs 57,885 loss."""
        engine = self._laddered(*self._rolling_chain())
        pending = [rung for rung in engine.rungs if rung.status == "PENDING"]
        self.assertTrue(pending, "need an untouched rung left to prove the guard")
        held = len(engine.fills)
        # 10 Aug: one day to the locked expiry, and the index drops far enough
        # to touch every level still pending.
        engine.on_candle(Bar(datetime(2026, 8, 10, 11, 0), 24_000, 24_005, 20_000, 24_000))
        self.assertEqual(len(engine.fills), held, "a rung was bought a day before its own expiry")
        self.assertEqual(
            {rung.status for rung in pending} - {"GAPPED", "COLLECTED"},
            set(),
            "nothing was BOUGHT inside min_dte; reached and jumped rungs are not buys",
        )

    def test_a_campaign_that_never_bought_never_locked_an_expiry(self):
        """The rebase used to free the lock; there is no rebase any more. A
        mother that breaks with nothing bought just ends, and a campaign that
        never bought never committed to a contract in the first place."""
        expiries, holder = self._rolling_chain()
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=expiries)
        holder["engine"] = engine
        for bar in candles:
            engine.on_candle(bar)
        self.assertEqual(engine.fills, [])
        self.assertIsNone(engine.expiry_locked)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_700, 24_800, 24_690, 24_790))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertIsNone(engine.expiry_locked, "nothing was ever committed")

    def test_a_ladder_saved_before_the_lock_comes_back_locked(self):
        engine = self._laddered(*self._rolling_chain())
        raw = engine.to_dict()
        del raw["expiry_locked"]  # written by an older build
        restored = FibTouchLadder.from_dict(
            raw, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 18)]
        )
        self.assertEqual(restored.expiry_locked, date(2026, 8, 11))


class BuyModeSwitchTests(unittest.TestCase):
    """One strategy, one geometry, and a switch for what it buys.

    Phil folded Fib Boundary and Fib Space together on 2026-08-15: "Fold into 1
    with a switch." Everything else -- one lot per rung, the target, the cap --
    is shared. Only the list of prices worth money differs.
    """

    def ladder_in(self, mode):
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            buy_mode=mode,
        )
        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)])
        for bar in candles:
            engine.on_candle(bar)
        return engine, candles[-1].timestamp

    def test_levels_mode_takes_every_level_of_every_fib(self):
        engine, _ = self.ladder_in("levels")
        self.assertEqual(len(engine.rungs), 14, "7 levels on each of 2 fibs")
        self.assertEqual(engine.rungs[0].key, "F1L2")
        self.assertTrue(all(rung.zone_floor is None for rung in engine.rungs))

    def test_convergence_mode_takes_only_where_two_fibs_meet(self):
        engine, _ = self.ladder_in("convergence")
        self.assertEqual(len(engine.rungs), 1, "these two fibs converge exactly once")
        rung = engine.rungs[0]
        # Fib 2's level 1 (its own low, 24,560) meeting fib 1's level 2 (24,502).
        self.assertEqual(rung.index_price, 24_560.0)
        self.assertEqual(rung.zone_label, "1-2")
        self.assertEqual(rung.fib_id, 2)
        self.assertEqual(rung.zone_bottom_fib_id, 1)
        self.assertNotEqual(rung.fib_id, rung.zone_bottom_fib_id, "a boundary needs TWO structures")

    def test_a_wide_space_is_worked_down_to_its_middle(self):
        engine, _ = self.ladder_in("convergence")
        rung = engine.rungs[0]
        self.assertAlmostEqual(rung.zone_floor, (24_560.0 + 24_502.0) / 2, places=2)

    def test_the_two_modes_disagree_about_price_and_agree_about_everything_else(self):
        levels, base = self.ladder_in("levels")
        spaces, _ = self.ladder_in("convergence")
        self.assertNotEqual(
            {r.index_price for r in levels.rungs},
            {r.index_price for r in spaces.rungs},
            "the switch has to change WHICH prices are worth money",
        )
        for engine in (levels, spaces):
            self.assertEqual(engine.mother_high, 24_780.0)
            self.assertEqual(len(engine.geometry.fibs), 2, "same geometry underneath both")

    def test_convergence_buys_one_lot_at_the_top_of_the_space(self):
        engine, base = self.ladder_in("convergence")
        top = max(rung.index_price for rung in engine.rungs)
        take_the_turn(engine, base, top)
        self.assertEqual(len(engine.fills), 1)
        fill = engine.fills[0]
        # The stop the turn armed -- the space is where the money was
        # COLLECTED; the buy happens where the market came back to.
        # Bought at the stop the turn armed, which the engine logs.
        armed = [row for row in engine.events if row["event"] == "turn_armed"][-1]
        self.assertAlmostEqual(fill.index_price, armed["stop"], places=2)
        self.assertEqual(fill.lots, 1, "one lot per rung in both modes")

    def test_an_unknown_mode_is_refused_at_construction(self):
        with self.assertRaises(FibTouchError):
            FibTouchConfig(
                symbol="NIFTY",
                side="CE",
                mother_timestamp=IST_START,
                lot_size=65,
                strike_step=50.0,
                buy_mode="both",
            )

    def test_the_boundary_set_is_narrower_than_the_drawn_ladder(self):
        """The ladder DRAWS L2..L16; only 1, 2, 4 and 8 may converge. Phil
        signed that set off on 2026-08-04 as the one change that improved both
        symbols."""
        self.assertEqual(BOUNDARY_LEVELS, (1, 2, 4, 8))
        self.assertNotEqual(set(BOUNDARY_LEVELS), set(HALVING_LEVELS))


class RestingExitTests(unittest.TestCase):
    """The target sits ON THE BROKER, not in this process.

    Phil, 2026-08-15: "I just wanted an automated target sell placed on the
    broker terminal... If not I'll cut the trade myself." So the sell has to
    fill whether or not the app is running, which means a real resting order --
    and its price has to be a PREMIUM even though the target is an index level.
    Nothing models that: it is measured from this contract's own quotes.
    """

    def turn(self, push, start, level):
        """The three-bar turn, pushed through the premium curve.

        A touch only collects since 2026-08-16; the buy needs two reds under
        the collected line and then a rise back through the first red's close.
        """
        step = timedelta(seconds=1)
        bars = [
            Bar(start + step, level + 8, level + 10, level - 7, level - 2),
            Bar(start + 2 * step, level - 2, level - 1, level - 12, level - 8),
            Bar(start + 3 * step, level - 8, level + 6, level - 9, level + 4),
        ]
        for bar in bars:
            push(bar)
        return bars[-1]

    def moving_premium(self):
        """A premium that actually tracks the index, as a real one does.

        The shared fixture prices everything at a flat 200, which carries no
        information about the relationship at all -- and the engine correctly
        refuses to rest an order on it.
        """
        candles = falling_then_bouncing()
        by_time: dict = {bar.timestamp: bar for bar in candles}

        def premium(when, strike, expiry, side):
            bar = by_time.get(when)
            index = float(bar.close) if bar is not None else 24_500.0
            # A clean 0.5 per index point off a 24,500 base -- a plausible ITM
            # call, and exactly measurable.
            return round(200.0 + 0.5 * (index - 24_500.0), 2)

        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        engine = FibTouchLadder(config, premium_lookup=premium, expiry_source=lambda on: [date(2026, 8, 11)])
        for bar in candles:
            engine.on_candle(bar)

        def push(bar):
            """Feed a bar the premium curve also knows about."""
            by_time[bar.timestamp] = bar
            engine.on_candle(bar)

        return engine, candles, premium, push

    def test_a_flat_premium_rests_nothing_rather_than_guessing(self):
        """No slope in the data means no honest price. A resting sell at a
        fabricated number is worse than none at all."""
        engine, candles, _ = ladder()  # flat 200 premium
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertTrue(engine.fills)
        self.assertEqual(engine.resting_exits, [])

    def test_a_buy_leaves_a_sell_resting_at_a_measured_price(self):
        engine, candles, premium, push = self.moving_premium()
        base = candles[-1].timestamp
        self.turn(push, base, 24_502)
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(len(engine.resting_exits), 1)
        row = engine.resting_exits[0]
        self.assertEqual(row["quantity"], engine.fills[0].quantity)
        self.assertEqual(row["strike"], engine.fills[0].strike)
        # 0.5 per index point, measured from the bar the buy happened on --
        # which since 2026-08-16 is the rise through the stop, not the fall.
        fill = engine.fills[0]
        expected = fill.premium + 0.5 * (engine.target_index - float(engine.history[-1].close))
        self.assertAlmostEqual(row["price"], round(expected, 2), places=2)
        self.assertGreater(row["price"], engine.fills[0].premium, "a target sells ABOVE what was paid")

    def test_the_sell_is_re_priced_when_a_deeper_buy_moves_the_average(self):
        engine, candles, _, push = self.moving_premium()
        base = candles[-1].timestamp
        self.turn(push, base, 24_502)
        first = [dict(row) for row in engine.resting_exits]
        self.turn(push, engine.history[-1].timestamp, 24_430)
        self.assertEqual(len(engine.fills), 2, "a second, deeper buy")
        self.assertEqual(len(engine.resting_exits), 2, "both legs rest")
        self.assertNotEqual(
            [row["order_id"] for row in engine.resting_exits],
            [row["order_id"] for row in first],
            "the old order is pulled and replaced, never left beside the new one",
        )

    def test_selling_the_basket_leaves_nothing_working_on_the_broker(self):
        """A resting target that outlives its own position is a naked sell."""
        engine, candles, _, push = self.moving_premium()
        base = candles[-1].timestamp
        self.turn(push, base, 24_502)
        self.assertTrue(engine.resting_exits)
        push(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        self.assertEqual(engine.status, "WAITING_NEW_LOW", "a banked round parks the mother")
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(engine.resting_exits, [], "the target order goes with the position")

    def test_the_resting_orders_survive_a_restart(self):
        """The whole point: they outlive this process. A ladder that came back
        blind to them would put a second sell on the same coin."""
        engine, candles, _, push = self.moving_premium()
        base = candles[-1].timestamp
        self.turn(push, base, 24_502)
        self.assertTrue(engine.resting_exits)
        raw = json.loads(json.dumps(engine.to_dict()))
        back = FibTouchLadder.from_dict(
            raw, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertEqual(back.resting_exits, engine.resting_exits)
        self.assertEqual(back.get_status()["resting_exits"], engine.resting_exits)

    def test_a_wrong_way_slope_is_refused(self):
        """A CE that gets CHEAPER as the index rises is a stale or crossed
        quote, not a measurement."""
        candles = falling_then_bouncing()
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            intraday_close=False,
        )
        by_time = {bar.timestamp: bar for bar in candles}

        def backwards(when, strike, expiry, side):
            bar = by_time.get(when)
            index = float(bar.close) if bar is not None else 24_500.0
            return round(200.0 - 0.5 * (index - 24_500.0), 2)

        engine = FibTouchLadder(config, premium_lookup=backwards, expiry_source=lambda on: [date(2026, 8, 11)])
        for bar in candles:
            engine.on_candle(bar)

        def push(bar):
            by_time[bar.timestamp] = bar  # the curve has to know every bar
            engine.on_candle(bar)

        self.turn(push, candles[-1].timestamp, 24_502)
        self.assertTrue(engine.fills)
        self.assertEqual(engine.resting_exits, [])


class SingleFibFallbackTests(unittest.TestCase):
    """Phil, 2026-08-16, on a 5m mother that sat ARMED through a 360-point
    fall: "we can draw 2 fibs here correct?" The market had drawn only one, and
    convergence had nothing to pair it with. His own rule covers that case --
    "that time we can follow the single fib levels rule" -- and the merge had
    stopped calling it."""

    def one_fib(self):
        """Stopped at bar 9, where the fixture has drawn its FIRST structure
        and not yet its second -- the market Phil was looking at."""
        engine, candles, _ = ladder(buy_mode="convergence")
        for bar in candles[:10]:
            engine.on_candle(bar)
        self.assertEqual(len(engine.geometry.fibs), 1, "exactly one structure")
        return engine, candles[:10]

    def test_a_lone_fib_still_puts_L4_and_L8_on_the_ladder(self):
        engine, _ = self.one_fib()
        levels = sorted({rung.level for rung in engine.rungs})
        self.assertEqual(levels, [4, 8], "the lone structure's own deep lines")

    def test_and_the_fall_can_actually_buy_them(self):
        engine, candles = self.one_fib()
        deepest = min(rung.index_price for rung in engine.rungs)
        take_the_turn(engine, candles[-1].timestamp, deepest, sweep_from=24_600)
        self.assertEqual(len(engine.fills), 1, "one buy, on the turn")
        self.assertEqual(engine.fills[0].lots, 1)


class IntradayCloseTests(unittest.TestCase):
    """Phil, 2026-08-16: "I need intraday close at 3:15 and normal option to be
    selected from console UI." The 22-Jul replay bought on the 24th and sold on
    the 27th, which is not the scalp he trades."""

    def held(self, **overrides):
        engine, candles, _ = ladder(intraday=True, **overrides)
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertEqual(len(engine.fills), 1)
        return engine

    def _at(self, hh, mm):
        return datetime(2026, 8, 6, hh, mm)

    def test_the_basket_is_sold_at_a_quarter_past_three(self):
        engine = self.held()
        engine.on_candle(Bar(self._at(15, 15), 24_520, 24_525, 24_515, 24_520))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "intraday_close")
        self.assertEqual(len(engine.rounds), 1, "the day's round is booked")
        self.assertIsNotNone(engine.net_pnl)
        # Terminal, so nothing can be added to it tomorrow. (An ended campaign
        # keeps its legs as the record, exactly as a target close does; the
        # status is what says the position is gone.)
        self.assertIn(engine.status, {"CLOSED"})
        engine.on_candle(Bar(self._at(15, 20), 24_520, 24_525, 24_400, 24_410))
        self.assertEqual(len(engine.rounds), 1, "and nothing trades after it")

    def test_it_is_1515_not_1530(self):
        engine = self.held()
        engine.on_candle(Bar(self._at(15, 14), 24_520, 24_525, 24_515, 24_520))
        self.assertEqual(engine.status, "OPEN", "14 minutes past three is still the session")
        engine.on_candle(Bar(self._at(15, 15), 24_520, 24_525, 24_515, 24_520))
        self.assertEqual(engine.status, "CLOSED")

    def test_a_mother_that_bought_nothing_still_ends_with_the_day(self):
        engine, candles, _ = ladder(intraday=True)
        for bar in candles:
            engine.on_candle(bar)
        self.assertEqual(engine.fills, [])
        engine.on_candle(Bar(self._at(15, 15), 24_610, 24_615, 24_605, 24_610))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "intraday_close")

    def test_nothing_is_bought_in_the_closing_window(self):
        """A rung reached at 15:14 must not open a position the next bar has to
        close."""
        engine, candles, _ = ladder(intraday=True)
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, self._at(15, 15), 24_502)
        self.assertEqual(engine.fills, [])
        self.assertEqual(engine.status, "CLOSED")

    def test_the_normal_rule_carries_on_past_the_close(self):
        engine, candles, _ = ladder()  # intraday OFF -- the other option
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        engine.on_candle(Bar(self._at(15, 15), 24_520, 24_525, 24_515, 24_520))
        self.assertEqual(engine.status, "OPEN")
        self.assertEqual(len(engine.fills), 1, "the position is still held")

    def test_the_setting_survives_a_restart(self):
        engine = self.held()
        saved = engine.to_dict()
        back = FibTouchLadder.from_dict(
            saved,
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 8, 11)],
        )
        self.assertTrue(back.config.intraday_close)
        self.assertEqual(back.config.intraday_close_at, INTRADAY_CLOSE_AT)

    def test_the_status_payload_says_which_rule_is_running(self):
        engine = self.held()
        payload = engine.get_status()
        self.assertTrue(payload["intraday_close"])
        self.assertEqual(payload["intraday_close_at"], "15:15")


class RearmOnNewLowTests(unittest.TestCase):
    """One target stops the mother -- unless the market goes lower still.

    Phil, 2026-08-15: "one target , then stop but if market moves down breaking
    the low after target hits, we do it again from the same mother." The low
    that wakes it is the DEEPEST of the whole campaign, which he chose over
    CryptoForge's looser rule of the low standing when the round closed.
    """

    def banked(self, **overrides):
        """A ladder that has bought, hit its target, and parked."""
        engine, candles, _ = ladder(**overrides)
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        return engine, base

    def test_a_target_parks_the_mother_rather_than_ending_it(self):
        engine, _ = self.banked()
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        self.assertEqual(len(engine.rounds), 1)
        self.assertEqual(engine.fills, [], "the round's legs are booked and gone")
        self.assertIsNotNone(engine.net_pnl)

    def test_the_rungs_it_bought_go_back_on_the_ladder(self):
        engine, _ = self.banked()
        self.assertTrue(engine.rungs)
        self.assertTrue(all(rung.status == "PENDING" for rung in engine.rungs))

    def test_a_shallower_dip_does_not_wake_it(self):
        """It has to beat the DEEPEST low, not merely go down a bit."""
        engine, base = self.banked()
        deepest = engine._rearm_below
        assert deepest is not None
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_575, 24_580, deepest + 5, deepest + 10))
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        self.assertEqual(engine.fills, [])

    def test_a_new_deepest_low_trades_the_same_mother_again(self):
        engine, base = self.banked()
        deepest = engine._rearm_below
        assert deepest is not None
        mother_before = engine.config.mother_timestamp
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_575, 24_580, deepest - 5, deepest))
        self.assertEqual(engine.status, "ARMED")
        self.assertEqual(engine.config.mother_timestamp, mother_before, "the SAME mother")
        self.assertIsNone(engine._rearm_below, "and it is live again, not still waiting")

    def test_the_bar_that_wakes_it_does_not_also_buy(self):
        """The low that re-armed the ladder is not itself a touch: the rungs
        went back on in the same instant."""
        engine, base = self.banked()
        deepest = engine._rearm_below
        assert deepest is not None
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_575, 24_580, deepest - 200, deepest - 150))
        self.assertEqual(engine.status, "ARMED")
        self.assertEqual(engine.fills, [], "no fill on the waking bar")

    def test_a_second_round_adds_to_the_campaign_rather_than_replacing_it(self):
        engine, base = self.banked()
        first_net = engine.net_pnl
        deepest = engine._rearm_below
        assert deepest is not None
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_575, 24_580, deepest - 5, deepest))
        # Clear of F3L3 (24,295): the reds have to close under the DEEPEST rung
        # the sweep collected, not merely under the one aimed at.
        take_the_turn(engine, base + timedelta(minutes=4), 24_280, sweep_from=24_500)
        self.assertTrue(engine.fills, "the second round buys")
        engine.on_candle(Bar(base + timedelta(minutes=5), 24_310, 24_700, 24_305, 24_690))
        self.assertEqual(len(engine.rounds), 2)
        self.assertAlmostEqual(
            engine.net_pnl,
            round(sum(row["net_pnl"] for row in engine.rounds), 2),
            places=2,
            msg="campaign P&L is every round, not the last one",
        )
        self.assertNotEqual(engine.net_pnl, first_net)

    def test_turning_the_rule_off_ends_the_campaign_on_one_target(self):
        engine, _ = self.banked(rearm=False)
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "target")

    def test_a_banked_round_keeps_its_own_legs(self):
        """`fills` is the OPEN position and empties when the round banks, so
        the round has to carry what it bought or the console loses it."""
        engine, _ = self.banked()
        self.assertEqual(engine.fills, [])
        legs = engine.rounds[0]["fills"]
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["rung_key"], "F1L2")
        self.assertIn("exit_premium", legs[0], "and what it sold for")

    def test_a_parked_ladder_comes_back_parked(self):
        engine, _ = self.banked()
        raw = json.loads(json.dumps(engine.to_dict()))
        back = FibTouchLadder.from_dict(
            raw, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertEqual(back.status, "WAITING_NEW_LOW")
        self.assertEqual(back._rearm_below, engine._rearm_below)
        self.assertEqual(len(back.rounds), 1)
        self.assertEqual(back.net_pnl, engine.net_pnl)


class DeepTargetTests(unittest.TestCase):
    """Phil: "tune up to 0.5 towards mother candle if the depth is huge.\""""

    def test_a_shallow_ladder_still_asks_for_a_quarter(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        self.assertEqual([f.level for f in engine.fills], [2], "only F1L2 is reached")
        self.assertEqual(engine.target_fraction, 0.25)
        # Bought at the stop, 24,500; mother 24,780 -> a quarter of the 280.
        self.assertAlmostEqual(engine.target_index, 24_570.00, places=2)

    def test_a_fall_THROUGH_level_four_widens_the_target_to_a_half(self):
        """One buy now, but the depth still counts. The fall crossed F1L4 on
        its way to a deeper rung, so it has paid for the bigger move -- reading
        only the level the buy is filed under would hand it the shallow one."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        # A bar sweeping to 24,295 crosses five rungs off BOTH fibs: F1L2
        # 24,502, F2L2 24,430, F1L3 24,404, F1L4 24,306, F2L3 24,300.
        take_the_turn(engine, candles[-1].timestamp, 24_295, sweep_from=24_612)
        self.assertEqual(len(engine.fills), 1)
        fill = engine.fills[0]
        self.assertEqual(fill.level, 3, "filed under the deepest rung reached")
        self.assertIn(4, fill.covered_levels, "and it went through an L4")
        self.assertEqual(engine.target_fraction, DEEP_TARGET_FRACTION, "so it asks for half")

    def test_the_deep_threshold_is_level_four(self):
        self.assertEqual(DEEP_TARGET_FROM_LEVEL, 4)
        self.assertEqual(DEEP_TARGET_FRACTION, 0.5)

    def test_the_status_payload_reports_the_fraction_actually_in_use(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_295, sweep_from=24_612)
        self.assertEqual(engine.get_status()["target_fraction"], 0.5)


class BarsBeforeTheMotherTests(unittest.TestCase):
    """The July-2026 sweep, 2026-08-18: every 5m mother typed after 09:15
    "broke" at 09:15 -- the backtest fed the whole day's 1m bars, and a 09:16
    close above a later mother's high ended a campaign that had not started."""

    def test_an_entry_bar_before_the_mother_cannot_break_it(self):
        engine, candles, _ = ladder(mother_index=2)
        # Give the engine its mother first, the way the geometry stream does.
        engine.on_geometry_candle(candles[2])
        self.assertIsNotNone(engine.mother_high)
        # Now a 1m bar from BEFORE the mother, closing well above its high.
        engine.on_candle(Bar(candles[0].timestamp, 24_700, 24_800, 24_690, 24_790))
        self.assertNotEqual(engine.status, "MOTHER_BROKEN")
        self.assertEqual(engine.history, [], "and it is not part of the campaign's history")

    def test_a_geometry_bar_before_the_mother_is_ignored(self):
        engine, candles, _ = ladder(mother_index=2)
        engine.on_geometry_candle(Bar(candles[0].timestamp, 24_660, 24_780, 24_100, 24_642))
        self.assertIsNone(engine._lowest_low, "a low from before the mother is not the campaign's low")
        self.assertEqual(engine.geometry_history, [])

    def test_the_same_geometry_bar_twice_is_read_once(self):
        """The paper poll re-fetches the stream from the mother's date on every
        tick, and a restart re-feeds it to rebuild the geometry."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_geometry_candle(bar)
        fibs_once = len(engine.geometry.fibs)
        events_once = len(engine.events)
        for bar in candles:
            engine.on_geometry_candle(bar)
        self.assertEqual(len(engine.geometry.fibs), fibs_once)
        self.assertEqual(len(engine.geometry_history), len(candles))
        self.assertEqual(len(engine.events), events_once, "no fib is announced twice")

    def test_a_restart_rebuilds_the_geometry_from_the_stream(self):
        """`to_dict` stores what the ladder decided, not the candles; feeding
        the stream again after `from_dict` must put the same fibs back with the
        same rung keys, and keep every rung's state."""
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        back = FibTouchLadder.from_dict(
            engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertEqual(back.geometry.fibs, [], "the structures are not on disk")
        for bar in candles:
            back.on_geometry_candle(bar)
        self.assertEqual(len(back.geometry.fibs), len(engine.geometry.fibs))
        self.assertEqual([r.key for r in back.rungs], [r.key for r in engine.rungs])
        self.assertEqual([r.status for r in back.rungs], [r.status for r in engine.rungs])
        self.assertTrue(all(r.drawn_at is not None for r in back.rungs), "drawn_at is filled back in")
        self.assertEqual(len(back.fills), 1)


class IntradayEndsTheDayWhateverTheStateTests(unittest.TestCase):
    """The 21-Jul-2026 09:15 5m mother, intraday, drew nothing by 15:15,
    carried overnight and closed at 15:15 on the 22nd. Phil's rule is that an
    intraday mother lives for its own session; tomorrow gets a fresh one."""

    def _at(self, hh, mm):
        return datetime(2026, 8, 6, hh, mm)

    def test_a_mother_with_no_swing_yet_still_ends_at_1515(self):
        engine, candles, _ = ladder(intraday=True)
        engine.on_candle(candles[0])  # the mother, nothing drawn
        self.assertIsNone(engine.anchor)
        engine.on_candle(Bar(self._at(15, 15), 24_650, 24_655, 24_645, 24_650))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "intraday_close")

    def test_a_parked_mother_ends_at_1515_instead_of_waiting_overnight(self):
        engine, candles, _ = ladder(intraday=True)
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        engine.on_candle(Bar(self._at(15, 15), 24_570, 24_575, 24_565, 24_570))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "intraday_close")
        self.assertEqual(len(engine.rounds), 1, "the banked round is kept")
        self.assertIsNotNone(engine.net_pnl)


class MotherBrokenAfterARoundTests(unittest.TestCase):
    def test_a_campaign_that_banked_a_round_is_not_labelled_no_buys(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        base = candles[-1].timestamp
        take_the_turn(engine, base, 24_502)
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, 24_580, 24_505, 24_575))
        self.assertEqual(engine.status, "WAITING_NEW_LOW")
        net = engine.net_pnl
        engine.on_candle(Bar(base + timedelta(minutes=3), 24_575, 24_800, 24_570, 24_790))
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertEqual(engine.exit_reason, "mother_broken")
        self.assertEqual(engine.net_pnl, net, "and the round's money is still the campaign's")

    def test_a_campaign_that_never_bought_still_says_so(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        engine.on_candle(Bar(candles[-1].timestamp + timedelta(minutes=1), 24_575, 24_800, 24_570, 24_790))
        self.assertEqual(engine.exit_reason, "mother_broken_no_buys")


class PersistedTurnAndCoverageTests(unittest.TestCase):
    def test_the_covered_rungs_survive_a_restart(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_295, sweep_from=24_612)
        self.assertIn(4, engine.fills[0].covered_levels)
        back = FibTouchLadder.from_dict(
            engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertEqual(back.fills[0].covered, engine.fills[0].covered)
        self.assertEqual(back.target_fraction, DEEP_TARGET_FRACTION, "the deep target is not lost on restart")

    def test_an_armed_stop_remembers_the_bar_that_armed_it(self):
        engine, candles, _ = ladder()
        for bar in candles:
            engine.on_candle(bar)
        step = timedelta(seconds=1)
        start = candles[-1].timestamp
        engine.on_candle(Bar(start + step, 24_510, 24_512, 24_495, 24_500))
        engine.on_candle(Bar(start + 2 * step, 24_500, 24_501, 24_490, 24_494))
        self.assertIsNotNone(engine._buy_stop)
        back = FibTouchLadder.from_dict(
            engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertEqual(back._stop_bar, engine._stop_bar)


class DeepCarryTests(unittest.TestCase):
    """Phil, 2026-08-18: a ladder that bought MORE than four rungs by 15:15 and
    has not reached its target is held to the next session's 15:15. Measured
    on 5m CE over 22 months: +Rs 1.19L -> +Rs 1.80L, every quarter green."""

    def _at(self, hh, mm, day=6):
        return datetime(2026, 8, day, hh, mm)

    def deep(self, **overrides):
        """An intraday ladder whose ONE buy covered five rungs (F1L2, F2L2,
        F1L3, F1L4, F2L3) -- more than DEEP_CARRY_RUNGS. The switch is OFF by
        default (it measured worse), so these turn it on."""
        overrides.setdefault("deep_carry", True)
        engine, candles, _ = ladder(intraday=True, **overrides)
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_295, sweep_from=24_612)
        self.assertEqual(len(engine.fills), 1)
        self.assertGreater(sum(1 for r in engine.rungs if r.status == "FILLED"), DEEP_CARRY_RUNGS)
        return engine

    def shallow(self):
        engine, candles, _ = ladder(intraday=True)
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)  # one rung
        return engine

    def test_a_deep_basket_is_not_sold_at_1515_on_its_own_day(self):
        engine = self.deep()
        engine.on_candle(Bar(self._at(15, 15), 24_300, 24_305, 24_295, 24_300))
        self.assertEqual(engine.status, "OPEN", "carried, not closed")
        self.assertEqual(len(engine.fills), 1)
        self.assertTrue(any(e["event"] == "deep_carry" for e in engine.events))

    def test_it_is_sold_at_the_next_sessions_1515(self):
        engine = self.deep()
        engine.on_candle(Bar(self._at(15, 15), 24_300, 24_305, 24_295, 24_300))
        engine.on_candle(Bar(self._at(15, 14, day=7), 24_310, 24_315, 24_305, 24_310))
        self.assertEqual(engine.status, "OPEN", "the extra day is still running")
        engine.on_candle(Bar(self._at(15, 15, day=7), 24_310, 24_315, 24_305, 24_310))
        self.assertEqual(engine.status, "CLOSED")
        self.assertEqual(engine.exit_reason, "intraday_close")
        self.assertEqual(len(engine.rounds), 1)

    def test_a_shallow_basket_still_sells_at_1515(self):
        engine = self.shallow()
        engine.on_candle(Bar(self._at(15, 15), 24_520, 24_525, 24_515, 24_520))
        self.assertEqual(engine.status, "CLOSED")

    def test_the_target_still_pays_while_carrying(self):
        engine = self.deep()
        engine.on_candle(Bar(self._at(15, 15), 24_300, 24_305, 24_295, 24_300))
        target = engine.target_index
        assert target is not None
        engine.on_candle(Bar(self._at(10, 0, day=7), 24_400, target + 5, 24_395, target))
        self.assertEqual(engine.rounds[-1]["exit_reason"], "target")

    def test_nothing_new_is_bought_in_the_closing_window_while_carrying(self):
        engine = self.deep()
        engine.on_candle(Bar(self._at(15, 15), 24_300, 24_305, 24_295, 24_300))
        take_the_turn(engine, self._at(15, 16), 24_200, sweep_from=24_300)
        self.assertEqual(len(engine.fills), 1, "the day's buying is done")

    def test_the_switch_turns_it_off_and_off_is_the_default(self):
        engine = self.deep(deep_carry=False)
        engine.on_candle(Bar(self._at(15, 15), 24_300, 24_305, 24_295, 24_300))
        self.assertEqual(engine.status, "CLOSED")
        self.assertFalse(
            FibTouchConfig(
                symbol="NIFTY", side="CE", mother_timestamp=IST_START, lot_size=65, strike_step=50.0
            ).deep_carry
        )

    def test_the_setting_survives_a_restart_and_the_extra_day_is_not_granted_twice(self):
        engine = self.deep()
        engine.on_candle(Bar(self._at(15, 15), 24_300, 24_305, 24_295, 24_300))
        back = FibTouchLadder.from_dict(
            engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertTrue(back.config.deep_carry)
        self.assertEqual(back.config.deep_carry_rungs, DEEP_CARRY_RUNGS)
        # Restarted on the second morning: history is empty, but the date says
        # this is not the mother's day any more.
        back.on_candle(Bar(self._at(15, 15, day=7), 24_310, 24_315, 24_305, 24_310))
        self.assertEqual(back.status, "CLOSED")

    def test_the_status_payload_says_so(self):
        engine = self.deep()
        payload = engine.get_status()
        self.assertTrue(payload["deep_carry"])
        self.assertEqual(payload["deep_carry_rungs"], DEEP_CARRY_RUNGS)


class MaxBuysTests(unittest.TestCase):
    """Phil, 2026-08-18, reading the drawdown: every campaign that reached six
    or more buys lost over 23 months. `max_buys` stops a round adding lots
    after that many fills; what is held runs on to its exit."""

    def two_turns(self, **overrides):
        engine, candles, _ = ladder(**overrides)
        for bar in candles:
            engine.on_candle(bar)
        last = take_the_turn(engine, candles[-1].timestamp, 24_502)
        take_the_turn(engine, last.timestamp, 24_295, sweep_from=24_500)
        return engine

    def test_four_is_the_default_and_zero_means_no_limit(self):
        self.assertEqual(
            FibTouchConfig(
                symbol="NIFTY", side="CE", mother_timestamp=IST_START, lot_size=65, strike_step=50.0
            ).max_buys,
            MAX_BUYS,
        )
        self.assertEqual(MAX_BUYS, 4)
        self.assertEqual(len(self.two_turns().fills), 2)
        self.assertEqual(len(self.two_turns(max_buys=0).fills), 2)

    def test_the_cap_stops_the_next_buy_and_marks_the_rungs(self):
        engine = self.two_turns(max_buys=1)
        self.assertEqual(len(engine.fills), 1, "the second turn bought nothing")
        self.assertEqual(engine.status, "OPEN_CAPPED")
        self.assertTrue([rung.key for rung in engine.rungs if rung.status == "UNFUNDED"])
        self.assertTrue(any(e["event"] == "buy_cap_reached" for e in engine.events))

    def test_a_cap_of_two_lets_both_buys_through(self):
        self.assertEqual(len(self.two_turns(max_buys=2).fills), 2)

    def test_it_survives_a_restart_and_shows_in_the_status(self):
        engine = self.two_turns(max_buys=1)
        self.assertEqual(engine.get_status()["max_buys"], 1)
        back = FibTouchLadder.from_dict(
            engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertEqual(back.config.max_buys, 1)

    def test_the_lot_ramp_buys_one_then_two(self):
        engine = self.two_turns(lot_ramp=True, cap=500_000.0)
        self.assertEqual([f.lots for f in engine.fills], [1, 2])
        self.assertEqual([f.quantity for f in engine.fills], [65, 130])
        self.assertTrue(
            FibTouchLadder.from_dict(
                engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
            ).config.lot_ramp
        )
        self.assertEqual([f.lots for f in self.two_turns().fills], [1, 1], "off by default")

    def test_a_negative_cap_is_refused(self):
        with self.assertRaises(FibTouchError):
            FibTouchConfig(
                symbol="NIFTY", side="CE", mother_timestamp=IST_START, lot_size=65, strike_step=50.0, max_buys=-1
            )


class TrailingPersistenceTests(unittest.TestCase):
    """The exit rule and an ARMED trail must survive a restart: a trailing
    campaign that came back as fixed would be sold at its target on the first
    bar, and one that came back unarmed would re-arm at a lower best."""

    def test_the_rule_and_the_armed_trail_survive_a_restart(self):
        engine, candles, _ = ladder(trailing_stop=True)
        for bar in candles:
            engine.on_candle(bar)
        take_the_turn(engine, candles[-1].timestamp, 24_502)
        target = engine.target_index
        assert target is not None
        base = candles[-1].timestamp
        engine.on_candle(Bar(base + timedelta(minutes=2), 24_510, target + 30, 24_505, target + 25))
        self.assertTrue(engine._trail_armed)
        self.assertEqual(engine._trail_best, target + 30)
        self.assertEqual(engine.status, "OPEN", "the trail rides; nothing sold at the target")
        back = FibTouchLadder.from_dict(
            engine.to_dict(), premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)]
        )
        self.assertTrue(back.config.trailing_stop)
        self.assertTrue(back._trail_armed)
        self.assertEqual(back._trail_best, target + 30)
        self.assertEqual(back.get_status()["trail_stop"], round(target + 30 - back.anchor.span, 2))

    def test_the_status_payload_names_the_stop_only_once_armed(self):
        engine, candles, _ = ladder(trailing_stop=True)
        for bar in candles:
            engine.on_candle(bar)
        payload = engine.get_status()
        self.assertTrue(payload["trailing_stop"])
        self.assertIsNone(payload["trail_stop"])


class ReplayOrderTests(unittest.TestCase):
    """`replay` walks both streams in time order: a geometry bar is knowable
    only once it has CLOSED, so an entry bar inside a still-forming 5m bar
    cannot see the fib that bar will draw. The old geometry-first feed let the
    21-Jul-2026 09:30 5m CE backtest offer zones from a fib drawn the NEXT
    morning, after the campaign had closed."""

    def _engine(self):
        candles = bars(
            [
                (o, h, low, c)
                for (o, h, low, c) in [
                    (24_660, 24_780, 24_640, 24_642),
                    (24_642, 24_644, 24_620, 24_622),
                    (24_622, 24_624, 24_600, 24_602),
                    (24_602, 24_612, 24_600, 24_610),
                    (24_610, 24_620, 24_608, 24_618),
                    (24_618, 24_650, 24_615, 24_645),
                    (24_645, 24_700, 24_640, 24_695),
                    (24_695, 24_698, 24_680, 24_682),
                    (24_682, 24_684, 24_670, 24_672),
                    (24_672, 24_674, 24_560, 24_570),
                    (24_570, 24_640, 24_565, 24_635),
                    (24_635, 24_690, 24_630, 24_640),
                    (24_640, 24_642, 24_505, 24_510),
                ]
            ],
            step_minutes=5,
        )
        config = FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=candles[0].timestamp,
            lot_size=65,
            strike_step=50.0,
            timeframe="5m",
            entry_timeframe="1m",
        )
        engine = FibTouchLadder(config, premium_lookup=lambda *a: 200.0, expiry_source=lambda on: [date(2026, 8, 11)])
        return engine, candles

    def test_an_entry_bar_inside_a_forming_geometry_bar_cannot_see_its_fib(self):
        engine, geometry = self._engine()
        # FIB 1 is drawn on the bar opening at +45 min (index 9), which CLOSES at +50.
        draw_open = geometry[9].timestamp
        # An entry bar at +46 that trades through F1L2 (24,502): the fib is not knowable yet.
        early = Bar(draw_open + timedelta(minutes=1), 24_560, 24_562, 24_495, 24_499)
        engine.replay(geometry, [early])
        self.assertEqual(engine.geometry.fibs, [], "the 5m bar has not closed")
        self.assertEqual([r.status for r in engine.rungs], [])
        # An entry bar at +50 (the close): now the fib exists and the level can be collected.
        on_close = Bar(draw_open + timedelta(minutes=5), 24_560, 24_562, 24_495, 24_499)
        engine.replay(geometry, [early, on_close])
        self.assertEqual(len(engine.geometry.fibs), 1)
        self.assertIn("COLLECTED", [r.status for r in engine.rungs])

    def test_a_one_minute_mother_needs_no_second_stream(self):
        engine, candles, _ = ladder()
        engine.replay([], candles)
        self.assertIsNotNone(engine.anchor)


if __name__ == "__main__":
    unittest.main()
