"""Charts calls leave paced and briefly cached, so six loops cost one request.

The sweep of 24-26 Aug: ~3,200 charts 429s and ~2,400 marketfeed 429s, whose
retry sleeps and breaker-openings starved every strategy poll at once (1,300
"circuit breaker is OPEN" skips). The loops mostly ask the SAME question --
NIFTY, one timeframe, today -- inside the same second.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import dhan as dhan_mod  # noqa: E402


def _fresh_state():
    dhan_mod._candles_cache.clear()
    dhan_mod._charts_last_call = 0.0
    dhan_mod._circuit_breaker.record_success()


class _FakeResp:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [10],
            "timestamp": [1756185600],
        }


class _StubClient(dhan_mod.DhanClient):
    """Just enough client to reach get_historical_data's fetch path.

    `headers` and friends are read-only properties on the real class, so the
    stub shadows them with plain class attributes instead of setting instance
    ones.
    """

    data_url = "https://api.dhan.co/v2"
    headers = {}
    _allow_token_refresh = False

    def __init__(self):
        pass

    def refresh_access_token(self):
        return None

    def _is_configured(self):
        return True


def _client():
    return _StubClient()


class ChartsPacingTests(unittest.TestCase):
    def setUp(self):
        _fresh_state()
        self.calls = 0
        self._orig = dhan_mod._request_with_retry

        def fake(method, url, **kw):
            self.calls += 1
            return _FakeResp()

        dhan_mod._request_with_retry = fake

    def tearDown(self):
        dhan_mod._request_with_retry = self._orig
        _fresh_state()

    def _fetch(self, c, **kw):
        args = dict(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date="2026-08-26",
            to_date="2026-08-26",
            candle_type="5",
        )
        args.update(kw)
        return c.get_historical_data(**args)

    def test_the_same_question_twice_costs_one_request(self):
        c = _client()
        a = self._fetch(c)
        b = self._fetch(c)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(a), len(b))

    def test_a_different_question_is_a_different_request(self):
        c = _client()
        self._fetch(c)
        self._fetch(c, candle_type="15")
        self.assertEqual(self.calls, 2)

    def test_the_cache_expires(self):
        c = _client()
        self._fetch(c)
        with dhan_mod._candles_cache_lock:
            for k, (t, df) in list(dhan_mod._candles_cache.items()):
                dhan_mod._candles_cache[k] = (t - dhan_mod._CANDLES_CACHE_TTL - 1, df)
        self._fetch(c)
        self.assertEqual(self.calls, 2)

    def test_a_cached_answer_survives_an_open_breaker(self):
        """A burst of 429s must not blind every strategy at once any more."""
        c = _client()
        self._fetch(c)
        for _ in range(dhan_mod._circuit_breaker._threshold):
            dhan_mod._circuit_breaker.record_failure()
        self.assertEqual(dhan_mod._circuit_breaker.state, "open")
        served = self._fetch(c)  # cache, no network, no raise
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(served), 1)

    def test_a_cache_hit_is_a_copy(self):
        c = _client()
        a = self._fetch(c)
        a.iloc[0, 0] = -1.0
        b = self._fetch(c)
        self.assertNotEqual(float(b.iloc[0, 0]), -1.0)

    def test_calls_leave_spaced(self):
        dhan_mod._throttle_charts()
        t0 = time.monotonic()
        dhan_mod._throttle_charts()
        self.assertGreaterEqual(time.monotonic() - t0, dhan_mod._CHARTS_MIN_INTERVAL * 0.9)


class MarketfeedFloorTests(unittest.TestCase):
    def test_the_floor_is_always_on(self):
        dhan_mod.enable_marketfeed_throttle(False)
        dhan_mod._mf_last_call = 0.0
        dhan_mod._throttle_marketfeed()
        t0 = time.monotonic()
        dhan_mod._throttle_marketfeed()
        self.assertGreaterEqual(time.monotonic() - t0, dhan_mod._MF_FLOOR_INTERVAL * 0.9)


if __name__ == "__main__":
    unittest.main()
