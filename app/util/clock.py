"""Clock port and test doubles.

The domain layer never calls :func:`datetime.now` directly. Instead it
depends on the :class:`Clock` protocol; production code wires
:class:`SystemClock`, tests use :class:`FrozenClock`.

See ``docs/specs/01-architecture.md`` §"Runtime invariants" #2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Protocol for "the current instant, in aware UTC"."""

    def now(self) -> datetime:
        """Return the current moment as an aware UTC ``datetime``."""
        ...


class SystemClock:
    """Default :class:`Clock` implementation backed by the OS clock.

    This is the only place in ``app/util`` allowed to call
    :func:`datetime.now`.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic :class:`Clock` for tests.

    Accepts only aware datetimes; any non-UTC timezone is converted to
    UTC on construction / :meth:`set`.
    """

    __slots__ = ("_now",)

    def __init__(self, at: datetime) -> None:
        self._now = _to_aware_utc(at)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward (or backward, for negative deltas)."""
        self._now = self._now + delta

    def set(self, at: datetime) -> None:
        """Reset the clock to a specific instant."""
        self._now = _to_aware_utc(at)


def _to_aware_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Raises :class:`ValueError` if ``value`` has no tzinfo — naive
    datetimes are ambiguous and never allowed through this seam.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "FrozenClock requires an aware datetime; got naive input. "
            "Pass datetime(..., tzinfo=timezone.utc) or similar."
        )
    return value.astimezone(UTC)
