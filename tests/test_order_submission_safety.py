"""Regression checks for no-duplicate live order safeguards."""

import os
import tempfile
import unittest
from unittest.mock import patch

from broker import dhan


class _Response:
    def __init__(self, status_code=503, text="transient failure"):
        self.status_code = status_code
        self.text = text


class _SyncSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        return self.response


class _AsyncClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def request(self, *args, **kwargs):
        self.calls += 1
        return self.response


class OrderSubmissionSafetyTests(unittest.TestCase):
    def test_sync_unsafe_request_does_not_retry_a_transient_response(self):
        session = _SyncSession(_Response())
        with patch.object(dhan, "_http_session", session):
            response = dhan._request_with_retry(
                "POST",
                "https://example.invalid/orders",
                headers={},
                retry_safe=False,
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(session.calls, 1)

    def test_place_order_marks_transient_broker_response_as_ambiguous(self):
        client = dhan.DhanClient(client_id="test-client", access_token="test-token")
        with (
            patch.object(client, "_is_configured", return_value=True),
            patch.object(dhan, "_request_with_retry", return_value=_Response()),
        ):
            with self.assertRaises(dhan.AmbiguousOrderSubmission):
                client.place_order(
                    security_id="1333",
                    exchange_segment="NSE_EQ",
                    transaction_type="BUY",
                    quantity=1,
                )

    def test_active_instance_gate_rejects_a_standby_port(self):
        import app

        with tempfile.TemporaryDirectory() as temp_dir:
            port_file = os.path.join(temp_dir, "active-port")
            with open(port_file, "w", encoding="utf-8") as handle:
                handle.write("8000")
            with patch.dict(
                os.environ,
                {"PHILFORGE_INSTANCE_PORT": "8001", "PHILFORGE_ACTIVE_PORT_FILE": port_file},
                clear=False,
            ):
                self.assertFalse(app._engine_restore_owner_is_active_instance())
            with patch.dict(
                os.environ,
                {"PHILFORGE_INSTANCE_PORT": "8000", "PHILFORGE_ACTIVE_PORT_FILE": port_file},
                clear=False,
            ):
                self.assertTrue(app._engine_restore_owner_is_active_instance())


class AsyncOrderSubmissionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_unsafe_request_does_not_retry_a_transient_response(self):
        client = _AsyncClient(_Response())
        with patch.object(dhan, "_get_async_client", return_value=client):
            response = await dhan._async_request_with_retry(
                "POST",
                "https://example.invalid/orders",
                headers={},
                retry_safe=False,
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
