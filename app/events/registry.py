"""Typed event registry.

Every cross-context event (``task.created``, ``task.completed``, …) is a
frozen :class:`pydantic.BaseModel` subclass registered under a stable
dotted name. The registry keeps the ``name`` → class mapping that the
bus uses to route subscribers and that serialisation layers use to
reconstitute events (future transport-agnostic delivery).

See ``docs/specs/01-architecture.md`` §"Boundary rules" #3.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "Event",
    "EventAlreadyRegistered",
    "EventNotFound",
    "_reset_for_tests",
    "get_event_type",
    "register",
    "registered_events",
]


class EventAlreadyRegistered(ValueError):
    """Raised when two distinct classes claim the same event name."""


class EventNotFound(KeyError):
    """Raised when :func:`get_event_type` is asked for an unknown name."""


class Event(BaseModel):
    """Base class for every typed event on the bus.

    Concrete subclasses set ``name`` (a dotted identifier like
    ``task.completed``) and add payload fields. Instances are frozen —
    events are values, not mutable records — and must carry the routing
    metadata every subscriber is allowed to rely on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Overridden by each concrete subclass. The base class carries an
    # empty string so ``Event`` itself is never a valid registration
    # target; ``register`` rejects empty names explicitly.
    name: ClassVar[str] = ""

    workspace_id: str
    actor_id: str
    correlation_id: str
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        """Events are only meaningful in UTC; naive or offset timestamps
        are a bug.

        Per spec §"Application-specific notes", "Time is UTC at rest".
        A ``+05:00`` datetime is just as wrong as a naive one — it would
        silently cross timezones once serialised. We accept only a
        timezone whose current offset is exactly zero.
        """
        offset = value.utcoffset() if value.tzinfo is not None else None
        if offset is None or offset != timedelta(0):
            raise ValueError(
                "Event.occurred_at must be timezone-aware and in UTC (offset 00:00)."
            )
        return value


_lock = threading.Lock()
_REGISTRY: dict[str, type[Event]] = {}


def register(event_cls: type[Event]) -> type[Event]:
    """Register ``event_cls`` under its declared ``name``.

    Used as a class decorator::

        @register
        class TaskCompleted(Event):
            name: ClassVar[str] = "task.completed"
            ...

    Re-registering the *same* class object is a no-op (safe under test
    re-imports). Registering a *different* class under a name already
    taken raises :class:`EventAlreadyRegistered` — the registry is
    process-global and a collision would silently reroute subscribers.
    """
    name = event_cls.name
    if not name:
        raise ValueError(
            f"{event_cls.__name__} must set a non-empty ``name`` ClassVar "
            "before registering."
        )
    with _lock:
        existing = _REGISTRY.get(name)
        if existing is None:
            _REGISTRY[name] = event_cls
        elif existing is not event_cls:
            raise EventAlreadyRegistered(
                f"Event name {name!r} is already registered to "
                f"{existing.__module__}.{existing.__qualname__}; cannot "
                f"re-register under {event_cls.__module__}."
                f"{event_cls.__qualname__}."
            )
    return event_cls


def get_event_type(name: str) -> type[Event]:
    """Return the class registered under ``name``.

    Raises :class:`EventNotFound` if no event claims that name.
    """
    # Dict lookup is atomic in CPython; lock only when we need a
    # consistent snapshot across multiple operations.
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise EventNotFound(name) from exc


def registered_events() -> Mapping[str, type[Event]]:
    """Return a read-only view of the current registry.

    The view is a snapshot: mutating the registry after this call does
    not change the returned mapping, so callers can iterate without
    risking ``RuntimeError: dictionary changed size during iteration``.
    """
    with _lock:
        snapshot = dict(_REGISTRY)
    return MappingProxyType(snapshot)


def _reset_for_tests() -> None:
    """Clear the registry. Tests use this via an autouse fixture.

    Underscore-prefixed: not part of the public surface. The autouse
    fixture in ``tests/unit/test_events.py`` snapshots the registry
    before each case and restores it afterwards, so the six initial
    events registered at import time survive test-case isolation.
    """
    with _lock:
        _REGISTRY.clear()
