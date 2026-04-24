"""In-memory :class:`~app.adapters.storage.envelope.EnvelopeEncryptor` fake.

Wraps plaintext with a small namespaced prefix so tests can assert the
"encrypted" ciphertext isn't the plaintext, while still being able to
round-trip through the decrypt path. The prefix carries the
``purpose`` so a test that encrypts with ``"A"`` and decrypts with
``"B"`` fails loudly — matching the cryptographic guarantee of the
production :class:`Aes256GcmEnvelope`.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from app.adapters.storage.envelope import EnvelopeDecryptError

__all__ = ["FakeEnvelope"]


_PREFIX = b"fake-envelope::"


class FakeEnvelope:
    """Deterministic :class:`EnvelopeEncryptor` fake.

    Ciphertext layout: ``b"fake-envelope::" + purpose + b"::" + plaintext``.
    Not cryptographically secure — do not use outside tests.
    """

    def encrypt(self, plaintext: bytes, *, purpose: str) -> bytes:
        return _PREFIX + purpose.encode("utf-8") + b"::" + plaintext

    def decrypt(self, ciphertext: bytes, *, purpose: str) -> bytes:
        if not ciphertext.startswith(_PREFIX):
            raise EnvelopeDecryptError("FakeEnvelope ciphertext missing magic prefix")
        rest = ciphertext[len(_PREFIX) :]
        expected = purpose.encode("utf-8") + b"::"
        if not rest.startswith(expected):
            raise EnvelopeDecryptError(
                f"FakeEnvelope purpose mismatch; expected {purpose!r}"
            )
        return rest[len(expected) :]
