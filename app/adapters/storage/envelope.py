"""Envelope encryption for small secrets stored in domain-owned columns.

This is the **first-of-its-kind** seam in the repo. §15 describes a
fuller ``secret_envelope`` table keyed by an 8-byte fingerprint of
the active root key, with per-row nonce, key-rotation support, and
audit plumbing. The full thing lands with the rotation machinery
(``crewday admin rotate-root-key``). Until then domain services that
need to persist a secret at rest (iCal feed URLs — cd-1ai — being the
first) need a minimal, auditable primitive so the bytes on disk are
never plaintext.

The primitive is:

* 32-byte key derived from :attr:`app.config.Settings.root_key` via
  :func:`app.auth.keys.derive_subkey` with a caller-supplied
  ``purpose`` label (HKDF-Expand's ``info`` parameter — different
  purposes produce unrelated key material, the whole point of the
  expand step).
* AES-256-GCM authenticated encryption with a per-ciphertext random
  96-bit nonce. ``cryptography``'s ``AESGCM`` is a single stdlib-
  adjacent call.
* Wire format ``version || nonce || ciphertext_with_tag``.
  Version byte ``0x01`` pins the format so a future migration to the
  full §15 ``secret_envelope`` row can distinguish legacy inline
  ciphertext from envelope-referenced rows.

``EnvelopeEncryptor`` is the **port** other layers consume. Production
code wires :class:`Aes256GcmEnvelope`; tests substitute
``InMemoryEnvelope`` (see ``tests/_fakes/envelope.py``) which is a
structural match with no key material.

**Threat model.** This helper defends against "attacker walks away
with the DB backup". The in-process root key is still plaintext in
memory while the service is running — that's inherent to any online
encryption. A DB-only exfiltration gets ciphertext + nonces; without
the root key there's no path to plaintext.

**Forward compatibility.** When the real ``secret_envelope`` table
lands, the domain service's write shape stays the same: the service
will pass a ``purpose`` + plaintext to a repository-backed
``EnvelopeEncryptor`` that persists a row rather than embedding the
ciphertext in the owner's column. The domain code here needs no
change.

See ``docs/specs/15-security-privacy.md`` §"Secret envelope",
§"Key management".
"""

from __future__ import annotations

import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from app.adapters.storage.ports import EnvelopeDecryptError, EnvelopeEncryptor
from app.auth.keys import derive_subkey

__all__ = [
    "Aes256GcmEnvelope",
    "EnvelopeDecryptError",
    "EnvelopeEncryptor",
]


# Pinned AES-GCM nonce length in bytes (96 bits — the NIST-SP-800-38D
# recommended default and what ``AESGCM.generate_nonce()`` defaults
# to). Kept explicit so the on-the-wire format is independent of
# upstream defaults if those ever shift.
_NONCE_LEN: Final[int] = 12
# Wire-format version byte. ``0x01`` = "inline AESGCM (nonce|ct|tag)".
# A future migration to the full §15 ``secret_envelope`` row uses
# ``0x02`` so legacy ciphertexts stay decryptable during the
# transition.
_VERSION_V1: Final[int] = 0x01


class Aes256GcmEnvelope:
    """AES-256-GCM envelope backed by HKDF-derived subkeys.

    Constructor takes the root key as a :class:`pydantic.SecretStr`
    (the same type :attr:`app.config.Settings.root_key` exposes) so
    no plaintext ever lands in ``repr`` or default serialisation.
    The derived 32-byte subkey is held on the stack inside
    :meth:`encrypt` / :meth:`decrypt` — we don't cache an
    :class:`AESGCM` instance since that would keep the key material
    pinned on the heap for longer than necessary. Derivation is a
    single HMAC-SHA-256 call; the cost is noise next to the fetch
    or DB write it accompanies.
    """

    __slots__ = ("_root_key",)

    def __init__(self, root_key: SecretStr) -> None:
        """Bind the encryptor to ``root_key``.

        Re-uses :func:`app.auth.keys.derive_subkey`'s validation — a
        ``None`` / empty root key raises :class:`KeyDerivationError`
        at encrypt / decrypt time (not here — the authoritative
        failure point is where the subkey is actually needed).
        """
        self._root_key = root_key

    def encrypt(self, plaintext: bytes, *, purpose: str) -> bytes:
        """Return ``version || nonce || AESGCM(key, nonce, plaintext)``.

        ``purpose`` is folded into the HKDF expand step as the
        ``info`` parameter. Every call generates a fresh random
        nonce via :func:`os.urandom` — never re-use a nonce with
        the same key (GCM's security reduction collapses
        catastrophically on nonce re-use).
        """
        key = derive_subkey(self._root_key, purpose=_purpose_label(purpose))
        aead = AESGCM(key)
        nonce = os.urandom(_NONCE_LEN)
        ct = aead.encrypt(nonce, plaintext, None)
        return bytes((_VERSION_V1,)) + nonce + ct

    def decrypt(self, ciphertext: bytes, *, purpose: str) -> bytes:
        """Inverse of :meth:`encrypt`.

        Fails with :class:`EnvelopeDecryptError` on shape / version /
        tag mismatch. The authentication tag is part of the ciphertext
        body — GCM binds plaintext to ciphertext, so a flipped bit
        anywhere in the body surfaces as a tag-mismatch rather than
        silent corruption.
        """
        if len(ciphertext) < 1 + _NONCE_LEN + 16:
            # 16 = AES-GCM tag length. A shorter blob can't possibly
            # be a valid ciphertext; raise before we call into the
            # primitive so the error message is specific.
            raise EnvelopeDecryptError(
                "ciphertext too short to be a valid AES-GCM envelope"
            )
        if ciphertext[0] != _VERSION_V1:
            raise EnvelopeDecryptError(
                f"unknown envelope version {ciphertext[0]!r}; expected {_VERSION_V1!r}"
            )
        nonce = ciphertext[1 : 1 + _NONCE_LEN]
        body = ciphertext[1 + _NONCE_LEN :]
        key = derive_subkey(self._root_key, purpose=_purpose_label(purpose))
        aead = AESGCM(key)
        try:
            return aead.decrypt(nonce, body, None)
        except Exception as exc:  # cryptography raises InvalidTag etc.
            raise EnvelopeDecryptError(
                "envelope decryption failed; ciphertext is not valid "
                "under the current root key for the given purpose"
            ) from exc


def _purpose_label(purpose: str) -> str:
    """Namespace a raw ``purpose`` under the envelope seam.

    Prefixes with ``"storage.envelope."`` so a collision with an
    auth-layer subkey label (``"magic-link"``, ``"session-cookie"``)
    is structurally impossible. HKDF's expand step already makes
    the namespaces disjoint byte-for-byte, but the explicit prefix
    keeps the audit story readable when operators scan subkey
    labels in code.
    """
    if not purpose or not purpose.strip():
        raise ValueError("envelope purpose must be a non-blank label")
    return f"storage.envelope.{purpose}"
