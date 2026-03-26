import json
import unittest
from unittest.mock import patch

import app as app_module


class _DummyBrokerEmptyProbe:
    def get_funds(self):
        return {"availabelBalance": 12345.67}

    def get_ltp_multi(self, segments):
        return {}

    def get_ohlc_multi(self, segments):
        return {"IDX_I": {}}


class _DummyBrokerFallbackProbe:
    def get_funds(self):
        return {"availabelBalance": 5000}

    def get_ltp_multi(self, segments):
        return {}

    def get_ohlc_multi(self, segments):
        return {"IDX_I": {"13": {"last_price": 24025.0, "ohlc": {"close": 23900.0}}}}


class _DummyBrokerAuthFailure:
    def get_funds(self):
        return {"availabelBalance": 999}

    def get_ltp_multi(self, segments):
        raise Exception(
            'LTP fetch failed: {"data":{"808":"Authentication Failed - Client ID or Token invalid"},"status":"failed"}'
        )

    def get_ohlc_multi(self, segments):
        return {}


class _DummyBrokerProbeError:
    def get_funds(self):
        return {"availabelBalance": 777}

    def get_ltp_multi(self, segments):
        raise Exception("Connection error - unable to reach Dhan servers")

    def get_ohlc_multi(self, segments):
        raise Exception("Connection error - unable to reach Dhan servers")


class _DummyTickerBrokenBroker:
    def _is_configured(self):
        return True

    def get_ohlc_multi(self, segments):
        raise Exception("Authentication Failed - Client ID or Token invalid")


class _DummyTickerWorkingBroker:
    def _is_configured(self):
        return True

    def get_ohlc_multi(self, segments):
        return {
            "IDX_I": {
                "13": {"last_price": 24025.0, "ohlc": {"close": 23900.0}},
                "25": {"last_price": 53010.0, "ohlc": {"close": 52900.0}},
                "49": {"last_price": 12455.0, "ohlc": {"close": 12400.0}},
                "51": {"last_price": 80050.0, "ohlc": {"close": 79900.0}},
            }
        }


class BrokerCheckRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app_module._ticker_cache["data"] = None
        app_module._ticker_cache["timestamp"] = 0

    async def test_check_broker_treats_empty_market_probe_as_connected(self):
        broker = _DummyBrokerEmptyProbe()
        with (
            patch.object(app_module, "_request_broker_context", return_value=({"role": "user"}, broker, "user")),
            patch.object(app_module, "_user_broker_auto_refresh_ready", return_value=False),
        ):
            result = await app_module.check_broker(None)

        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["available_balance"], 12345.67)
        self.assertFalse(result["market_data_ok"])
        self.assertEqual(result["message"], "Broker connection active")

    async def test_check_broker_uses_ohlc_fallback_when_ltp_probe_is_empty(self):
        broker = _DummyBrokerFallbackProbe()
        with (
            patch.object(app_module, "_request_broker_context", return_value=({"role": "user"}, broker, "user")),
            patch.object(app_module, "_user_broker_auto_refresh_ready", return_value=False),
        ):
            result = await app_module.check_broker(None)

        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["market_data_ok"])

    async def test_check_broker_keeps_auth_probe_failures_as_auth_error(self):
        broker = _DummyBrokerAuthFailure()
        with (
            patch.object(app_module, "_request_broker_context", return_value=({"role": "user"}, broker, "user")),
            patch.object(app_module, "_user_broker_auto_refresh_ready", return_value=False),
        ):
            result = await app_module.check_broker(None)

        self.assertEqual(result["status"], "auth_error")
        self.assertFalse(result["market_data_ok"])

    async def test_check_broker_keeps_funds_connection_when_probe_endpoint_errors(self):
        broker = _DummyBrokerProbeError()
        with (
            patch.object(app_module, "_request_broker_context", return_value=({"role": "user"}, broker, "user")),
            patch.object(app_module, "_user_broker_auto_refresh_ready", return_value=False),
        ):
            result = await app_module.check_broker(None)

        self.assertEqual(result["status"], "connected")
        self.assertFalse(result["market_data_ok"])
        self.assertEqual(result["available_balance"], 777.0)

    async def test_get_ticker_falls_back_to_global_broker_when_user_broker_fails(self):
        with (
            patch.object(
                app_module,
                "_request_broker_context",
                return_value=({"role": "user"}, _DummyTickerBrokenBroker(), "user"),
            ),
            patch.object(app_module, "dhan", _DummyTickerWorkingBroker()),
            patch.object(app_module.ScripMaster, "ensure_loaded", return_value=None),
            patch.object(app_module.ScripMaster, "get_nearest_expiry", return_value=""),
            patch.object(app_module, "_fetch_nse_vix", return_value={"price": 12.4, "prev_close": 12.0}),
            patch.object(app_module, "_is_cash_market_closed_ist", return_value=False),
        ):
            response = await app_module.get_ticker(None)

        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source"], "dhan")
        self.assertEqual(payload["broker_source"], "global")
        self.assertEqual(payload["nifty"]["price"], 24025.0)
        self.assertEqual(payload["sensex"]["price"], 80050.0)

    async def test_get_ticker_uses_global_broker_when_context_lookup_fails(self):
        with (
            patch.object(app_module, "_request_broker_context", side_effect=RuntimeError("no request user")),
            patch.object(app_module, "dhan", _DummyTickerWorkingBroker()),
            patch.object(app_module.ScripMaster, "ensure_loaded", return_value=None),
            patch.object(app_module.ScripMaster, "get_nearest_expiry", return_value=""),
            patch.object(app_module, "_fetch_nse_vix", return_value={"price": 12.4, "prev_close": 12.0}),
            patch.object(app_module, "_is_cash_market_closed_ist", return_value=False),
        ):
            response = await app_module.get_ticker(None)

        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["broker_source"], "global")
        self.assertEqual(payload["vix"]["price"], 12.4)


if __name__ == "__main__":
    unittest.main()
