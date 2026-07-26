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
from engine.cascade_options import IndexCandle


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
