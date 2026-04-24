"""Storage ports.

Defines the content-addressed blob-store seam the domain layer uses
for uploads (receipts, task evidence, instruction attachments). The
caller computes the SHA-256 hex digest; the store keys blobs by that
digest and returns :class:`Blob` metadata.

See ``docs/specs/01-architecture.md`` §"Adapters" for the concrete
``LocalFsStorage`` / ``S3Storage`` implementations that sit behind
this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import IO, Protocol

__all__ = [
    "Blob",
    "BlobNotFound",
    "EnvelopeDecryptError",
    "EnvelopeEncryptor",
    "Storage",
]


class BlobNotFound(Exception):
    """Raised by :meth:`Storage.get` when a hash has no blob on the store."""


class EnvelopeDecryptError(Exception):
    """Raised by :class:`EnvelopeEncryptor` when a ciphertext can't decrypt.

    Ciphertext shape mismatch, wrong version byte, wrong purpose, or
    AEAD tag mismatch — every failure mode collapses to this single
    error. Callers treat it as "the bytes on disk are not decipherable
    under the current key for this purpose": fail loudly rather than
    silently return garbage. Mirrors the §15 ``KeyFingerprintMismatch``
    surface for the simpler inline-envelope case.

    See ``docs/specs/15-security-privacy.md`` §"Secret envelope".
    """


class EnvelopeEncryptor(Protocol):
    """Port: encrypt / decrypt small secrets at rest.

    Every secret persisted in a domain-owned column (iCal feed URL,
    property wifi password, workspace SMTP secret, ...) flows
    through this seam so the bytes on disk are never plaintext.

    ``purpose`` is a short ASCII label passed down as the HKDF-Expand
    ``info`` parameter — different purposes produce unrelated key
    streams, so ``"ical-feed-url"`` and ``"wifi-password"`` can never
    decrypt each other's ciphertexts. Callers pin one purpose per
    column / per owner-entity kind.

    Concrete implementation: :class:`app.adapters.storage.envelope.
    Aes256GcmEnvelope` (AES-256-GCM, HKDF-derived subkey). Tests
    wire :class:`tests._fakes.envelope.FakeEnvelope`, a deterministic
    no-crypto stand-in that still enforces the purpose contract.

    See ``docs/specs/15-security-privacy.md`` §"Secret envelope".
    """

    def encrypt(self, plaintext: bytes, *, purpose: str) -> bytes:
        """Return an opaque ciphertext blob. Format is implementation-defined."""
        ...

    def decrypt(self, ciphertext: bytes, *, purpose: str) -> bytes:
        """Inverse of :meth:`encrypt`; raises :class:`EnvelopeDecryptError`."""
        ...


@dataclass(frozen=True, slots=True)
class Blob:
    """Metadata record returned by :meth:`Storage.put`.

    ``content_hash`` is the SHA-256 hex digest; ``content_type`` may be
    absent when the caller did not assert a MIME type. ``created_at``
    is aware UTC.
    """

    content_hash: str
    size_bytes: int
    content_type: str | None
    created_at: datetime


class Storage(Protocol):
    """Content-addressed blob store.

    All methods are idempotent. Streams are synchronous ``IO[bytes]``
    in v1; an async variant can be added behind a sibling protocol
    once needed.
    """

    def put(
        self,
        content_hash: str,
        data: IO[bytes],
        *,
        content_type: str | None = None,
    ) -> Blob:
        """Write ``data`` under ``content_hash`` and return its metadata.

        Safe to call repeatedly with the same ``content_hash``; the
        stored bytes MUST match an existing blob with that hash.
        """
        ...

    def get(self, content_hash: str) -> IO[bytes]:
        """Open the blob for reading.

        Raises :class:`BlobNotFound` if no blob exists for ``content_hash``.
        """
        ...

    def exists(self, content_hash: str) -> bool:
        """Return whether a blob exists for ``content_hash``."""
        ...

    def sign_url(self, content_hash: str, *, ttl_seconds: int) -> str:
        """Return a short-lived signed URL pointing at the blob."""
        ...

    def delete(self, content_hash: str) -> None:
        """Delete the blob if present.

        Silently succeeds if no blob exists for ``content_hash``.
        """
        ...
