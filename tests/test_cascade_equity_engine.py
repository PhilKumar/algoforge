import unittest
from datetime import datetime, timedelta

from engine.cascade_equity import (
    ESCALATION_BARS,
    CashCascadeInstrument,
    CashCascadePaperConfig,
    CashCascadePaperEngine,
    CashCascadeRung,
    cash_budget_to_quantity,
    cash_cascade_reference_symbol,
    next_timeframe_up,
    structure_bucket,
)
from engine.cascade_options import CascadeError, IndexCandle


def ts(offset: int) -> datetime:
    return datetime(2026, 7, 20, 9, 15) + timedelta(minutes=5 * offset)


def candle(offset: int, open_: float, high: float, low: float, close: float) -> IndexCandle:
    return IndexCandle(ts(offset), open_, high, low, close)


def instrument(symbol: str = "NIFTYBEES") -> CashCascadeInstrument:
    return CashCascadeInstrument(
        symbol=symbol,
        name=symbol,
        security_id="10576",
        signal_symbol=cash_cascade_reference_symbol(symbol),
        signal_name="NIFTY 50" if symbol == "NIFTYBEES" else symbol,
        signal_security_id="13" if symbol == "NIFTYBEES" else "10576",
        signal_exchange_segment="IDX_I" if symbol == "NIFTYBEES" else "NSE_EQ",
        signal_instrument_type="INDEX" if symbol == "NIFTYBEES" else "EQUITY",
    )


class CashCascadeReferenceTests(unittest.TestCase):
    def test_only_niftybees_and_bankbees_use_reference_indices(self):
        self.assertEqual(cash_cascade_reference_symbol("NIFTYBEES"), "NIFTY")
        self.assertEqual(cash_cascade_reference_symbol("BANKBEES"), "BANKNIFTY")
        self.assertEqual(cash_cascade_reference_symbol("JUNIORBEES"), "JUNIORBEES")
        self.assertEqual(cash_cascade_reference_symbol("RELIANCE"), "RELIANCE")

    def test_budget_to_quantity_keeps_undersized_cash_as_zero_qty(self):
        self.assertEqual(cash_budget_to_quantity(200, 250), 0)
        self.assertEqual(cash_budget_to_quantity(500, 250), 2)


class CashCascadePaperEngineTests(unittest.TestCase):
    def test_cash_carry_waits_until_next_level_can_buy_whole_shares(self):
        engine = CashCascadePaperEngine(
            candle(0, 100, 100, 99, 99.5),
            candle(0, 250, 250, 249, 249.5),
            instrument(),
            CashCascadePaperConfig(capital_inr=100000),
        )
        engine.rungs = {
            "1:2": CashCascadeRung(1, 2, 95, 200, 1.0, 1000),
            "1:4": CashCascadeRung(1, 4, 92, 300, 1.0, 1000),
        }
        engine.geometry.campaign.state = "MOTHER_BROKEN"

        # Level 2 crosses first. Rs 200 is below one share at Rs 250, so the
        # engine collects it but cannot arm a stop yet.
        engine.on_candle(candle(1, 96, 97, 94, 94.5), candle(1, 250, 251, 249, 250))
        self.assertEqual(engine.pending_inr, 200)
        self.assertIsNone(engine.pending_line)

        # Level 4 adds Rs 300. The Rs 500 total can buy two shares, then two
        # lower red closes arm the reverse stop and the recovery fills.
        engine.on_candle(candle(2, 94.5, 95, 91.5, 91.8), candle(2, 250, 251, 249, 250))
        self.assertEqual(engine.pending_line, 92)
        engine.on_candle(candle(3, 91.8, 92.1, 90.5, 91.0), candle(3, 250, 251, 249, 250))
        engine.on_candle(candle(4, 91.0, 91.2, 89.5, 90.0), candle(4, 250, 251, 249, 250))
        self.assertEqual(engine.pending_stop, 91.0)
        engine.on_candle(candle(5, 90.0, 91.2, 89.8, 90.8), candle(5, 250, 251, 249, 250))

        self.assertEqual(engine.open_quantity, 2)
        self.assertEqual(engine.open_fills[0].spent_inr, 500)
        self.assertEqual(engine.cash_carry_inr, 0)
        self.assertEqual(engine.rungs["1:2"].status, "FILLED")
        self.assertEqual(engine.rungs["1:4"].status, "FILLED")

    def test_target_uses_traded_scrip_mother_high_not_reference_index_high(self):
        engine = CashCascadePaperEngine(
            candle(0, 100, 100, 99, 99.5),
            candle(0, 250, 250, 248, 249),
            instrument(),
            CashCascadePaperConfig(capital_inr=100000),
        )
        engine.rungs = {"1:2": CashCascadeRung(1, 2, 95, 1000, 1.0, 1000)}
        engine.pending_rung_keys = ["1:2"]
        engine.pending_inr = 1000
        engine.pending_line = 95
        engine.pending_stop = 94
        engine.pending_stop_timestamp = ts(1)
        engine.geometry.campaign.state = "MOTHER_BROKEN"

        engine.on_candle(candle(2, 93, 94.5, 92.5, 94), candle(2, 240, 241, 239, 240))

        self.assertEqual(engine.open_quantity, 4)
        # 240 + 0.25 * (250 - 240) = 242.5; the NIFTY signal mother high of
        # 100 must not participate in this BEES target.
        self.assertAlmostEqual(engine.target_price, 242.5)

    def test_daily_frames_restamp_to_the_session_open(self):
        """Dhan stamps daily bars off its feed epoch; the engine reads them as
        09:15 session opens and holds back today's bar until 15:30."""
        import pandas as pd

        idx = pd.to_datetime(["2026-07-28 05:30", "2026-07-29 05:30", "2026-07-30 05:30"])
        frame = pd.DataFrame(
            {"open": [100.0] * 3, "high": [102.0] * 3, "low": [99.0] * 3, "close": [101.0] * 3}, index=idx
        )
        mid_session = datetime(2026, 7, 30, 11, 0)
        rows = CashCascadePaperEngine.normalise_frame(
            frame, mid_session, timeframe_minutes=CashCascadePaperEngine.DAILY_BAR_MINUTES
        )
        self.assertEqual([row.timestamp.strftime("%d %H:%M") for row in rows], ["28 09:15", "29 09:15"])
        after_close = datetime(2026, 7, 30, 15, 30)
        rows = CashCascadePaperEngine.normalise_frame(
            frame, after_close, timeframe_minutes=CashCascadePaperEngine.DAILY_BAR_MINUTES
        )
        self.assertEqual(len(rows), 3)

    def test_mother_break_with_nothing_held_ends_the_campaign(self):
        """PHOENIXLTD 17 Jul: mother broke in 40 minutes with no entry, and the
        campaign read WAITING forever on a chart frozen at the break."""
        engine = CashCascadePaperEngine(
            candle(0, 100, 102, 99, 100.5),
            candle(0, 2060, 2071.1, 2058, 2061),
            instrument("PHOENIXLTD"),
            CashCascadePaperConfig(capital_inr=50000),
        )
        engine.on_candle(candle(1, 100.5, 101, 100, 100.8), candle(1, 2061, 2065, 2060, 2064))
        self.assertEqual(engine.status, "WAITING")
        # Signal high pierces the signal mother high -> geometry MOTHER_BROKEN.
        engine.on_candle(candle(2, 100.8, 102.5, 100.5, 102.2), candle(2, 2064, 2072.5, 2063, 2072))
        self.assertEqual(engine.geometry.campaign.state, "MOTHER_BROKEN")
        self.assertEqual(engine.status, "MOTHER_BROKEN")
        self.assertFalse(engine.get_status()["running"])
        self.assertEqual(engine.events[-1]["event"], "campaign_ended")

    def test_roundtrip_persists_open_cash_campaign(self):
        engine = CashCascadePaperEngine(
            candle(0, 100, 100, 99, 99.5),
            candle(0, 250, 250, 248, 249),
            instrument("RELIANCE"),
            CashCascadePaperConfig(capital_inr=50000, target_fraction=0.3),
        )
        engine.rungs = {"1:2": CashCascadeRung(1, 2, 95, 1000, 1.0, 1000, status="COLLECTED")}
        engine.pending_rung_keys = ["1:2"]
        engine.pending_inr = 1000
        engine.pending_line = 95
        engine.events.append({"timestamp": ts(1).isoformat(), "event": "rung_collected"})

        restored = CashCascadePaperEngine.from_dict(engine.to_dict())

        self.assertEqual(restored.instrument.symbol, "RELIANCE")
        self.assertEqual(restored.config.product_type, "CNC")
        self.assertEqual(restored.pending_inr, 1000)
        self.assertEqual(restored.rungs["1:2"].status, "COLLECTED")


class DailyTimeframeTests(unittest.TestCase):
    """1d is offered by the Terminal picker, so the engine must accept it.

    It did not: app.py maps "1d" to Dhan's "D" interval, the engine already had
    DAILY_BAR_MINUTES, and the UI listed Daily -- but the config validator still
    named only 5m/15m/1h. Choosing Daily raised CascadeError, which nothing in
    app.py catches, so the page returned a bare 500.
    """

    def test_daily_is_accepted(self):
        config = CashCascadePaperConfig(capital_inr=100000, timeframe="1d")
        self.assertEqual(config.timeframe, "1d")

    def test_every_timeframe_the_terminal_offers_is_accepted(self):
        for timeframe in ("5m", "15m", "1h", "4h", "1d"):
            with self.subTest(timeframe=timeframe):
                self.assertEqual(CashCascadePaperConfig(capital_inr=100000, timeframe=timeframe).timeframe, timeframe)

    def test_an_unsupported_timeframe_is_still_refused(self):
        with self.assertRaises(CascadeError):
            CashCascadePaperConfig(capital_inr=100000, timeframe="3m")


class ProductTypeTests(unittest.TestCase):
    """Paper must not be more permissive than live.

    MTF accrues daily interest and carries pledge mechanics; CashMarketCostSchedule
    models neither. A paper MTF campaign would therefore report a profit it did
    not make, and this cascade holds positions for months, so the error compounds.
    engine/cascade_equity_live.py already refuses MTF for that reason.
    """

    def test_cnc_is_accepted(self):
        self.assertEqual(CashCascadePaperConfig(capital_inr=100000).product_type, "CNC")

    def test_mtf_is_refused_with_the_reason(self):
        with self.assertRaises(CascadeError) as caught:
            CashCascadePaperConfig(capital_inr=100000, product_type="MTF")
        self.assertIn("CNC only", str(caught.exception))
        self.assertIn("not modelled", str(caught.exception))

    def test_the_cost_schedule_still_has_no_interest_line(self):
        """If interest is ever modelled, this test should fail and MTF reopen."""
        from engine.cascade_equity import CashMarketCostSchedule

        fields = set(CashMarketCostSchedule().__dataclass_fields__)
        self.assertNotIn("interest_pct", fields)
        self.assertNotIn("mtf_interest_pct", fields)


class TimeframeEscalationTests(unittest.TestCase):
    """A campaign named on 15m must be able to end up drawing weekly candles.

    Phil's rule: he gives the mother, the campaign starts on 15m and works it to
    target however long that takes -- "even if it goes till Week". Frozen on 15m
    a months-old campaign draws every trendline off noise, so the STRUCTURE
    climbs while the mother, the position and the execution stay where they are.
    """

    def engine(self, timeframe: str = "15m") -> CashCascadePaperEngine:
        return CashCascadePaperEngine(
            candle(0, 100, 100, 99, 99.5),
            candle(0, 250, 250, 249, 249.5),
            instrument("RELIANCE"),
            CashCascadePaperConfig(capital_inr=100000, timeframe=timeframe),
        )

    def test_the_ladder_skips_4h_but_a_campaign_started_there_still_climbs(self):
        self.assertEqual(next_timeframe_up("15m"), "1h")
        self.assertEqual(next_timeframe_up("1h"), "1d")
        self.assertEqual(next_timeframe_up("1d"), "1w")
        # 4h is offered as a start and drawn on charts, but an NSE session is
        # 375 minutes so it is not a rung. A campaign started there goes to 1d.
        self.assertEqual(next_timeframe_up("4h"), "1d")
        # The top of the ladder has nowhere to go.
        self.assertIsNone(next_timeframe_up("1w"))

    def test_intraday_buckets_are_measured_from_the_open_not_from_midnight(self):
        """09:15-10:15 is one hourly bar; 09:15 and 09:45 must not straddle two."""
        day = datetime(2026, 7, 20)
        first = structure_bucket(day.replace(hour=9, minute=15), "1h")
        same = structure_bucket(day.replace(hour=9, minute=45), "1h")
        second = structure_bucket(day.replace(hour=10, minute=15), "1h")
        self.assertEqual(first, same)
        self.assertNotEqual(first, second)
        # And the short 15:15-15:30 stub at the end of the session is its own bar.
        self.assertNotEqual(second, structure_bucket(day.replace(hour=15, minute=15), "1h"))

    def test_a_week_is_one_bucket_monday_to_friday(self):
        monday = datetime(2026, 7, 20, 9, 15)
        friday = datetime(2026, 7, 24, 15, 15)
        next_monday = datetime(2026, 7, 27, 9, 15)
        self.assertEqual(structure_bucket(monday, "1w"), structure_bucket(friday, "1w"))
        self.assertNotEqual(structure_bucket(monday, "1w"), structure_bucket(next_monday, "1w"))

    def test_an_unescalated_campaign_passes_candles_straight_through(self):
        """The regression that matters: no lag until something actually climbs.

        If the stepper buffered from the start, every existing campaign would see
        its structure one bar later than it does today and every measured number
        would move for a reason that has nothing to do with the strategy.
        """
        engine = self.engine()
        bar = candle(1, 99, 99.5, 98, 98.2)
        self.assertIs(engine._step_structure(bar), bar)
        self.assertEqual(engine.structure_timeframe, "15m")

    def test_it_climbs_after_200_bars_and_keeps_the_mother(self):
        engine = self.engine()
        mother_high = engine.geometry.campaign.mother_high
        engine.structure_bars = ESCALATION_BARS + 1
        self.assertTrue(engine._maybe_escalate(candle(1, 99, 99, 98, 98.5)))
        self.assertEqual(engine.structure_timeframe, "1h")
        # Counting restarts on the new rung, and the mother is untouched.
        self.assertEqual(engine.structure_bars, 0)
        self.assertEqual(engine.geometry.campaign.mother_high, mother_high)
        self.assertEqual(engine.events[-1]["event"], "escalated")
        self.assertEqual(engine.events[-1]["to_timeframe"], "1h")

    def test_it_does_not_climb_out_from_under_a_resting_buy_stop(self):
        """Mid-arm the structure the order was placed against must not move."""
        engine = self.engine()
        engine.structure_bars = ESCALATION_BARS + 50
        engine.pending_stop = 97.5
        self.assertFalse(engine._maybe_escalate(candle(1, 99, 99, 98, 98.5)))
        self.assertEqual(engine.structure_timeframe, "15m")
        # Once the arm resolves it climbs on the next bar.
        engine.pending_stop = None
        self.assertTrue(engine._maybe_escalate(candle(2, 99, 99, 98, 98.5)))

    def test_a_climbed_campaign_hands_the_geometry_whole_bars_only(self):
        """Four 15m candles make one hourly bar, and it is emitted once, closed."""
        engine = self.engine()
        engine.structure_timeframe = "1h"
        base = datetime(2026, 7, 20, 9, 15)
        quarters = [IndexCandle(base + timedelta(minutes=15 * i), 100 - i, 101 - i, 97 - i, 99 - i) for i in range(4)]
        for bar in quarters:
            self.assertIsNone(engine._step_structure(bar), "an unfinished hour must not reach the geometry")
        # The candle that OPENS the next hour is what closes this one.
        hourly = engine._step_structure(IndexCandle(base + timedelta(minutes=60), 96, 96, 95, 95.5))
        self.assertIsNotNone(hourly)
        self.assertEqual(hourly.timestamp, base)  # stamped at its open
        self.assertEqual(hourly.open, 100)  # first open
        self.assertEqual(hourly.high, 101)  # highest high
        self.assertEqual(hourly.low, 94)  # lowest low (97 - 3)
        self.assertEqual(hourly.close, 96)  # last close (99 - 3)

    def test_the_rung_survives_a_restart(self):
        """A restart used to be invisible; a campaign silently dropping from 1D
        back to 15m under an open position is the worst kind of silent."""
        engine = self.engine()
        engine.structure_timeframe = "1d"
        engine.structure_bars = 37
        engine._step_structure(candle(1, 99, 99.5, 98, 98.2))
        restored = CashCascadePaperEngine.from_dict(engine.to_dict())
        self.assertEqual(restored.structure_timeframe, "1d")
        self.assertEqual(restored.structure_bars, 37)
        self.assertIsNotNone(restored._structure_open)
        self.assertEqual(restored._structure_key, engine._structure_key)

    def test_a_campaign_saved_before_escalation_existed_resumes_where_it_was(self):
        engine = self.engine("1h")
        payload = engine.to_dict()
        payload.pop("structure_timeframe")
        payload.pop("structure_bars")
        restored = CashCascadePaperEngine.from_dict(payload)
        self.assertEqual(restored.structure_timeframe, "1h")
        self.assertEqual(restored.structure_bars, 0)

    def test_the_status_says_which_rung_it_is_on(self):
        engine = self.engine()
        engine.structure_timeframe = "1d"
        engine.structure_bars = 190
        structure = engine.get_status()["structure"]
        self.assertEqual(structure["timeframe"], "1d")
        self.assertEqual(structure["started_on"], "15m")
        self.assertEqual(structure["next_timeframe"], "1w")
        self.assertEqual(structure["bars_to_next"], 11)
        self.assertTrue(structure["escalated"])

    def test_a_fixed_campaign_never_climbs(self):
        """Measured, fixed 1H beats the climbing ladder, so staying put is a
        real choice and not a fallback -- it has to actually hold."""
        engine = CashCascadePaperEngine(
            candle(0, 100, 100, 99, 99.5),
            candle(0, 250, 250, 249, 249.5),
            instrument("RELIANCE"),
            CashCascadePaperConfig(capital_inr=100000, timeframe="1h", escalates=False),
        )
        engine.structure_bars = ESCALATION_BARS * 5
        self.assertFalse(engine._maybe_escalate(candle(1, 99, 99, 98, 98.5)))
        self.assertEqual(engine.structure_timeframe, "1h")
        structure = engine.get_status()["structure"]
        self.assertFalse(structure["climbs"])
        self.assertIsNone(structure["next_timeframe"])
        self.assertEqual(structure["ladder"], ["1h"])

    def test_the_ladder_the_page_draws_starts_at_the_chosen_timeframe(self):
        engine = self.engine("15m")
        self.assertEqual(engine.get_status()["structure"]["ladder"], ["15m", "1h", "1d", "1w"])
        # A campaign started on 1h has a shorter ladder, not the whole one.
        self.assertEqual(self.engine("1h").get_status()["structure"]["ladder"], ["1h", "1d", "1w"])

    def test_a_campaign_saved_before_the_flag_existed_stays_fixed(self):
        """Absent flag must not silently start an old campaign climbing."""
        engine = self.engine()
        payload = engine.to_dict()
        payload["config"].pop("escalates")
        self.assertFalse(CashCascadePaperEngine.from_dict(payload).config.escalates)
