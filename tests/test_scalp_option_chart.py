import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-scalp-option-chart.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-scalp-option-chart-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import app as app_module  # noqa: E402


class _Request:
    def __init__(self, user_id=7):
        self.state = SimpleNamespace(user_id=user_id, current_user={"id": user_id, "role": "user"})


class _Trade:
    def __init__(self, current_premium=128.0):
        self.current_premium = current_premium

    def to_dict(self):
        return {
            "trade_id": 41,
            "underlying": "NIFTY",
            "strike": 25000,
            "option_type": "CE",
            "expiry": "2026-08-13",
            "entry_time": "2026-08-10 09:35:00",
            "entry_premium": 125.5,
            "current_premium": self.current_premium,
            "target_premium": 145.0,
            "sl_premium": 110.0,
        }


class _Broker:
    def __init__(self):
        self.calls = []

    def get_historical_data(self, **kwargs):
        self.calls.append(kwargs)
        return pd.DataFrame(
            {
                "open": [120.0, 125.0],
                "high": [126.0, 131.0],
                "low": [118.0, 123.0],
                "close": [125.0, 128.0],
                "volume": [10, 12],
            },
            index=pd.to_datetime(["2026-08-10 09:30:00", "2026-08-10 09:35:00"]),
        )


class ScalpOptionChartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved_engines = dict(app_module._scalp_engines)
        app_module._scalp_engines.clear()
        app_module._SCALP_OPTION_CHART_CACHE.clear()

    def tearDown(self):
        app_module._scalp_engines.clear()
        app_module._scalp_engines.update(self.saved_engines)
        app_module._SCALP_OPTION_CHART_CACHE.clear()

    async def test_chart_returns_exact_contract_native_candles_and_uses_short_cache(self):
        broker = _Broker()
        app_module._scalp_engines[7] = SimpleNamespace(open_trades={41: _Trade()}, dhan=broker)
        with patch.object(app_module.ScripMaster, "lookup", return_value="555"):
            first = await app_module.get_scalp_option_chart(41, _Request())
            second = await app_module.get_scalp_option_chart(41, _Request())

        self.assertEqual(first["instrument"]["security_id"], "555")
        self.assertEqual(first["instrument"]["strike"], 25000)
        self.assertEqual(first["timeframe"], "5m")
        self.assertEqual(
            first["candles"][0], {"t": 1786334400, "o": 120.0, "h": 126.0, "l": 118.0, "c": 125.0, "v": 10}
        )
        self.assertEqual(first["entries"][0]["price"], 125.5)
        self.assertEqual([line["label"] for line in first["lines"]], ["TARGET", "STOP", "LIVE"])
        self.assertEqual(first["live_price"], 128.0)
        self.assertEqual(len(broker.calls), 1)
        self.assertTrue(second["cached"])
        self.assertEqual(broker.calls[0]["exchange_segment"], "NSE_FNO")
        self.assertEqual(broker.calls[0]["instrument_type"], "OPTIDX")
        self.assertEqual(broker.calls[0]["candle_type"], "5")

    async def test_cached_chart_keeps_its_candles_but_updates_the_live_price(self):
        broker = _Broker()
        trade = _Trade(current_premium=128.0)
        app_module._scalp_engines[7] = SimpleNamespace(open_trades={41: trade}, dhan=broker)
        with patch.object(app_module.ScripMaster, "lookup", return_value="555"):
            first = await app_module.get_scalp_option_chart(41, _Request())
            trade.current_premium = 131.5
            second = await app_module.get_scalp_option_chart(41, _Request())

        self.assertEqual(first["live_price"], 128.0)
        self.assertTrue(second["cached"])
        self.assertEqual(second["live_price"], 131.5)
        self.assertEqual(len(broker.calls), 1)

    async def test_chart_never_constructs_an_engine_for_a_missing_trade(self):
        with (
            patch.object(app_module._db_mod, "get_app_state", AsyncMock(return_value=None)),
            self.assertRaises(app_module.HTTPException) as raised,
        ):
            await app_module.get_scalp_option_chart(999, _Request())
        self.assertEqual(raised.exception.status_code, 404)
        self.assertNotIn(7, app_module._scalp_engines)

    async def test_chart_can_resolve_a_saved_open_trade_without_creating_an_engine(self):
        saved = {"open_trades": [_Trade().to_dict()]}
        with patch.object(app_module._db_mod, "get_app_state", AsyncMock(return_value=json.dumps(saved))):
            trade = await app_module._scalp_open_trade_for_chart(7, 41)
        self.assertEqual(trade["strike"], 25000)
        self.assertNotIn(7, app_module._scalp_engines)


if __name__ == "__main__":
    unittest.main()
