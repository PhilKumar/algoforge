import unittest

from cascade_costs import (
    NiftyOptionCostSchedule,
    OptionCostFill,
    calculate_nifty_option_basket_round_costs,
    calculate_nifty_option_round_costs,
)


class NiftyOptionCostTests(unittest.TestCase):
    def test_net_costs_include_every_required_component(self):
        costs = calculate_nifty_option_round_costs(
            buy_price=100,
            sell_price=120,
            quantity=65,
            schedule=NiftyOptionCostSchedule(
                brokerage_per_order=10,
                brokerage_per_lot=1,
                sell_stt_rate=0.001,
                exchange_transaction_rate=0.001,
                sebi_rate=0.0001,
                stamp_buy_rate=0.0002,
                gst_rate=0.18,
            ),
        )
        self.assertEqual(costs.buy_turnover, 6500)
        self.assertEqual(costs.sell_turnover, 7800)
        self.assertGreater(costs.stt, 0)
        self.assertGreater(costs.exchange_transaction, 0)
        self.assertGreater(costs.sebi, 0)
        self.assertGreater(costs.stamp, 0)
        self.assertGreater(costs.gst, 0)
        self.assertGreater(costs.total, costs.brokerage)

    def test_basket_costs_charge_every_buy_order_not_a_synthetic_average(self):
        costs = calculate_nifty_option_basket_round_costs(
            buys=[OptionCostFill(100, 65, 1), OptionCostFill(80, 130, 2)],
            sell_price=110,
            sell_quantity=195,
            sell_lots=3,
            schedule=NiftyOptionCostSchedule(
                brokerage_per_order=10,
                brokerage_per_lot=1,
                sell_stt_rate=0.001,
                exchange_transaction_rate=0.001,
                sebi_rate=0.0001,
                stamp_buy_rate=0.0002,
                gst_rate=0.18,
            ),
        )
        self.assertEqual(costs.buy_turnover, 16900)
        self.assertEqual(costs.sell_turnover, 21450)
        # Two buys + one sell, plus six charged lots.
        self.assertEqual(costs.brokerage, 36)


if __name__ == "__main__":
    unittest.main()
