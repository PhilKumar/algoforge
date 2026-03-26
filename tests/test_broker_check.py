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


class BrokerCheckRegressionTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
