"""The two-red ladder on cash equity, tested against the rules that pay for it.

Each test here maps to a finding from the 36-month backtest, because those are
the behaviours that turned a rule which wins nearly every trade and nets zero
into one that books money.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from engine.candle_ladder import LadderCandle
from engine.two_red_equity import (
    DP_PER_SELL_DAY,
    TwoRedEquityConfig,
    TwoRedEquityError,
    TwoRedEquityInstrument,
    TwoRedEquityPaperEngine,
    complete_weeks,
    find_mothers,
    ladder_for,
    regroup_weekly,
)

IST_START = datetime(2026, 3, 2, 9, 15)


def _instrument() -> TwoRedEquityInstrument:
    return TwoRedEquityInstrument(symbol="reliance", name="Reliance", security_id="2885")


def _mother(high: float = 1000.0) -> LadderCandle:
    return LadderCandle(
        timeframe="1d",
        timestamp=IST_START,
        open=high - 20,
        high=high,
        low=high - 30,
        close=high - 10,
    )


def _bar(index: int, o: float, h: float, low: float, c: float, timeframe: str = "1h") -> LadderCandle:
    return LadderCandle(
        timeframe=timeframe,
        timestamp=IST_START + timedelta(hours=index),
        open=o,
        high=h,
        low=low,
        close=c,
    )


def _engine(**overrides) -> TwoRedEquityPaperEngine:
    config = TwoRedEquityConfig(
        capital_inr=overrides.pop("capital_inr", 200_000.0),
        start_timeframe=overrides.pop("start_timeframe", "1h"),
        **overrides,
    )
    return TwoRedEquityPaperEngine(_instrument(), _mother(), config)


def _walk_down_to(engine: TwoRedEquityPaperEngine, price: float, start_index: int = 1) -> int:
    """Drift price down to `price` on bars that are not two-red pairs.

    Green bars only, so nothing arms on the way down -- the tests that follow
    want to control exactly which pair of reds the ladder sees.
    """
    index = start_index
    level = 995.0
    while level > price:
        level = max(price, level - 10)
        engine.on_candle(_bar(index, level - 1, level + 0.5, level - 1.5, level))
        index += 1
    return index


class TestFirstBuyGate:
    """The 8% gate is the finding: shallower than that and fees eat the target."""

    def test_two_reds_just_under_the_mother_do_not_buy(self):
        engine = _engine()
        # A 1% dip, exactly the case that used to fund a Rs 1,600 position.
        engine.on_candle(_bar(1, 995, 996, 990, 991))
        engine.on_candle(_bar(2, 991, 992, 988, 989))
        engine.on_candle(_bar(3, 989, 996, 989, 995))  # recovers through the stop
        assert engine.ladder.fills == []
        assert engine.status == "WATCHING"

    def test_the_same_setup_fills_once_price_is_far_enough_down(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))  # takes out 896
        assert len(engine.ladder.fills) == 1
        assert engine.status == "HOLDING"

    def test_a_blocked_rung_keeps_waiting_rather_than_dying(self):
        """Returning zero shares is a wait, not a refusal -- it must fill later."""
        engine = _engine()
        engine.on_candle(_bar(1, 995, 996, 990, 991))
        engine.on_candle(_bar(2, 991, 992, 988, 989))
        engine.on_candle(_bar(3, 989, 992, 989, 991))
        assert engine.status == "WATCHING"
        index = _walk_down_to(engine, 900.0, start_index=4)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        assert len(engine.ladder.fills) == 1

    def test_the_gate_is_configurable_down_to_nothing(self):
        engine = _engine(min_fall_pct=0.0)
        # A red is only a red against a PRIOR close on the same chart, so the
        # first bar the engine ever sees can never be one of the pair. The
        # other tests get this for free from the walk down.
        engine.on_candle(_bar(1, 993, 996, 992, 995))
        engine.on_candle(_bar(2, 995, 996, 990, 991))
        engine.on_candle(_bar(3, 991, 992, 988, 989))
        engine.on_candle(_bar(4, 989, 996, 989, 995))
        assert len(engine.ladder.fills) == 1


class TestFundingRule:
    """Down x% from the mother commits x% of the purse."""

    def test_size_follows_the_fall(self):
        engine = _engine(capital_inr=100_000.0)
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        fill = engine.ladder.fills[0]
        # Filled at 896, which is 10.4% under a 1000 mother.
        expected_fraction = (1000.0 - 896.0) / 1000.0
        assert fill.quantity == int(100_000.0 * expected_fraction / 896.0)

    def test_a_bigger_purse_buys_proportionally_more(self):
        small = _engine(capital_inr=100_000.0)
        big = _engine(capital_inr=400_000.0)
        for engine in (small, big):
            index = _walk_down_to(engine, 900.0)
            engine.on_candle(_bar(index, 900, 901, 895, 896))
            engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
            engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        # Not exactly 4x: a share is whole, so each size truncates on its own
        # and the remainders do not scale. Close enough is the honest claim.
        assert big.ladder.fills[0].quantity == pytest.approx(small.ladder.fills[0].quantity * 4, rel=0.06)
        assert big.ladder.fills[0].quantity > small.ladder.fills[0].quantity


class TestMotherVoid:
    """The trap that read -12% to -27% a year before it was fixed."""

    def test_a_close_above_the_mother_voids_an_unfilled_setup(self):
        engine = _engine()
        engine.on_candle(_bar(1, 995, 996, 990, 991))
        engine.on_candle(_bar(2, 991, 1005, 991, 1004))
        assert engine.status == "VOID"
        assert engine.ended_reason == "mother_reclaimed"

    def test_a_voided_campaign_ignores_everything_after(self):
        engine = _engine()
        engine.on_candle(_bar(1, 995, 1005, 995, 1004))
        index = _walk_down_to(engine, 900.0, start_index=2)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        assert engine.ladder.fills == []
        assert engine.status == "VOID"

    def test_a_filled_basket_survives_a_close_above_the_mother(self):
        """Once bought, the basket stays -- its target sits under the mother."""
        engine = _engine(target_fraction=1.0)
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 899, 891, 898))
        assert len(engine.ladder.fills) == 1
        assert engine.status == "HOLDING"


class TestTarget:
    def test_the_target_is_three_quarters_of_the_way_back(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        entry = engine.ladder.average_entry
        assert engine.ladder.target_index == pytest.approx(entry + 0.75 * (1000.0 - entry), abs=0.01)

    def test_reaching_the_target_closes_the_campaign(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        target = engine.ladder.target_index
        engine.on_candle(_bar(index + 3, 899, target + 5, 899, target + 4))
        assert engine.status == "CLOSED"
        assert engine.ladder.exit_reason == "target"

    def test_the_shipped_quarter_target_is_still_available(self):
        engine = _engine(target_fraction=0.25)
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        entry = engine.ladder.average_entry
        assert engine.ladder.target_index == pytest.approx(entry + 0.25 * (1000.0 - entry), abs=0.01)


class TestMoney:
    def test_nothing_bought_means_no_pnl_not_a_zero(self):
        """A confident 0.00 reads as a flat result; absent is not flat."""
        engine = _engine()
        assert engine.money_at(950.0) is None
        assert engine.get_status(950.0)["realised"] is None

    def test_the_depository_charge_is_in_the_net(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        money = engine.money_at(950.0)
        assert money is not None
        assert money.dp_charge == pytest.approx(DP_PER_SELL_DAY)
        assert money.net_pnl == pytest.approx(money.gross_pnl - money.charges - money.dp_charge, abs=0.01)

    def test_a_flat_exit_loses_exactly_the_costs(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        money = engine.money_at(896.0)  # out where it went in
        assert money is not None
        assert money.gross_pnl == pytest.approx(0.0, abs=0.01)
        assert money.net_pnl < 0

    def test_realised_appears_only_once_closed(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        assert engine.realised is None
        target = engine.ladder.target_index
        engine.on_candle(_bar(index + 3, 899, target + 5, 899, target + 4))
        assert engine.realised is not None
        assert engine.realised.net_pnl > 0


class TestPersistence:
    def test_a_holding_campaign_round_trips(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        restored = TwoRedEquityPaperEngine.from_dict(engine.to_dict())
        assert restored.status == "HOLDING"
        assert restored.quantity == engine.quantity
        assert restored.ladder.average_entry == engine.ladder.average_entry
        assert restored.ladder.target_index == engine.ladder.target_index

    def test_a_restored_campaign_does_not_buy_its_first_rung_again(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        restored = TwoRedEquityPaperEngine.from_dict(engine.to_dict())
        before = len(restored.ladder.fills)
        restored.on_candle(_bar(index + 3, 899, 900, 894, 895))
        restored.on_candle(_bar(index + 4, 895, 896, 889, 890))
        restored.on_candle(_bar(index + 5, 890, 899, 890, 898))
        assert len(restored.ladder.fills) == before

    def test_a_closed_campaign_round_trips_with_its_money(self):
        engine = _engine()
        index = _walk_down_to(engine, 900.0)
        engine.on_candle(_bar(index, 900, 901, 895, 896))
        engine.on_candle(_bar(index + 1, 896, 897, 890, 891))
        engine.on_candle(_bar(index + 2, 891, 900, 891, 899))
        target = engine.ladder.target_index
        engine.on_candle(_bar(index + 3, 899, target + 5, 899, target + 4))
        restored = TwoRedEquityPaperEngine.from_dict(engine.to_dict())
        assert restored.status == "CLOSED"
        assert restored.realised is not None
        assert restored.realised.net_pnl == pytest.approx(engine.realised.net_pnl, abs=0.01)


class TestWeeklyBars:
    def test_days_fold_into_iso_weeks(self):
        # Mon 2 Mar to Fri 6 Mar 2026, then the Monday after.
        daily = [
            LadderCandle("1d", datetime(2026, 3, 2), 100, 110, 95, 105),
            LadderCandle("1d", datetime(2026, 3, 3), 105, 112, 100, 108),
            LadderCandle("1d", datetime(2026, 3, 6), 108, 115, 90, 112),
            LadderCandle("1d", datetime(2026, 3, 9), 112, 120, 111, 119),
        ]
        weekly = regroup_weekly(daily)
        assert len(weekly) == 2
        assert weekly[0].open == 100
        assert weekly[0].high == 115
        assert weekly[0].low == 90
        assert weekly[0].close == 112
        assert weekly[0].timeframe == "1w"

    def test_a_holiday_short_week_is_still_one_week(self):
        daily = [
            LadderCandle("1d", datetime(2026, 3, 3), 100, 110, 95, 105),
            LadderCandle("1d", datetime(2026, 3, 5), 105, 112, 100, 108),
        ]
        assert len(regroup_weekly(daily)) == 1

    def test_the_running_week_is_not_offered(self):
        """A bar that has not closed must never be acted on."""
        daily = [
            LadderCandle("1d", datetime(2026, 3, 2), 100, 110, 95, 105),
            LadderCandle("1d", datetime(2026, 3, 9), 105, 112, 100, 108),
        ]
        weekly = regroup_weekly(daily)
        done = complete_weeks(weekly, date(2026, 3, 10))  # inside the 9 Mar week
        assert len(done) == 1
        assert done[0].timestamp == datetime(2026, 3, 2)


class TestMotherFinder:
    """The piece that makes the page usable: which mothers are still live."""

    @staticmethod
    def _run_up(start_day: int, base: float, bars: int = 6, warmup: int = 20) -> list:
        """Warm-up bars, then a run of consecutive higher highs.

        The warm-up is not padding: `find_run_mothers` skips any bar whose ATR
        is not yet defined, and ATR needs `atr_period` (14) bars behind it. A
        fixture without it finds nothing and looks like a broken detector.
        """
        day = datetime(2026, 1, 1) + timedelta(days=start_day)
        flat = [
            LadderCandle("1d", day + timedelta(days=i), base - 40, base - 32, base - 48, base - 36)
            for i in range(warmup)
        ]
        run = [
            LadderCandle(
                "1d",
                day + timedelta(days=warmup + i),
                base + i * 10,
                base + i * 10 + 8,
                base + i * 10 - 4,
                base + i * 10 + 6,
            )
            for i in range(bars)
        ]
        return flat + run

    @staticmethod
    def _after(rows: list, offset: int) -> datetime:
        return rows[-1].timestamp + timedelta(days=offset)

    def test_a_mother_that_fell_far_enough_reads_ready(self):
        rows = self._run_up(0, 900.0)
        top = rows[-1].high
        # Drift well below the gate without ever closing back above the high.
        rows += [
            LadderCandle(
                "1d",
                self._after(rows, 1 + i),
                top - 20 - i * 20,
                top - 15 - i * 20,
                top - 30 - i * 20,
                top - 25 - i * 20,
            )
            for i in range(8)
        ]
        found = find_mothers(rows, min_fall_pct=8.0)
        assert found
        assert found[0].state == "ready"
        assert found[0].fall_pct >= 8.0

    def test_a_mother_price_climbed_back_over_reads_spent(self):
        rows = self._run_up(0, 900.0)
        top = rows[-1].high
        reclaim_at = self._after(rows, 2)
        rows += [
            LadderCandle("1d", self._after(rows, 1), top - 20, top - 15, top - 30, top - 25),
            LadderCandle("1d", reclaim_at, top - 20, top + 30, top - 25, top + 25),
        ]
        found = find_mothers(rows, min_fall_pct=8.0)
        assert found
        assert found[0].state == "spent"
        assert found[0].reclaimed_at == reclaim_at

    def test_a_shallow_fall_reads_waiting_not_ready(self):
        rows = self._run_up(0, 900.0)
        top = rows[-1].high
        rows += [LadderCandle("1d", self._after(rows, 1 + i), top - 5, top - 3, top - 9, top - 7) for i in range(4)]
        found = find_mothers(rows, min_fall_pct=8.0)
        assert found
        assert found[0].state == "waiting"
        assert found[0].fall_pct < 8.0

    def test_the_fall_is_measured_only_up_to_the_reclaim(self):
        """A crash AFTER the mother was reclaimed belongs to a later structure."""
        rows = self._run_up(0, 900.0)
        top = rows[-1].high
        rows += [
            LadderCandle("1d", self._after(rows, 1), top - 20, top - 15, top - 30, top - 25),
            LadderCandle("1d", self._after(rows, 2), top - 20, top + 40, top - 25, top + 35),
            # A 40% collapse, but it is not this mother's fall.
            LadderCandle("1d", self._after(rows, 3), top * 0.7, top * 0.72, top * 0.6, top * 0.62),
        ]
        found = find_mothers(rows, min_fall_pct=8.0)
        assert found[0].state == "spent"
        assert found[0].fall_pct < 8.0

    def test_daily_bars_are_not_dropped_by_the_same_session_guard(self):
        """A daily bar IS a session; the intraday guard would find zero here."""
        rows = self._run_up(0, 900.0)
        rows += [LadderCandle("1d", self._after(rows, 1), 940, 945, 930, 935)]
        assert find_mothers(rows, min_fall_pct=8.0)

    def test_newest_first(self):
        rows = self._run_up(0, 900.0)
        rows += self._run_up(60, 1200.0)
        rows += [LadderCandle("1d", self._after(rows, 1), 1200, 1205, 1190, 1195)]
        found = find_mothers(rows, min_fall_pct=8.0)
        assert len(found) >= 2
        assert found[0].timestamp > found[-1].timestamp

    def test_too_short_a_series_returns_nothing_rather_than_raising(self):
        assert find_mothers([LadderCandle("1d", datetime(2026, 3, 2), 1, 2, 0.5, 1.5)]) == []


class TestConfig:
    def test_the_ladder_climbs_to_weekly_from_an_hour(self):
        assert ladder_for("1h") == ("1h", "1d", "1w")

    def test_an_unknown_start_is_refused(self):
        with pytest.raises(TwoRedEquityError):
            ladder_for("3m")

    def test_capital_must_be_positive(self):
        with pytest.raises(TwoRedEquityError):
            TwoRedEquityConfig(capital_inr=0)

    def test_a_target_beyond_the_mother_is_refused(self):
        with pytest.raises(TwoRedEquityError):
            TwoRedEquityConfig(capital_inr=100_000, target_fraction=1.5)
