"""Tests for :mod:`app.util.ulid`."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from itertools import pairwise

import pytest
from ulid import ULID

from app.util.clock import FrozenClock
from app.util.ulid import new_ulid, parse_ulid

_CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class TestNewUlid:
    def test_returns_26_char_crockford(self) -> None:
        value = new_ulid()
        assert len(value) == 26
        assert _CROCKFORD_RE.match(value), value

    def test_distinct_calls_are_unique(self) -> None:
        values = {new_ulid() for _ in range(1000)}
        assert len(values) == 1000

    def test_monotonic_under_tight_loop(self) -> None:
        # 10k ULIDs back-to-back should sort strictly ascending.
        values = [new_ulid() for _ in range(10_000)]
        for a, b in pairwise(values):
            assert a < b, f"non-monotonic: {a} !< {b}"

    def test_clock_injection_pins_timestamp_prefix(self) -> None:
        fixed = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
        clock = FrozenClock(fixed)
        a = parse_ulid(new_ulid(clock))
        b = parse_ulid(new_ulid(clock))
        # Both ULIDs sit in the same (or adjacent — see the 1ms push on
        # random saturation) millisecond as the frozen clock.
        a_ms = int(a.timestamp * 1000)
        b_ms = int(b.timestamp * 1000)
        fixed_ms = int(fixed.timestamp() * 1000)
        assert abs(a_ms - fixed_ms) <= 1
        assert abs(b_ms - fixed_ms) <= 1
        # And the second one sorts strictly after the first.
        assert str(a) < str(b)

    def test_same_frozen_instant_still_monotonic(self) -> None:
        clock = FrozenClock(datetime(2026, 4, 19, tzinfo=UTC))
        values = [new_ulid(clock) for _ in range(1000)]
        for a, b in pairwise(values):
            assert a < b


class TestParseUlid:
    def test_round_trips(self) -> None:
        raw = new_ulid()
        parsed = parse_ulid(raw)
        assert isinstance(parsed, ULID)
        assert str(parsed) == raw

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_ulid("not-a-ulid")
