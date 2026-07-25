import unittest

from cascade_costs import NiftyOptionCostSchedule, calculate_nifty_option_round_costs


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


if __name__ == "__main__":
    unittest.main()
