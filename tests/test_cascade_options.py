import unittest
from datetime import date, datetime

from engine.cascade_options import (
    Candle,
    CascadeConfig,
    Contract,
    NiftyContractResolver,
    OneHourCascade,
    OptionCandle,
)


def t(hour: int, minute: int = 15) -> datetime:
    return datetime(2026, 7, 20, hour, minute)


class CascadeOptionsTests(unittest.TestCase):
    def setUp(self):
        self.expiries = [date(2026, 7, 21), date(2026, 7, 28), date(2026, 8, 4), date(2026, 8, 11)]

    def test_resolver_skips_current_week_and_selects_two_itm_mirror(self):
        resolver = NiftyContractResolver(self.expiries, strike_step=50, lot_size=65)
        config = CascadeConfig(mother_timestamp=t(9), mother_high=25000, mother_low=24000)
        ce = resolver.select(t(10), 24876, "CE", config)
        pe = resolver.select(t(10), 24876, "PE", config)
        self.assertEqual(ce.expiry, date(2026, 7, 28))
        self.assertEqual(ce.strike, 24800)
        self.assertEqual(pe.strike, 25000)
        self.assertEqual(ce.lot_size, 65)

    def test_ce_allows_green_between_reds_and_scales_one_two_three(self):
        config = CascadeConfig(mother_timestamp=t(9), mother_high=25000, mother_low=24900)
        resolver = NiftyContractResolver(self.expiries, strike_step=50, lot_size=65)
        candles = [
            Candle(t(10), 24950, 24960, 24880, 24890),  # first red below mother low
            Candle(t(11), 24890, 24920, 24870, 24910),  # green ignored
            Candle(t(12), 24920, 24930, 24860, 24870),  # second red arms at 24890
            Candle(t(13), 24850, 24900, 24840, 24880),  # stop fill, stage 1
            Candle(t(14), 24850, 24870, 24820, 24830),  # first red below marked low
            Candle(t(15), 24830, 24860, 24810, 24820),  # second red arms
            Candle(t(16), 24790, 24860, 24780, 24840),  # stage 2 fill
            Candle(t(17), 24840, 24855, 24750, 24770),  # first red for stage 3
            Candle(t(18), 24770, 24780, 24720, 24730),  # second red arms
            Candle(t(19), 24720, 24810, 24700, 24790),  # stage 3 fill
            Candle(t(20), 24790, 24920, 24780, 24900),  # target
        ]

        def option_lookup(timestamp, contract: Contract):
            prices = {t(13): 100, t(16): 80, t(19): 60, t(20): 90}
            price = prices.get(timestamp)
            return OptionCandle(timestamp, price, price + 2, price - 2, price + 5) if price is not None else None

        result = OneHourCascade(config, resolver, option_lookup).run(candles)
        self.assertEqual(result.status, "closed")
        self.assertEqual([entry.lots for entry in result.entries], [1, 2, 3])
        self.assertEqual([entry.quantity for entry in result.entries], [65, 130, 195])
        self.assertIsNotNone(result.target_index)
        self.assertGreater(result.realized_pnl, 0)

    def test_pe_reverses_colour_and_target_direction(self):
        config = CascadeConfig(mother_timestamp=t(9), mother_high=25000, mother_low=24000, option_type="PE")
        resolver = NiftyContractResolver(self.expiries, strike_step=50, lot_size=65)
        candles = [
            Candle(t(10), 24950, 25050, 24940, 25040),  # green above mother high
            Candle(t(11), 25040, 25070, 25020, 25030),  # red ignored
            Candle(t(12), 25020, 25100, 25010, 25080),  # second green arms
            Candle(t(13), 25050, 25060, 24980, 25000),  # downward stop fill
            Candle(t(14), 25020, 25040, 24970, 24980),  # red ignored
            Candle(t(15), 24980, 25030, 24960, 25020),  # red ignored
            Candle(t(16), 24980, 25120, 24970, 25110),  # first green above marked high
            Candle(t(17), 25110, 25150, 25090, 25140),  # second green arms
            Candle(t(18), 25100, 25120, 25080, 25100),  # stage 2 fill
            Candle(t(19), 25020, 25040, 24500, 24550),  # PE target toward mother low
        ]

        def option_lookup(timestamp, contract: Contract):
            prices = {t(13): 100, t(18): 80, t(19): 120}
            price = prices.get(timestamp)
            return OptionCandle(timestamp, price, price + 5, price - 5, price + 10) if price is not None else None

        result = OneHourCascade(config, resolver, option_lookup).run(candles)
        self.assertEqual(result.status, "closed")
        self.assertEqual([entry.lots for entry in result.entries], [1, 2])
        self.assertLess(result.target_index, result.average_spot)
        self.assertGreater(result.realized_pnl, 0)

    def test_strict_missing_option_candle_blocks_fill(self):
        config = CascadeConfig(mother_timestamp=t(9), mother_high=25000, mother_low=24900)
        resolver = NiftyContractResolver(self.expiries)
        candles = [
            Candle(t(10), 24950, 24960, 24880, 24890),
            Candle(t(11), 24900, 24920, 24870, 24880),
            Candle(t(12), 24870, 24920, 24850, 24890),
        ]
        result = OneHourCascade(config, resolver, lambda _ts, _contract: None).run(candles)
        self.assertEqual(result.status, "data_gap")
        self.assertIn("missing option candle", result.data_gap)


if __name__ == "__main__":
    unittest.main()
