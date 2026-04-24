"""Unit tests for :mod:`app.adapters.storage.envelope`.

Covers the AES-256-GCM envelope primitive introduced by cd-1ai:

* Round-trip: plaintext → ciphertext → plaintext.
* Wire-format version byte pins to ``0x01``; a different first byte
  raises :class:`EnvelopeDecryptError`.
* Purpose binding: ciphertext encrypted under purpose ``"A"`` fails
  to decrypt under purpose ``"B"`` (HKDF-Expand separates the key
  streams).
* Tampered ciphertext fails the AEAD tag check.
* Empty / missing root key surfaces the :class:`KeyDerivationError`
  from :func:`app.auth.keys.derive_subkey`.

See ``docs/specs/15-security-privacy.md`` §"Secret envelope".
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeDecryptError
from app.auth.keys import KeyDerivationError

_KEY = SecretStr("x" * 32)  # 32 bytes of high-entropy-ish input


class TestAes256GcmEnvelope:
    """Round-trip + negative paths for :class:`Aes256GcmEnvelope`."""

    def test_round_trip(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        plaintext = b"https://www.airbnb.com/ical/secret-token.ics"
        ciphertext = env.encrypt(plaintext, purpose="ical-feed-url")
        # Ciphertext is opaque — never contains the plaintext.
        assert plaintext not in ciphertext
        # First byte is the version marker.
        assert ciphertext[0] == 0x01
        # Decrypt recovers the original exactly.
        assert env.decrypt(ciphertext, purpose="ical-feed-url") == plaintext

    def test_fresh_nonce_per_call(self) -> None:
        """Two encrypts of the same plaintext produce different ciphertexts.

        AES-GCM's security hinges on nonce uniqueness per key — the
        impl uses :func:`os.urandom` for every call; this test
        indirectly asserts that the nonce isn't deterministic.
        """
        env = Aes256GcmEnvelope(_KEY)
        plaintext = b"same-input"
        ct1 = env.encrypt(plaintext, purpose="p")
        ct2 = env.encrypt(plaintext, purpose="p")
        assert ct1 != ct2

    def test_purpose_mismatch_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"secret", purpose="purpose-a")
        with pytest.raises(EnvelopeDecryptError):
            env.decrypt(ciphertext, purpose="purpose-b")

    def test_blank_purpose_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        with pytest.raises(ValueError, match="non-blank"):
            env.encrypt(b"secret", purpose="")
        with pytest.raises(ValueError, match="non-blank"):
            env.encrypt(b"secret", purpose="   ")

    def test_tampered_ciphertext_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"secret", purpose="p")
        # Flip one bit in the body (past the version + nonce bytes).
        tampered = bytearray(ciphertext)
        tampered[20] ^= 0x01
        with pytest.raises(EnvelopeDecryptError):
            env.decrypt(bytes(tampered), purpose="p")

    def test_wrong_version_byte_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"secret", purpose="p")
        mutated = b"\x02" + ciphertext[1:]
        with pytest.raises(EnvelopeDecryptError, match="unknown envelope version"):
            env.decrypt(mutated, purpose="p")

    def test_truncated_ciphertext_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        # Anything shorter than version + nonce + tag (1 + 12 + 16) is
        # structurally invalid.
        with pytest.raises(EnvelopeDecryptError, match="too short"):
            env.decrypt(b"\x01short", purpose="p")

    def test_empty_root_key_raises_derivation_error(self) -> None:
        env = Aes256GcmEnvelope(SecretStr(""))
        with pytest.raises(KeyDerivationError):
            env.encrypt(b"secret", purpose="p")

    def test_different_purposes_produce_different_ciphertext(self) -> None:
        """HKDF-Expand separates key streams — different purpose → different key."""
        env = Aes256GcmEnvelope(_KEY)
        ct_a = env.encrypt(b"same", purpose="a")
        ct_b = env.encrypt(b"same", purpose="b")
        # Nonce randomness also contributes; what matters for the
        # security argument is that decrypt under the wrong purpose
        # fails — see ``test_purpose_mismatch_rejected`` — but the
        # byte-inequality assertion is a handy quick sanity check.
        assert ct_a != ct_b
