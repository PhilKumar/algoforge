"""Unit tests for the headless Upstox token auto-refresh.

The undocumented consent posts can't be exercised without live 2FA, so those
are stubbed; everything around them -- TOTP generation, config gating, .env
persistence, validity probing, the cooldown, and the ensure/refresh decision --
is real code under test.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import upstox_token_manager as utm  # noqa: E402


def _resp(status_code, json_body=None, headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.headers = headers or {}
    return r


_FULL_ENV = {
    "UPSTOX_API_KEY": "k",
    "UPSTOX_API_SECRET": "s",
    "UPSTOX_REDIRECT_URI": "https://x/cb",
    "UPSTOX_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    "UPSTOX_PIN": "123456",
    "UPSTOX_MOBILE": "9999999999",
}


class TotpTest(unittest.TestCase):
    def test_generate_totp_is_six_digits(self):
        code = utm.generate_totp("JBSWY3DPEHPK3PXP")
        self.assertRegex(code, r"^\d{6}$")

    def test_empty_secret_raises(self):
        with self.assertRaises(utm.UpstoxTokenError):
            utm.generate_totp("  ")


class ConfigTest(unittest.TestCase):
    def test_missing_config_is_quiet_none(self):
        with patch.dict(os.environ, {k: "" for k in _FULL_ENV}, clear=False):
            self.assertIsNone(utm._config())

    def test_full_config_returns_dict(self):
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            cfg = utm._config()
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["mobile"], "9999999999")


class EnvPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = ROOT / "tests" / "_tmp_upstox.env"
        self.tmp.write_text("FOO=bar\nUPSTOX_ACCESS_TOKEN=old\nBAZ=qux\n")
        self._patch = patch.object(utm, "ENV_PATH", self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.unlink(missing_ok=True)

    def test_updates_existing_line_and_preserves_others(self):
        utm._update_env_token("fresh")
        text = self.tmp.read_text()
        self.assertIn("UPSTOX_ACCESS_TOKEN=fresh\n", text)
        self.assertIn("FOO=bar\n", text)
        self.assertIn("BAZ=qux\n", text)
        self.assertNotIn("old", text)

    def test_appends_when_absent(self):
        self.tmp.write_text("FOO=bar\n")
        utm._update_env_token("fresh")
        self.assertIn("UPSTOX_ACCESS_TOKEN=fresh\n", self.tmp.read_text())


class ValidityProbeTest(unittest.TestCase):
    def test_200_is_valid(self):
        with patch.object(utm.requests, "get", return_value=_resp(200)):
            self.assertTrue(utm.token_is_valid("tok"))

    def test_401_is_invalid(self):
        with patch.object(utm.requests, "get", return_value=_resp(401)):
            self.assertFalse(utm.token_is_valid("tok"))

    def test_the_probe_is_a_data_endpoint_not_the_profile(self):
        """/user/profile answers 401 UDAPI1221 to every host regardless of the
        token; probing it declared a valid token dead on every run."""
        seen = {}

        def get(url, **kw):
            seen["url"] = url
            return _resp(200)

        with patch.object(utm.requests, "get", get):
            utm.token_is_valid("tok")
        self.assertIn("/expired-instruments/expiries", seen["url"])
        self.assertNotIn("/user/profile", seen["url"])

    def test_empty_token_is_invalid(self):
        self.assertFalse(utm.token_is_valid(""))

    def test_network_error_is_not_valid(self):
        with patch.object(utm.requests, "get", side_effect=RuntimeError("boom")):
            self.assertFalse(utm.token_is_valid("tok"))


class AutoGenerateTest(unittest.TestCase):
    def setUp(self):
        utm._last_refresh_at = 0.0

    def test_missing_config_returns_none(self):
        with patch.dict(os.environ, {k: "" for k in _FULL_ENV}, clear=False):
            self.assertIsNone(utm.auto_generate_token(force=True))

    def test_cooldown_blocks_rapid_refresh(self):
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with (
                patch.object(utm, "_headless_authorize", return_value="code123"),
                patch.object(utm, "_exchange_code_for_token", return_value="TOK"),
                patch.object(utm, "_update_env_token") as persist,
            ):
                first = utm.auto_generate_token()
                second = utm.auto_generate_token()  # within cooldown
        self.assertEqual(first, "TOK")
        self.assertIsNone(second)
        persist.assert_called_once_with("TOK")

    def test_headless_failure_falls_back_to_none(self):
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with patch.object(utm, "_headless_authorize", side_effect=utm.UpstoxTokenError("1FA failed")):
                self.assertIsNone(utm.auto_generate_token(force=True))

    def test_full_headless_flow_mocked(self):
        # 1FA -> validateOTPToken; 2FA/PIN ok; authorize -> Location with code;
        # exchange -> access_token. Proves the wiring end to end.
        session = MagicMock()
        session.post.side_effect = [
            _resp(200, {"data": {"validateOTPToken": "vtok"}}),  # 1FA
            _resp(200, {}),  # 2FA
            _resp(200, {}),  # PIN
        ]
        session.get.return_value = _resp(302, headers={"Location": "https://x/cb?code=AUTHCODE&state=1"})
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with (
                patch.object(utm.requests, "Session") as Sess,
                patch.object(utm.requests, "post", return_value=_resp(200, {"access_token": "LIVE"})),
                patch.object(utm, "_update_env_token") as persist,
            ):
                Sess.return_value.__enter__.return_value = session
                token = utm.auto_generate_token(force=True)
        self.assertEqual(token, "LIVE")
        persist.assert_called_once_with("LIVE")


class EnsureFreshTest(unittest.TestCase):
    def setUp(self):
        utm._last_refresh_at = 0.0

    def test_valid_token_is_returned_without_refresh(self):
        with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "good"}, clear=False):
            with (
                patch.object(utm, "token_is_valid", return_value=True),
                patch.object(utm, "auto_generate_token") as gen,
            ):
                self.assertEqual(utm.ensure_fresh_token(), "good")
                gen.assert_not_called()

    def test_dead_token_triggers_refresh(self):
        with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "dead"}, clear=False):
            with (
                patch.object(utm, "token_is_valid", return_value=False),
                patch.object(utm, "auto_generate_token", return_value="new") as gen,
            ):
                self.assertEqual(utm.ensure_fresh_token(), "new")
                gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
