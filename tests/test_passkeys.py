"""Passkeys — the ceremonies that must be accepted, and the ones that must not.

Every check here is about the LOGIN, not about which brand of authenticator
was used: attestation is deliberately not verified (see webauthn_auth's module
docstring), so these tests pin the things that actually keep an attacker out —
challenge, origin, RP ID, the biometric flag, the signature and the counter.

The authenticator is synthesised with a real P-256 key, so a passing signature
test means the same arithmetic a phone performs.
"""

import hashlib
import json
import os
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

import webauthn_auth as wa

RP_ID = "philforge.in"
ORIGIN = "https://philforge.in"


class FakeAuthenticator:
    """A stand-in for the Secure Enclave: same shapes, same signatures."""

    def __init__(self, rp_id: str = RP_ID):
        self.rp_id = rp_id
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)

    def cose_key(self) -> dict:
        numbers = self.private_key.public_key().public_numbers()
        return {
            1: 2,
            3: wa.COSE_ES256,
            -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }

    def auth_data(self, *, flags: int, sign_count: int, attested: bool) -> bytes:
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + sign_count.to_bytes(4, "big")
        if attested:
            data += (
                b"\x00" * 16
                + len(self.credential_id).to_bytes(2, "big")
                + self.credential_id
                + wa._cose_to_bytes(self.cose_key())
            )
        return data

    @staticmethod
    def client_data(ceremony: str, challenge: bytes, origin: str = ORIGIN) -> bytes:
        return json.dumps({"type": ceremony, "challenge": wa.b64url_encode(challenge), "origin": origin}).encode()

    def register(self, challenge: bytes, *, verified: bool = True) -> dict:
        flags = wa.FLAG_USER_PRESENT | wa.FLAG_ATTESTED_CREDENTIAL_DATA
        if verified:
            flags |= wa.FLAG_USER_VERIFIED
        auth_data = self.auth_data(flags=flags, sign_count=0, attested=True)
        attestation = (
            b"\xa3"
            + b"\x63fmt"
            + b"\x64none"
            + b"\x67attStmt"
            + b"\xa0"
            + b"\x68authData"
            + wa._cbor_write_head(2, len(auth_data))
            + auth_data
        )
        return {
            "id": wa.b64url_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": wa.b64url_encode(self.client_data("webauthn.create", challenge)),
                "attestationObject": wa.b64url_encode(attestation),
            },
        }

    def authenticate(
        self, challenge: bytes, *, sign_count: int = 1, verified: bool = True, origin: str = ORIGIN
    ) -> dict:
        flags = wa.FLAG_USER_PRESENT | (wa.FLAG_USER_VERIFIED if verified else 0)
        auth_data = self.auth_data(flags=flags, sign_count=sign_count, attested=False)
        client_data = self.client_data("webauthn.get", challenge, origin)
        signature = self.private_key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": wa.b64url_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": wa.b64url_encode(client_data),
                "authenticatorData": wa.b64url_encode(auth_data),
                "signature": wa.b64url_encode(signature),
            },
        }


class PasskeyRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeAuthenticator()
        self.challenge = wa.new_challenge()

    def test_a_real_device_registers(self):
        stored = wa.verify_registration(
            credential=self.device.register(self.challenge),
            expected_challenge=self.challenge,
            rp_id=RP_ID,
            origin=ORIGIN,
        )
        self.assertEqual(wa.b64url_decode(stored["credential_id"]), self.device.credential_id)
        self.assertTrue(stored["public_key"])

    def test_registration_without_a_biometric_is_refused(self):
        with self.assertRaises(wa.WebAuthnError):
            wa.verify_registration(
                credential=self.device.register(self.challenge, verified=False),
                expected_challenge=self.challenge,
                rp_id=RP_ID,
                origin=ORIGIN,
            )

    def test_a_different_sites_passkey_is_refused(self):
        other_site = FakeAuthenticator(rp_id="evil.example")
        with self.assertRaises(wa.WebAuthnError):
            wa.verify_registration(
                credential=other_site.register(self.challenge),
                expected_challenge=self.challenge,
                rp_id=RP_ID,
                origin=ORIGIN,
            )


class PasskeyAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeAuthenticator()
        registration_challenge = wa.new_challenge()
        self.stored = wa.verify_registration(
            credential=self.device.register(registration_challenge),
            expected_challenge=registration_challenge,
            rp_id=RP_ID,
            origin=ORIGIN,
        )
        self.challenge = wa.new_challenge()

    def _verify(self, credential, *, challenge=None, origin=ORIGIN, stored_count=0):
        return wa.verify_authentication(
            credential=credential,
            expected_challenge=self.challenge if challenge is None else challenge,
            rp_id=RP_ID,
            origin=origin,
            public_key_b64=self.stored["public_key"],
            stored_sign_count=stored_count,
        )

    def test_a_real_signature_is_accepted(self):
        self.assertEqual(self._verify(self.device.authenticate(self.challenge, sign_count=7)), 7)

    def test_a_replayed_challenge_is_refused(self):
        credential = self.device.authenticate(self.challenge)
        with self.assertRaises(wa.WebAuthnError):
            self._verify(credential, challenge=wa.new_challenge())

    def test_a_phished_origin_is_refused(self):
        """The whole point of passkeys: a lookalike site cannot use one."""
        credential = self.device.authenticate(self.challenge, origin="https://philforge.in.evil.example")
        with self.assertRaises(wa.WebAuthnError):
            self._verify(credential)

    def test_a_missing_biometric_is_refused(self):
        credential = self.device.authenticate(self.challenge, verified=False)
        with self.assertRaises(wa.WebAuthnError):
            self._verify(credential)

    def test_a_tampered_signature_is_refused(self):
        credential = self.device.authenticate(self.challenge)
        signature = bytearray(wa.b64url_decode(credential["response"]["signature"]))
        signature[-1] ^= 0xFF
        credential["response"]["signature"] = wa.b64url_encode(bytes(signature))
        with self.assertRaises(wa.WebAuthnError):
            self._verify(credential)

    def test_a_cloned_authenticator_is_refused(self):
        """A counter that fails to advance is the only clone signal there is."""
        credential = self.device.authenticate(self.challenge, sign_count=4)
        with self.assertRaises(wa.WebAuthnError):
            self._verify(credential, stored_count=4)

    def test_a_counter_that_never_moves_is_still_allowed(self):
        """Apple and Google authenticators always report 0. That is legal."""
        credential = self.device.authenticate(self.challenge, sign_count=0)
        self.assertEqual(self._verify(credential, stored_count=0), 0)

    def test_another_devices_key_cannot_sign_for_this_one(self):
        impostor = FakeAuthenticator()
        impostor.credential_id = self.device.credential_id
        with self.assertRaises(wa.WebAuthnError):
            self._verify(impostor.authenticate(self.challenge))


if __name__ == "__main__":
    unittest.main()
