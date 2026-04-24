"""Unit tests for :mod:`app.api.pagination`.

The helper is thin but load-bearing: every paginated v1 router keys
off :func:`encode_cursor` / :func:`decode_cursor` and the
:func:`paginate` envelope builder. Breakage here silently desyncs the
``{data, next_cursor, has_more}`` envelope shape across contexts.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    decode_cursor,
    encode_cursor,
    paginate,
)


@dataclass(frozen=True, slots=True)
class _Row:
    """Minimal stand-in for a domain view with an ``id`` key."""

    id: str


class TestCursorRoundTrip:
    """A ULID-shaped key survives encode / decode unchanged."""

    def test_roundtrip_preserves_key(self) -> None:
        key = "01HW9ZABCDE1234567890ABCDE"
        assert decode_cursor(encode_cursor(key)) == key

    def test_none_cursor_decodes_to_none(self) -> None:
        assert decode_cursor(None) is None

    def test_empty_string_decodes_to_none(self) -> None:
        assert decode_cursor("") is None

    def test_malformed_cursor_raises_422(self) -> None:
        """A cursor that isn't URL-safe base64 trips a typed 422."""
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor("not a cursor!!!!")
        assert exc_info.value.status_code == 422
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "invalid_cursor"

    def test_non_ascii_cursor_raises_422(self) -> None:
        """Base64 payload that decodes to non-ASCII bytes is rejected."""
        import base64

        payload = base64.urlsafe_b64encode(b"\xff\xfe\xfd").rstrip(b"=").decode("ascii")
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(payload)
        assert exc_info.value.status_code == 422


class TestPaginate:
    """Envelope-building semantics."""

    def test_no_overflow_has_no_cursor(self) -> None:
        rows = [_Row(id=f"id{i}") for i in range(3)]
        page = paginate(rows, limit=5, key_getter=lambda r: r.id)
        assert page.has_more is False
        assert page.next_cursor is None
        assert [r.id for r in page.items] == ["id0", "id1", "id2"]

    def test_exactly_limit_has_no_cursor(self) -> None:
        """``len == limit`` means no more rows follow."""
        rows = [_Row(id=f"id{i}") for i in range(5)]
        page = paginate(rows, limit=5, key_getter=lambda r: r.id)
        assert page.has_more is False
        assert page.next_cursor is None

    def test_overflow_trims_and_encodes_last_returned(self) -> None:
        """``len == limit + 1`` → trim the last row + encode cursor."""
        rows = [_Row(id=f"id{i:02}") for i in range(6)]
        page = paginate(rows, limit=5, key_getter=lambda r: r.id)
        assert page.has_more is True
        assert [r.id for r in page.items] == [f"id{i:02}" for i in range(5)]
        # The cursor keys off the last returned row (id04), not the
        # sentinel row we trimmed off (id05) — that way the next page
        # query uses ``id > id04`` and picks up id05 as the first row.
        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor) == "id04"

    def test_empty_rows(self) -> None:
        page: object = paginate([], limit=5, key_getter=lambda r: r.id)
        # ``page`` comes back as ``CursorPage[Unknown]`` because the
        # empty list doesn't pin the type-var; the attribute reads
        # still narrow correctly thanks to :class:`CursorPage`'s own
        # typed annotations.
        from app.api.pagination import CursorPage

        assert isinstance(page, CursorPage)
        assert page.items == ()
        assert page.next_cursor is None
        assert page.has_more is False

    def test_invalid_limit_raises(self) -> None:
        with pytest.raises(ValueError):
            paginate([], limit=0, key_getter=lambda r: r.id)

    def test_overflow_without_key_raises(self) -> None:
        """Missing both ``key`` and ``key_getter`` is a caller bug."""
        rows = [_Row(id="id0"), _Row(id="id1")]
        with pytest.raises(ValueError):
            paginate(rows, limit=1)

    def test_explicit_key_overrides_getter_absence(self) -> None:
        """The shortcut ``key=`` form works when the caller has the key in hand."""
        rows = [_Row(id="id0"), _Row(id="id1")]
        page = paginate(rows, limit=1, key="id0")
        assert page.has_more is True
        assert decode_cursor(page.next_cursor) == "id0"


class TestBounds:
    """Spec §12 pins default=50 / max=500."""

    def test_default_limit(self) -> None:
        assert DEFAULT_LIMIT == 50

    def test_max_limit(self) -> None:
        assert MAX_LIMIT == 500
