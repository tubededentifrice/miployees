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

__all__ = ["Blob", "BlobNotFound", "Storage"]


class BlobNotFound(Exception):
    """Raised by :meth:`Storage.get` when a hash has no blob on the store."""


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
