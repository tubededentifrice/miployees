"""Initial set of typed events.

Each class inherits from :class:`app.events.registry.Event`, overrides
``name``, and adds its payload fields. The ``@register`` decorator runs
at import time so a subscriber can resolve a name to a class without
importing the class directly.

See ``docs/specs/01-architecture.md`` §"Boundary rules" #3 for the list
of events and their purpose.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import field_validator

from app.events.registry import Event, register

__all__ = [
    "ExpenseApproved",
    "ShiftChanged",
    "ShiftChangedAction",
    "ShiftEnded",
    "StayUpcoming",
    "TaskCompleted",
    "TaskCreated",
    "TaskOverdue",
]


# Values the ``ShiftChanged.action`` field narrows to. Kept as a
# module-level ``Literal`` alias so domain callers + tests can import
# it alongside the event class without re-declaring the union.
ShiftChangedAction = Literal["opened", "closed", "edited"]


def _require_aware_utc(value: datetime) -> datetime:
    """Shared guard for payload ``datetime`` fields.

    The base class enforces this on ``occurred_at``; payload timestamps
    (``overdue_since``, ``arrives_at``, ``ended_at``) need the same
    guarantee — a naive timestamp in a cross-context event would
    silently cross timezones, and a non-UTC aware timestamp (e.g.
    ``+05:00``) violates spec §"Application-specific notes" ("Time is
    UTC at rest"). We accept only an offset of exactly zero.
    """
    offset = value.utcoffset() if value.tzinfo is not None else None
    if offset is None or offset != timedelta(0):
        raise ValueError(
            "datetime fields on events must be timezone-aware and in UTC "
            "(offset 00:00)."
        )
    return value


@register
class TaskCreated(Event):
    """A task occurrence has been created (RRULE generation, manual add)."""

    name: ClassVar[str] = "task.created"

    task_id: str


@register
class TaskCompleted(Event):
    """A task occurrence has been marked done by ``completed_by``."""

    name: ClassVar[str] = "task.completed"

    task_id: str
    completed_by: str


@register
class TaskOverdue(Event):
    """A task occurrence is past its due time without completion."""

    name: ClassVar[str] = "task.overdue"

    task_id: str
    overdue_since: datetime

    @field_validator("overdue_since")
    @classmethod
    def _overdue_since_is_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


@register
class StayUpcoming(Event):
    """A reservation's arrival is imminent (digest, turnover trigger)."""

    name: ClassVar[str] = "stay.upcoming"

    stay_id: str
    arrives_at: datetime

    @field_validator("arrives_at")
    @classmethod
    def _arrives_at_is_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


@register
class ExpenseApproved(Event):
    """A submitted expense has been approved by ``approved_by``."""

    name: ClassVar[str] = "expense.approved"

    expense_id: str
    approved_by: str


@register
class ShiftEnded(Event):
    """A shift has been closed (worker clock-out or auto-close)."""

    name: ClassVar[str] = "shift.ended"

    shift_id: str
    ended_at: datetime

    @field_validator("ended_at")
    @classmethod
    def _ended_at_is_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


@register
class ShiftChanged(Event):
    """A shift row mutated — opened, closed, or edited.

    The SSE transport (cd-clz9) fans this out to subscribed clients so
    the manager timesheet + worker clock widget update without polling.
    The ``action`` field narrows the mutation shape; downstream
    invalidation maps can switch on it (open → refresh "my open
    shift" query, closed → refresh timesheet totals, edited → both).

    The name is ``time.shift.changed`` — dotted by bounded context
    (``time``) + entity (``shift``) + action (``changed``). A sibling
    ``shift.ended`` event already exists for the legacy "clock out
    happened" narrow signal; ``time.shift.changed`` is the richer
    union that carries the action verb on the payload.
    """

    name: ClassVar[str] = "time.shift.changed"

    shift_id: str
    user_id: str
    action: ShiftChangedAction
