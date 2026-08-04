import unittest
from datetime import datetime, timedelta

from engine.cascade_equity import (
    CashCascadeInstrument,
    CashCascadePaperConfig,
    CashCascadePaperEngine,
    CashCascadeRung,
    cash_budget_to_quantity,
    cash_cascade_reference_symbol,
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
        for timeframe in ("5m", "15m", "1h", "1d"):
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
