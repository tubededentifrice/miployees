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

import re
from datetime import datetime, timedelta
from typing import ClassVar, Final, Literal

from pydantic import field_validator

from app.events.registry import Event, EventRole, register

__all__ = [
    "ExpenseApproved",
    "LlmAssignmentChanged",
    "NotificationCreated",
    "ShiftChanged",
    "ShiftChangedAction",
    "ShiftEnded",
    "StayUpcoming",
    "TaskAssigned",
    "TaskCompleted",
    "TaskCreated",
    "TaskOverdue",
    "TaskPrimaryUnavailable",
    "TaskReassigned",
    "TaskUnassigned",
]


# Values the ``ShiftChanged.action`` field narrows to. Kept as a
# module-level ``Literal`` alias so domain callers + tests can import
# it alongside the event class without re-declaring the union.
ShiftChangedAction = Literal["opened", "closed", "edited"]


# Regex constraining short "reason code" fields on events that fan out
# to every grant role via SSE. The shape mirrors an identifier
# (``[a-z][a-z0-9_]*``) so callers cannot slip a free-text explanation
# (a manager's note, a guest-visible message) into an event that
# reaches worker / client / guest subscribers. The 64-char cap is well
# above the longest legitimate value we emit today
# (``primary_and_backups_unavailable`` is 31) and leaves headroom for
# future compound codes without ever approaching sentence-length text.
_REASON_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _require_reason_code(value: str) -> str:
    """Guard for "reason code" fields — enforces the identifier shape.

    The ``task.unassigned`` event (and any future sibling that ships
    a caller-supplied reason) fans out to every workspace grant role,
    so the field must be a short opaque code the client can switch
    on — never free text typed by a human. A failing value raises at
    publish time so a careless caller learns loudly instead of
    leaking the text downstream.
    """
    if not _REASON_CODE_RE.match(value):
        raise ValueError(
            "reason must be a short identifier-shaped code matching "
            f"{_REASON_CODE_RE.pattern!r} (lowercase letters, digits, "
            "underscores; <= 64 chars). Free-text reasons are rejected "
            "to keep the event safe to fan out to every grant role."
        )
    return value


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
class TaskReassigned(Event):
    """A task occurrence's assignee was changed via an explicit move.

    Fired by :func:`app.domain.tasks.assignment.reassign_task` when an
    owner, manager, or agent pins the task to a different user than
    the current assignee. ``previous_user_id`` carries the user the
    task was moved **from** (``None`` if the task was unassigned), and
    ``new_user_id`` the user it was moved **to**. Subscribers that
    need the delta can read both fields without walking the audit log.

    Distinct from :class:`TaskAssigned`, which fires on the first
    assignment (creation-time or auto-resolved). The two events carry
    different semantics on the UI — a reassignment surfaces a
    "moved from X to Y" toast, an assignment a "now on Y" chip — so
    the frontend dispatcher maps them to separate invalidations.
    """

    name: ClassVar[str] = "task.reassigned"

    task_id: str
    previous_user_id: str | None
    new_user_id: str


@register
class TaskUnassigned(Event):
    """A task occurrence has no assignee (either explicitly cleared or
    auto-assignment found zero candidates from scratch).

    Fired by :func:`app.domain.tasks.assignment.assign_task` when the
    algorithm runs without a :class:`Schedule.default_assignee` and
    the candidate pool is empty (§06 "Assignment algorithm" step 5,
    "candidate pool was empty from the start" branch), and by
    :func:`app.domain.tasks.assignment.unassign_task` when a manager
    or agent explicitly clears the assignee.

    Companion to :class:`TaskPrimaryUnavailable`, which fires when
    step 1 was attempted (schedule had a listed primary or backup)
    but none of them passed availability + rota — the two events are
    mutually exclusive on any one auto-assignment run.

    ``reason`` is a short identifier-shaped code the caller supplies
    (``"candidate_pool_empty"``, ``"manager_cleared"``, …). The code
    is constrained to ``[a-z][a-z0-9_]{0,63}`` at publish time so a
    careless caller cannot slip free text (a manager's note, a
    guest-visible message) into an event that fans out to every
    grant role via SSE — callers translate the code to UI copy in
    the client's toast / digest reducers.
    """

    name: ClassVar[str] = "task.unassigned"

    task_id: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_is_code(cls, value: str) -> str:
        return _require_reason_code(value)


@register
class TaskPrimaryUnavailable(Event):
    """Schedule's primary + every backup failed availability; pool also empty.

    Fired by :func:`app.domain.tasks.assignment.assign_task` when the
    algorithm attempted step 1 (the schedule carried a primary or
    backup list) but none of those users was available AND the
    candidate pool was also empty (§06 "Assignment algorithm" step 5,
    "step 1 was attempted but no listed user was available" branch).

    The manager's daily digest surfaces this as a "primary
    unavailable" alert — the owner can decide whether to add backups,
    override, or accept the unassigned state before the SLA breach.
    The ``candidate_count`` field carries how many pool candidates
    were considered after the primary/backup walk (``0`` for this
    event, by definition — the pool was empty too).
    """

    name: ClassVar[str] = "task.primary_unavailable"

    task_id: str
    candidate_count: int


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
class NotificationCreated(Event):
    """A :class:`~app.adapters.db.messaging.models.Notification` row has
    just been persisted for ``actor_user_id``.

    Fired by :class:`~app.domain.messaging.notifications.NotificationService`
    after the row lands in the caller's Unit of Work, before the
    email / push fanout — so the client-side cache invalidation races
    the outbound channels and the unread badge updates the instant
    the DB row is visible to the reader.

    The event is **user-scoped**: the SSE transport filters the
    fanout so only the recipient's tabs see it. A manager watching
    the workspace stream does not receive another user's
    notifications — notifications are personal in both the domain
    model and the wire protocol.

    Scope: the full grant-role tuple (``manager``, ``worker``,
    ``client``, ``guest``), since a notification can land for any
    role. The user-scope filter carries the real protection;
    ``allowed_roles`` is only the coarse first pass.

    Payload is kept deliberately narrow: the ``notification_id`` so
    the client can ``GET /notifications/{id}`` for the rendered
    subject / body, the ``kind`` so the client can pick an icon
    without a round-trip, and ``actor_user_id`` for the SSE user-
    scope filter. Free-text fields (subject, body) are NOT on the
    wire — the client fetches them over REST under the normal
    per-row authorisation path.
    """

    name: ClassVar[str] = "notification.created"
    # One recipient → one notification. The SSE transport compares
    # this against the caller's ``WorkspaceContext.actor_id`` and
    # drops the frame for every other subscriber, including managers
    # watching the same workspace.
    user_scoped: ClassVar[bool] = True

    notification_id: str
    kind: str
    # Required by the ``user_scoped=True`` contract on the registry
    # (see :mod:`app.events.registry`). Always equal to the
    # ``Notification.recipient_user_id`` column — the event is
    # addressed to the recipient, not to whoever caused it.
    actor_user_id: str


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
