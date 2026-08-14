from datetime import date

import pandas as pd

from engine.backtest import get_option_contract_lot_size, run_backtest


def test_nifty_option_lot_size_follows_contract_expiry_transitions():
    assert get_option_contract_lot_size("NIFTY", date(2024, 4, 25)) == 50
    assert get_option_contract_lot_size("NIFTY", date(2024, 5, 2)) == 25
    assert get_option_contract_lot_size("NIFTY", date(2024, 12, 26)) == 25
    assert get_option_contract_lot_size("NIFTY", date(2025, 1, 2)) == 75
    assert get_option_contract_lot_size("NIFTY", date(2025, 1, 30)) == 25
    assert get_option_contract_lot_size("NIFTY", date(2025, 2, 6)) == 75
    assert get_option_contract_lot_size("NIFTY", date(2025, 12, 30)) == 75
    assert get_option_contract_lot_size("NIFTY", date(2026, 1, 6)) == 65


def test_next_minute_signal_exit_uses_following_minute_open():
    index = pd.date_range("2026-08-03 09:15", periods=9, freq="1min")
    frame = pd.DataFrame(
        {
            "open": [100, 100, 100, 100, 100, 100, 98, 97, 96],
            "high": [101] * 9,
            "low": [95] * 9,
            "close": [100, 100, 100, 100, 100, 100, 98, 96, 95],
            "volume": [1] * 9,
        },
        index=index,
    )
    result = run_backtest(
        frame,
        entry_conditions=[
            {
                "logic": "IF",
                "left": "current_close",
                "operator": "is_above",
                "right": "number",
                "right_number_value": 0,
            }
        ],
        exit_conditions=[
            {
                "logic": "IF",
                "left": "current_close",
                "operator": "is_below",
                "right": "number",
                "right_number_value": 99,
            }
        ],
        strategy_config={
            "instrument": "RELIANCE",
            "lot_size": 1,
            "lots": 1,
            "market_open": "09:15",
            "market_close": "15:25",
            "max_trades_per_day": 1,
            "execution_timeframe_minutes": 1,
            "entry_evaluation_timeframe_minutes": 5,
            "signal_exit_next_open": True,
            "fetch_timeframe_minutes": 1,
            "fee_pct": 0.000001,
        },
    )

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == "2026-08-03 09:20"
    assert trade["exit_time"] == "2026-08-03 09:22"
    assert trade["entry_price"] == 100.0
    assert trade["exit_price"] == 97.0
