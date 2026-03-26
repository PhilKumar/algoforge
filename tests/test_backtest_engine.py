import contextlib
import io
import os
import unittest
from importlib import import_module

import pandas as pd

from engine.backtest import eval_condition, run_backtest


def _make_ohlcv(
    start: str, closes: list[float], *, freq: str = "1min", opens: list[float] | None = None
) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq=freq)
    open_values = opens or closes
    return pd.DataFrame(
        {
            "open": open_values,
            "high": [max(op, cl) + 0.5 for op, cl in zip(open_values, closes)],
            "low": [min(op, cl) - 0.5 for op, cl in zip(open_values, closes)],
            "close": closes,
            "volume": [100] * len(closes),
        },
        index=index,
    )


def _always_true_conditions():
    return [{"left": "current_close", "operator": "is_above", "right": "number", "right_number_value": 0}]


def _run_backtest(*args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return run_backtest(*args, **kwargs)


def _load_app_module():
    os.environ.setdefault("ALGOFORGE_PIN", "123456")
    os.environ.setdefault("ALGOFORGE_SKIP_STARTUP_JOBS", "1")
    return import_module("app")


class BacktestRegressionTests(unittest.TestCase):
    def test_touches_distinguishes_candle_range_from_close_value(self):
        row = pd.Series(
            {"open": 101.0, "high": 105.0, "low": 100.0, "close": 102.0},
            name=pd.Timestamp("2026-03-18 09:20"),
        )

        self.assertTrue(
            eval_condition(
                row,
                {"left": "current_high", "operator": "touches", "right": "number", "right_number_value": 104},
            )
        )
        self.assertFalse(
            eval_condition(
                row,
                {"left": "current_close", "operator": "touches", "right": "number", "right_number_value": 104},
            )
        )

    def test_touches_detects_series_intersection(self):
        prev_row = pd.Series({"EMA_5_5m": 99.0, "VWAP_5m": 101.0}, name=pd.Timestamp("2026-03-18 09:15"))
        row = pd.Series({"EMA_5_5m": 101.0, "VWAP_5m": 99.0}, name=pd.Timestamp("2026-03-18 09:20"))

        self.assertTrue(eval_condition(row, {"left": "EMA_5_5m", "operator": "touches", "right": "VWAP_5m"}, prev_row))

    def test_signal_exit_closes_on_current_signal_candle(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102, 103])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=_always_true_conditions(),
            strategy_config={"instrument": "RELIANCE", "timeframe_minutes": 1, "lot_size": 1},
        )

        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], "2026-03-18 09:21")
        self.assertEqual(trade["exit_time"], "2026-03-18 09:22")
        self.assertEqual(trade["exit_reason"], "Signal")

    def test_square_off_uses_raw_timestamp_for_derived_timeframe(self):
        df = _make_ohlcv("2026-03-18 15:15", [100, 101, 102, 103, 104, 105, 106])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=[],
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 3,
                "fetch_timeframe_minutes": 1,
                "lot_size": 1,
                "combined_sqoff_time": "15:20",
            },
        )

        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], "2026-03-18 15:18")
        self.assertEqual(trade["exit_time"], "2026-03-18 15:20")
        self.assertEqual(trade["exit_reason"], "SquareOff")

    def test_max_daily_loss_blocks_later_entries(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 100, 95, 96, 97, 98])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=_always_true_conditions(),
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 1,
                "lot_size": 1,
                "max_daily_loss": 4,
                "fee_pct": 0.01,
            },
        )

        self.assertEqual(result["stats"]["total_trades"], 1)
        self.assertTrue(result["stats"]["max_daily_loss_hit"])

    def test_entry_delay_candles_delays_fill(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102, 103, 104])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=[],
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 1,
                "lot_size": 1,
                "combined_sqoff_time": "09:24",
                "entry_delay_candles": 1,
            },
        )

        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], "2026-03-18 09:22")

    def test_signal_exit_delay_candles_delays_signal_fill(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102, 103, 104])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=_always_true_conditions(),
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 1,
                "lot_size": 1,
                "signal_exit_delay_candles": 1,
            },
        )

        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], "2026-03-18 09:21")
        self.assertEqual(trade["exit_time"], "2026-03-18 09:23")
        self.assertEqual(trade["exit_reason"], "Signal")

    def test_analysis_start_ignores_warmup_candles_before_requested_range(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102, 103, 104])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=[],
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 1,
                "lot_size": 1,
                "combined_sqoff_time": "09:24",
                "_analysis_start": "2026-03-18 09:22",
            },
        )

        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], "2026-03-18 09:22")

    def test_analysis_start_after_data_returns_error(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=[],
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 1,
                "lot_size": 1,
                "_analysis_start": "2026-03-19",
            },
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("requested backtest date range", result["message"])


class StrategyRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_mod = _load_app_module()

    def test_runtime_normalization_keeps_empty_exit_conditions(self):
        runtime = self.app_mod._normalize_strategy_runtime(
            ["EMA_20_5m"],
            [{"left": "current_close", "operator": "is_above", "right": "EMA_20_5m"}],
            [],
        )
        self.assertEqual(runtime["exit_conditions"], [])
        self.assertIn("No exit conditions", " ".join(runtime["warnings"]))

    def test_runtime_normalization_auto_adds_indicator_dependencies(self):
        runtime = self.app_mod._normalize_strategy_runtime(
            [],
            [
                {
                    "left": "MACD_12_26_9_15m_histogram",
                    "operator": "is_above",
                    "right": "number",
                    "right_number_value": 0,
                }
            ],
            [],
        )
        self.assertIn("MACD_12_26_9_15m", runtime["indicators"])
        self.assertTrue(any("Auto-added indicator dependencies" in warning for warning in runtime["warnings"]))

    def test_runtime_normalization_uses_legacy_condition_keys(self):
        runtime = self.app_mod._normalize_strategy_runtime(
            ["EMA_20_5m"],
            [{"lhs": "current_close", "operator": "is_above", "rhs": "EMA_20_5m"}],
            [],
        )
        self.assertEqual(runtime["entry_conditions"][0]["left"], "current_close")
        self.assertEqual(runtime["entry_conditions"][0]["right"], "EMA_20_5m")

    def test_estimate_strategy_warmup_expands_for_monthly_cpr(self):
        warmup = self.app_mod._estimate_strategy_warmup_days(["CPR_0.2_0.5_M"])
        self.assertGreaterEqual(warmup, 70)

    def test_spread_and_slippage_adjust_fill_prices(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=[],
            strategy_config={
                "instrument": "RELIANCE",
                "timeframe_minutes": 1,
                "lot_size": 1,
                "combined_sqoff_time": "09:22",
                "spread_bps": 100,
                "entry_slippage_bps": 50,
                "exit_slippage_bps": 50,
            },
        )

        trade = result["trades"][0]
        self.assertAlmostEqual(trade["entry_price"], 102.01, places=2)
        self.assertAlmostEqual(trade["exit_price"], 100.98, places=2)

    def test_capital_enforcement_rejects_short_option_margin(self):
        df = _make_ohlcv("2026-03-18 09:20", [100, 101, 102, 103])
        result = _run_backtest(
            df,
            entry_conditions=_always_true_conditions(),
            exit_conditions=[],
            strategy_config={
                "instrument": "26000",
                "timeframe_minutes": 1,
                "initial_capital": 50000,
                "enforce_capital": True,
                "lot_size": 1,
                "legs": [
                    {
                        "option_type": "CE",
                        "transaction_type": "SELL",
                        "strike_type": "atm",
                        "lots": 1,
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "no_trades")
        self.assertGreaterEqual(result["stats"]["capital_rejections"], 1)


if __name__ == "__main__":
    unittest.main()
