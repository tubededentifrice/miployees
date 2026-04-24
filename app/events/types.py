"""Initial set of typed events.

Each class inherits from :class:`app.events.registry.Event`, overrides
``name``, and adds its payload fields. The ``@register`` decorator runs
at import time so a subscriber can resolve a name to a class without
importing the class directly.

**Role-scope invariant (SSE, cd-clz9).** Every concrete event here
ships with an :class:`~app.events.registry.Event` subclass whose
``allowed_roles`` controls which SSE subscribers observe it
(:mod:`app.api.transport.sse`). The base class defaults the tuple to
:data:`~app.events.registry.ALL_ROLES` for ergonomic reasons — most
business events (tasks, stays, shifts) legitimately fan out to every
role in the workspace. That default is a **choice, not a
right-to-ignore**: when adding a new event, decide consciously
whether every grant role (manager, worker, client, guest) may see
its payload, or narrow the tuple on the subclass. Free-text payload
fields (bodies, subjects, names) are especially sensitive — prefer a
narrower ``allowed_roles`` OR keep the payload to foreign-key IDs
only and let the client re-fetch free text via REST, where the usual
per-row authorisation applies.

The ``DEFAULT_ROLE_EVENTS_ALLOWLIST`` in
:mod:`tests.api.transport.test_sse` tracks which events knowingly
inherit the ``ALL_ROLES`` default; a new event that keeps the
default without being added to that list fails its test, forcing a
review conversation. See ``docs/specs/01-architecture.md``
§"Boundary rules" #3 for the list of events and their purpose.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import field_validator

from app.events.registry import Event, EventRole, register

__all__ = [
    "ExpenseApproved",
    "LlmAssignmentChanged",
    "ShiftChanged",
    "ShiftChangedAction",
    "ShiftEnded",
    "StayUpcoming",
    "TaskAssigned",
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
class TaskAssigned(Event):
    """A task occurrence now has an ``assigned_user_id``.

    Fired at creation time when the one-off service resolves an
    assignee (explicit payload value or assignment-hook output), and
    at amend time when an owner / manager / agent changes who is on
    the hook. The ``assigned_to`` field is the new assignee; the
    previous one (if any) is available on the task row itself via
    the audit trail, not on this event — subscribers that need the
    transition rebuild it from the audit log.
    """

    name: ClassVar[str] = "task.assigned"

    task_id: str
    assigned_to: str


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
class LlmAssignmentChanged(Event):
    """A workspace's LLM model assignments or capability inheritance changed.

    Fired whenever an admin mutates the ``model_assignment`` or
    ``llm_capability_inheritance`` table for a workspace (create,
    update, delete, reorder, enable/disable). The §11 router
    (:mod:`app.domain.llm.router`) listens on this event to drop its
    30s in-process resolver cache for the affected workspace so the
    next ``resolve_model`` call observes the new chain without
    waiting for the TTL to expire.

    Scope: ``("manager",)``. LLM configuration is admin-only surface;
    workers, clients, and guests neither see nor care about the
    ``/admin/llm`` graph. Narrowing the role tuple also keeps the
    event off client-bound SSE streams where its arrival would be an
    information-leak about the workspace's internal configuration
    (which providers / models are wired up, how often they change).

    Spec §12 names the adjacent deployment-scope SSE event
    ``admin.llm.assignment_updated`` that fans out on
    ``/admin/events``. The workspace-scoped event here is a
    separate, sibling signal: the deployment-admin edit the
    former announces can trigger the latter on every affected
    workspace, but the two events have different scopes
    (deployment vs workspace) and different subscribers (deployment
    admin console vs per-workspace caches) so they are not the same
    event on the bus.
    """

    name: ClassVar[str] = "llm.assignment.changed"
    # LLM configuration is admin-only surface; keep off worker /
    # client / guest SSE streams.
    allowed_roles: ClassVar[tuple[EventRole, ...]] = ("manager",)


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
