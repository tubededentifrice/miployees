"""Cursor-based pagination helpers shared across v1 routers.

Spec ``docs/specs/12-rest-api.md`` §"Pagination" and §"Request/response
shape" pin the collection envelope:

* ``GET /<resource>?cursor=<opaque>&limit=<int>``
* Response body: ``{"data": [...], "next_cursor": "…", "has_more": …}``
* ``limit`` default 50, max 500.
* No offset pagination.

The helpers here give every paginated router a single source of truth
so the envelope shape, bounds, and cursor encoding cannot drift
between contexts.

The cursor itself is a URL-safe base64 encoding of the row's key (a
ULID string, for every v1 resource). Keeping the encoding
transport-agnostic means the domain service does not need to know
anything about HTTP; it receives a plain decoded key and returns one
extra row to decide ``has_more``.

The module is deliberately thin: no SQL helpers (each resource's
query shape is specific enough that a cursor WHERE clause reads
clearest inline), no router-side response-model coupling. Routers
construct their own typed :class:`CursorPage` by calling
:func:`paginate` with the raw row list.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import HTTPException, Query

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "CursorPage",
    "LimitQuery",
    "PageCursorQuery",
    "decode_cursor",
    "encode_cursor",
    "paginate",
]


# Spec §12 "Pagination" — verbatim. Centralised here so a bounds bump
# lands in one place.
DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 500


# Reusable FastAPI query-param dependencies so every paginated router
# shares the same bounds + description without re-declaring the
# ``ge``/``le`` guards.
LimitQuery = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_LIMIT,
        description=(
            "Maximum rows to return. Default 50, cap 500 per spec §12 "
            "'Pagination'. Rejected with 422 outside ``[1, 500]``."
        ),
    ),
]
PageCursorQuery = Annotated[
    str | None,
    Query(
        max_length=256,
        description=(
            "Opaque forward cursor from the previous page's "
            "``next_cursor``. Omitted on the first call. Bounded to "
            "256 chars to keep the URL below reverse-proxy header "
            "limits."
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    """Result of a cursor-paginated query.

    ``items`` is the rows the caller should surface (already trimmed
    to the requested limit). ``next_cursor`` is the opaque string the
    client passes back to fetch the next page, or ``None`` when
    ``has_more`` is ``False``.

    Slots keep the object cheap on the hot path; ``frozen`` means the
    router cannot accidentally stash mutable state on the return
    value between the domain service and the Pydantic projection.
    """

    items: tuple[T, ...]
    next_cursor: str | None
    has_more: bool


def encode_cursor(key: str) -> str:
    """Encode a row key (ULID string) as an opaque URL-safe cursor.

    Base64-url encoding keeps the cursor character-set identical
    across reverse proxies + query-string decoders. Stripping the
    trailing ``=`` padding is deliberate: every ULID is exactly 26
    ASCII characters, so the encoded cursor is a fixed 36 chars
    before padding — keeping it a constant width makes log diffing
    easier and removes an escape in query-string serialisation.
    """
    return base64.urlsafe_b64encode(key.encode("ascii")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> str | None:
    """Return the underlying row key, or ``None`` if ``cursor`` is ``None``.

    A malformed cursor raises :class:`HTTPException` 422 rather than
    swallowing the error — the caller likely tampered with the value
    and a silent "reset to first page" surface is a much worse
    debugging experience. The explicit envelope shape mirrors the
    rest of §12's validation errors.
    """
    if cursor is None or cursor == "":
        return None
    # Re-add the padding we stripped on encode so the base64 library
    # accepts the value. Up to three ``=`` chars cover every valid
    # payload length.
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
    except (ValueError, binascii.Error) as exc:
        # 422 constant renamed in Starlette 2024; the literal keeps the
        # call stable across minor versions and avoids the deprecation
        # warning on newer releases.
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_cursor", "message": "cursor is malformed"},
        ) from exc
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "cursor payload is not ASCII",
            },
        ) from exc


def paginate[T](
    rows: Sequence[T],
    *,
    limit: int,
    key: str | None = None,
    key_getter: Callable[[T], str] | None = None,
) -> CursorPage[T]:
    """Trim ``rows`` to ``limit`` + build the forward-cursor envelope.

    The caller's query fetches ``limit + 1`` rows and passes the full
    slice here; if the extra row is present we trim it off and
    encode the last-returned row's key into ``next_cursor``.

    Either ``key`` (for the simple case where the caller already knows
    the last-returned row's key) or ``key_getter`` (reads it off the
    row object) must be provided when ``len(rows) > limit``. When
    there is no overflow, both are ignored.
    """
    if limit < 1:
        # Defence-in-depth: the FastAPI :data:`LimitQuery` dependency
        # rejects out-of-range values, but calling :func:`paginate`
        # directly (tests, other transports) must still refuse a
        # nonsensical ``limit``.
        raise ValueError(f"limit must be >= 1; got {limit!r}")
    has_more = len(rows) > limit
    items = tuple(rows[:limit])
    if not has_more:
        return CursorPage(items=items, next_cursor=None, has_more=False)

    # The row we encode is the last row IN the returned page — passing
    # it back as the cursor means "give me rows strictly after this
    # key". ``items`` is non-empty here because ``has_more`` implies
    # ``len(rows) > limit >= 1``.
    last = items[-1]
    if key_getter is not None:
        cursor_key = key_getter(last)
    elif key is not None:
        cursor_key = key
    else:
        raise ValueError(
            "paginate(rows, limit) requires key or key_getter when has_more"
        )
    return CursorPage(
        items=items,
        next_cursor=encode_cursor(cursor_key),
        has_more=True,
    )
