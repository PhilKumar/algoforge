import contextlib
import io
import unittest
from datetime import date

import pandas as pd

from engine.backtest import get_option_contract_lot_size, run_backtest


def _run_backtest(*args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return run_backtest(*args, **kwargs)


class NextMinuteParityTests(unittest.TestCase):
    def test_nifty_lot_size_follows_contract_expiry_cycle(self):
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2024, 4, 25)), 50)
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2024, 5, 2)), 25)
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2024, 12, 26)), 25)
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2025, 1, 2)), 75)
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2025, 1, 30)), 25)
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2025, 2, 6)), 75)
        self.assertEqual(get_option_contract_lot_size("NIFTY", date(2026, 1, 6)), 65)

    def test_next_minute_mode_evaluates_entry_on_strategy_boundary_and_exits_at_next_open(self):
        index = pd.date_range("2026-03-18 09:15", periods=9, freq="1min")
        frame = pd.DataFrame(
            {
                "open": [100, 100, 100, 100, 100, 100, 96, 97, 98],
                "high": [101, 101, 101, 101, 101, 101, 97, 98, 99],
                "low": [99, 99, 99, 99, 99, 99, 95, 96, 97],
                "close": [100, 100, 100, 100, 100, 100, 96, 97, 98],
                "volume": [100] * 9,
            },
            index=index,
        )
        result = _run_backtest(
            frame,
            entry_conditions=[
                {"left": "current_close", "operator": "is_above", "right": "number", "right_number_value": 0}
            ],
            exit_conditions=[
                {"left": "current_close", "operator": "is_below", "right": "number", "right_number_value": 99}
            ],
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 5,
                "fetch_timeframe_minutes": 1,
                "execution_timeframe_minutes": 1,
                "entry_evaluation_timeframe_minutes": 5,
                "signal_exit_next_open": True,
                "lot_size": 1,
                "max_trades_per_day": 1,
            },
        )

        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], "2026-03-18 09:20")
        self.assertEqual(trade["exit_time"], "2026-03-18 09:22")
        self.assertEqual(trade["entry_price"], 100)
        self.assertEqual(trade["exit_price"], 97)
        self.assertEqual(trade["exit_reason"], "Signal")


if __name__ == "__main__":
    unittest.main()
