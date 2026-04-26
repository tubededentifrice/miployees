"""``occurrence`` one-off creation service (§06 task kind 1).

A one-off task is a single :class:`~app.adapters.db.tasks.models.Occurrence`
row with no parent schedule and no stay-lifecycle bundle. It powers
three call sites:

* the quick-add modal on `/today` and `/schedule` (``is_personal`` is
  the default per the mock; callers flip it off to share the task),
* the manager's "+ New task" button (share-by-default),
* the NL-intake agent's ``POST /api/v1/tasks/from_nl/commit`` path,
  which arrives here once the preview is confirmed (§11).

Public surface:

* **DTO** — :class:`TaskCreate`. Pydantic v2 model validating the
  ``template_id`` OR ``title`` branch plus the ``is_personal`` /
  ``assigned_user_id`` pairing.
* **Service function** — :func:`create_oneoff`. Accepts a
  :class:`~app.tenancy.WorkspaceContext`, the payload, and every
  injection seam (``clock``, ``rule_repo``, ``expand_checklist``,
  ``assign``, ``event_bus``) so tests drive the service
  deterministically.
* **View** — :class:`TaskView`. Frozen + slotted projection of the
  row the caller can echo back to the HTTP layer.
* **Errors** — :class:`TaskTemplateNotFound` (re-exported from
  :mod:`app.domain.tasks.templates`; ``LookupError`` → 404),
  :class:`PersonalAssignmentError` (``ValueError`` → 422 when
  ``is_personal=True`` but ``assigned_user_id`` disagrees with the
  caller).

**Permission.** The router already gates the endpoint with the
``tasks.create`` :class:`app.authz.dep.Permission` dependency;
this service re-asserts the check as defence-in-depth (§01
"Handlers are thin" forbids routers-as-authority, but a service
that silently skips the check would be a trust-the-caller anti-
pattern). Scope is ``property`` when the payload carries one,
``workspace`` otherwise.

**State machine.** Per §06 "State machine" — the scheduler worker
drives the automatic ``scheduled → pending`` flip at
``scheduled_for_utc - 1h``. For a one-off created now with a past
``scheduled_for_local`` there is no scheduler flip to wait for, so
we stamp ``state = 'pending'`` inline. Future ``scheduled_for_local``
values land in ``state = 'scheduled'``.

**Personal tasks.** §06 "Self-created and personal tasks" pins the
quick-add default to ``is_personal=True, assigned_user_id=created_by``.
The DTO enforces the pairing: ``is_personal=True`` requires
``assigned_user_id == ctx.user_id``. Visibility is the §15 read-
layer's job; this service is the write surface.

**Template-backed copy.** When ``template_id`` is set the service
copies ``title``, ``description_md``, ``priority``, ``photo_evidence``,
``duration_minutes``, ``linked_instruction_ids``, and
``inventory_consumption_json`` from the template. The payload's
explicit fields override the copy — a caller who sets
``priority='urgent'`` on an otherwise template-backed task wins.
Checklist seeding runs via the injectable hook
:data:`ChecklistExpansionHook` (the real :func:`expand_checklist_for_task`
lands with cd-p5; see the docstring on :func:`_noop_expand_checklist`).

**Assignment.** When ``assigned_user_id`` is ``None`` and
``expected_role_id`` is set we delegate to the
:data:`AssignmentHook` (the real assignment-algorithm lands with
cd-8luu; the default hook is a no-op). If the hook picks an
assignee it writes it through onto the row and fires
``task.assigned`` alongside ``task.created``. If the hook leaves
the row unassigned, only ``task.created`` fires.

**Audit.** One ``task.create_oneoff`` audit row per call; the diff
payload carries the resolved fields (``after`` only — no ``before``
because the row is new).

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task kinds"
(kind 1 — One-off), §"Self-created and personal tasks",
§"Natural-language intake (agent)".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Area, Property, PropertyWorkspace, Unit
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.workspace.models import WorkRole
from app.audit import write_audit
from app.authz import (
    EmptyPermissionRuleRepository,
    PermissionRuleRepository,
    require,
)
from app.domain.tasks.templates import (
    PhotoEvidence,
    Priority,
    TaskTemplateNotFound,
    _narrow_photo_evidence,
    _narrow_priority,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import TaskAssigned, TaskCreated, TaskUpdated
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AssignmentHook",
    "ChecklistExpansionHook",
    "InvalidLocalDatetime",
    "PersonalAssignmentError",
    "TaskCreate",
    "TaskFieldInvalid",
    "TaskNotFound",
    "TaskPatch",
    "TaskTemplateNotFound",
    "TaskView",
    "create_oneoff",
    "read_task",
    "update_task",
]


# ---------------------------------------------------------------------------
# Hook signatures
# ---------------------------------------------------------------------------


# Seed :class:`ChecklistItem` rows for the newly-inserted occurrence
# from the template's checklist payload. The ``is_ad_hoc`` flag maps
# to the spec's "Ad-hoc tasks always include every item, regardless
# of RRULE" rule (§06 "Seeding is RRULE-filtered"). The default is
# a no-op; cd-p5 wires the real body.
ChecklistExpansionHook = Callable[
    [Session, WorkspaceContext, str, TaskTemplate, bool],
    None,
]


# Pick an assignee for an otherwise-unassigned occurrence. Returns
# the chosen ``user_id`` or ``None`` when the candidate pool is
# empty. The default is a no-op; cd-8luu wires the real §06
# "Assignment algorithm" body.
AssignmentHook = Callable[[Session, WorkspaceContext, str], str | None]


def _noop_expand_checklist(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
    template: TaskTemplate,
    is_ad_hoc: bool,
) -> None:
    """Default checklist-expansion hook — no-op until cd-p5 lands.

    We deliberately do not attempt a partial implementation here:
    the RRULE-filtered seeding rule (§06 "Seeding is RRULE-filtered"
    + "Ad-hoc tasks always include every item") is subtle enough
    that a stopgap would either lie to downstream readers or be
    thrown away when the real implementation arrives.
    """
    _ = session, ctx, occurrence_id, template, is_ad_hoc


def _noop_assign(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
) -> str | None:
    """Default assignment hook — returns ``None`` until cd-8luu lands.

    The assignment algorithm (§06 "Assignment algorithm") depends on
    availability precedence, rota composition, and the work-role
    tables (cd-5kv4 landed ``work_role`` + ``user_work_role``; cd-8luu
    wires the real assignment service). Until cd-8luu lands the
    service leaves ``assignee_user_id`` null; §06 step 5 handles the
    unassigned state with the daily digest.
    """
    _ = session, ctx, occurrence_id
    return None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PersonalAssignmentError(ValueError):
    """``is_personal=True`` requires ``assigned_user_id == ctx.user_id``.

    422-equivalent. Fires when the caller tries to pin a task
    personal to themselves but assigns it to someone else (or leaves
    the assignee blank). §06 "Self-created and personal tasks" pins
    the quick-add default to ``is_personal=True, assigned_user_id =
    created_by``; any deviation is a caller bug the router should
    surface as a 422 rather than silently flipping the flag off.
    """


class TaskNotFound(LookupError):
    """The task id is unknown in the caller's workspace (404).

    Re-exported by :mod:`app.domain.tasks.oneoff` so callers that
    already import ``oneoff`` do not have to cross into
    :mod:`app.domain.tasks.completion` or
    :mod:`app.domain.tasks.assignment` for the ``read`` / ``update``
    paths. The three modules share one loader shape; a single
    ``TaskNotFound`` matching class per module kept the modules
    decoupled, but cd-sn26's router needs one import site for the
    CRUD handlers — this alias.
    """


class InvalidLocalDatetime(ValueError):
    """A ``scheduled_for_local`` payload could not be parsed as ISO-8601.

    Raised by :func:`_parse_local_datetime` when the string is
    syntactically malformed or carries a timezone (the field is
    documented as property-local, tz-naive). Distinct from
    :class:`TaskFieldInvalid` (a referenced row failed validation) and
    from a plain :class:`ValueError` raised by service-internal
    invariants (e.g. the clock contract) — keeping a dedicated subclass
    lets the router map "caller bug" to ``422 invalid_field`` without
    swallowing other ``ValueError``s.
    """


class TaskFieldInvalid(ValueError):
    """A :class:`TaskPatch` field references a row that fails validation.

    Raised by :func:`update_task` when:

    * ``property_id`` is not linked to the caller's workspace through
      :class:`PropertyWorkspace` (cross-workspace borrowing forbidden).
    * ``area_id`` does not belong to the task's resolved property.
    * ``unit_id`` does not belong to the task's resolved property
      (the v1 schema models units as ``unit.property_id``; the §04
      "unit belongs to area or property" widening lands with
      cd-8u5's ``unit.area_id`` column).
    * ``expected_role_id`` is not a live :class:`WorkRole` row in
      the caller's workspace.

    The router maps the exception to ``422 invalid_task_field`` with
    the offending field name + value so the SPA can pin the
    validation error to the right input. Distinct from
    :class:`TaskNotFound` (the row itself is missing) and
    :class:`PersonalAssignmentError` (a different invariant on the
    create path); each lands as its own HTTP code.
    """

    def __init__(self, field: str, value: str | None, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.value = value


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


# Matches the caps on :mod:`app.domain.tasks.templates` so an
# ad-hoc task carries the same field-length ceilings as a template-
# backed one — a template whose name fits is free to promote to a
# one-off task.
_MAX_TITLE_LEN = 200
_MAX_DESC_LEN = 20_000
_MAX_ID_LEN = 64
_MAX_INSTRUCTION_LINKS = 200


class TaskCreate(BaseModel):
    """Request body for ``POST /api/v1/tasks`` — one-off create.

    Two branches, gated by ``template_id``:

    * **Template-backed.** ``template_id`` is set; ``title``
      defaults to ``None`` and the service fills it from the
      template. Any explicit field (``title``, ``priority``, …)
      wins over the template's value.
    * **Template-less.** ``template_id`` is ``None``; ``title`` is
      required. Every other field carries its own default.

    ``scheduled_for_local`` is an ISO-8601 property-local timestamp
    (timezone-naive). The service projects to UTC using the
    property's ``timezone`` column; if no property is attached
    (``property_id is None``) we treat the value as UTC directly —
    a personal task without a property has no property frame to
    resolve against.

    The ``is_personal`` / ``assigned_user_id`` pairing is enforced
    here AND in the service: pydantic cannot see the caller's
    ``user_id`` (that lives on the :class:`WorkspaceContext`), so
    the DTO only rejects the impossible shapes (``is_personal=True``
    with an explicit mismatching assignee would still need the
    service to know the actor id to compare against).
    """

    model_config = ConfigDict(extra="forbid")

    # Template-backed path: every other body field optional.
    template_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)

    # Template-less path: these must all be carried explicitly. The
    # model_validator below enforces "title required when template_id
    # is None". Defaults match the template-layer conventions.
    title: str | None = Field(default=None, max_length=_MAX_TITLE_LEN)
    description_md: str | None = Field(default=None, max_length=_MAX_DESC_LEN)
    priority: Priority | None = None
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    area_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    unit_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    expected_role_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    assigned_user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    # ISO-8601 property-local timestamp (e.g. ``2026-04-20T09:00``).
    # Empty string is rejected by the ``min_length=1`` constraint.
    scheduled_for_local: str = Field(..., min_length=1, max_length=32)
    # Per-occurrence duration override. Nullable — a template-backed
    # task falls back to the template's value when unset.
    duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    photo_evidence: PhotoEvidence | None = None
    is_personal: bool = False
    # Override lists. When the caller passes ``None`` on a template-
    # backed path we copy from the template; an explicit ``[]`` means
    # "ignore the template's links" (caller's intent is explicit).
    linked_instruction_ids: list[str] | None = Field(
        default=None, max_length=_MAX_INSTRUCTION_LINKS
    )
    inventory_consumption_json: dict[str, int] | None = None

    @model_validator(mode="after")
    def _validate_branches(self) -> TaskCreate:
        """Enforce the template-less path's "title required" rule.

        The template-backed path can omit ``title``; the service
        copies from the template. A template-less task without a
        title is impossible to render. An explicit title (either
        branch) must carry a non-blank string — a whitespace-only
        override on the template-backed path would otherwise
        silently replace the template's name with an empty string
        in the row.
        """
        if self.title is not None and not self.title.strip():
            raise ValueError("title must be a non-blank string when provided")
        if self.template_id is None and self.title is None:
            raise ValueError("title is required when template_id is not set")
        if self.inventory_consumption_json is not None:
            for sku, qty in self.inventory_consumption_json.items():
                if qty <= 0:
                    raise ValueError(
                        f"inventory_consumption_json[{sku!r}]={qty} must be a "
                        "positive integer"
                    )
        return self


@dataclass(frozen=True, slots=True)
class TaskView:
    """Immutable read projection of a created one-off ``occurrence`` row.

    Mirrors the §06 "Task row" shape the router echoes back to the
    client: every column the ad-hoc service populates is surfaced
    (explicit nulls included so clients can tell "unset" from
    "missing field").
    """

    id: str
    workspace_id: str
    template_id: str | None
    schedule_id: str | None
    property_id: str | None
    area_id: str | None
    unit_id: str | None
    title: str
    description_md: str | None
    priority: Priority
    # The full §06 state machine. ``create_oneoff`` only ever stamps
    # ``'scheduled'`` or ``'pending'`` at insert time, but
    # :func:`read_task` / :func:`update_task` (cd-sn26) re-project the
    # same :class:`TaskView` for tasks that have since transitioned
    # through the completion service — so the Literal must cover the
    # full enum or the read path blows up with a narrowing error on
    # any non-fresh row.
    state: Literal[
        "scheduled",
        "pending",
        "in_progress",
        "done",
        "skipped",
        "cancelled",
        "overdue",
    ]
    scheduled_for_local: str
    scheduled_for_utc: datetime
    duration_minutes: int | None
    photo_evidence: PhotoEvidence
    linked_instruction_ids: tuple[str, ...]
    inventory_consumption_json: dict[str, int]
    expected_role_id: str | None
    assigned_user_id: str | None
    created_by: str
    is_personal: bool
    created_at: datetime
    # cd-hurw soft-overdue marker. ``None`` when the row is not
    # currently overdue (either it never slipped or a manual transition
    # cleared it). Carried on the view so :func:`TaskPayload.from_view`
    # can pin ``overdue=True`` from the column instead of re-computing
    # the time-derived projection — the column wins when present.
    overdue_since: datetime | None = None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_local_datetime(value: str) -> datetime:
    """Parse a property-local ISO-8601 timestamp (must be tz-naive).

    Matches the convention in
    :func:`app.domain.tasks.schedules._parse_local_datetime`:
    a tz-aware input is rejected explicitly rather than silently
    stripped — coercing a ``+02:00`` local-clock into naive
    ``09:00`` would hide authoring errors and produce tasks at the
    wrong wall-clock.
    """
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidLocalDatetime(
            f"scheduled_for_local must be an ISO-8601 local timestamp; got {value!r}"
        ) from exc
    if parsed.tzinfo is not None:
        raise InvalidLocalDatetime(
            "scheduled_for_local must be timezone-naive (property-local); "
            f"got tz-aware {value!r} — strip the zone and use the "
            "property's wall-clock time"
        )
    return parsed


def _resolve_property_zone(session: Session, property_id: str | None) -> ZoneInfo:
    """Resolve the property's IANA timezone; fall back to UTC.

    A personal task may carry ``property_id is None``; there is no
    property frame to project against, so we treat the local
    timestamp as UTC directly. A property with a junk timezone is a
    data bug (§04 property CRUD owns validation); falling back to
    UTC here keeps the one-off path unblocked while the manager fixes
    the row. Matches the convention in
    :func:`app.worker.tasks.generator._resolve_zone`.
    """
    if property_id is None:
        return ZoneInfo("UTC")
    stmt = select(Property).where(Property.id == property_id)
    row = session.scalars(stmt).one_or_none()
    if row is None:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(row.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _to_utc(candidate_local: datetime, zone: ZoneInfo) -> datetime:
    """Attach ``zone`` to ``candidate_local`` and project to UTC.

    Matches :func:`app.worker.tasks.generator._to_utc`; kept local
    here rather than imported so the one-off service does not reach
    into the worker module's private API.
    """
    aware = candidate_local.replace(tzinfo=zone)
    return aware.astimezone(ZoneInfo("UTC"))


def _iso_local(candidate_local: datetime) -> str:
    """Render a naive local datetime as ``YYYY-MM-DDTHH:MM:SS``."""
    return candidate_local.replace(microsecond=0).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Resolved-payload projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Resolved:
    """Resolved field set applied to the inserted row + audit payload.

    Split out from :func:`create_oneoff` so the template-copy logic
    has a single return shape the inserter trusts. Narrowing the
    priority / photo_evidence strings to their :class:`Literal` types
    is done here so the inserter never sees a plain ``str``.
    """

    title: str
    description_md: str | None
    priority: Priority
    photo_evidence: PhotoEvidence
    duration_minutes: int | None
    linked_instruction_ids: tuple[str, ...]
    inventory_consumption_json: dict[str, int]


def _resolve_fields(
    payload: TaskCreate,
    *,
    template: TaskTemplate | None,
) -> _Resolved:
    """Copy template defaults and layer payload overrides on top.

    Rules (§06 spec):

    * ``template_id`` present → copy ``title``, ``description_md``,
      ``priority``, ``photo_evidence``, ``duration_minutes``,
      ``linked_instruction_ids``, ``inventory_consumption_json``
      from the template.
    * Every explicit payload field wins over the template copy.
    * Template-less → use payload values + sane defaults
      (``priority='normal'``, ``photo_evidence='disabled'``, empty
      lists / dict).
    """
    if template is not None:
        base_title = template.name if template.name is not None else template.title
        base_description = template.description_md or None
        base_priority = _narrow_priority(template.priority)
        base_photo = _narrow_photo_evidence(template.photo_evidence)
        base_duration = (
            template.duration_minutes
            if template.duration_minutes is not None
            else template.default_duration_min
        )
        base_links = tuple(template.linked_instruction_ids or [])
        base_inventory = dict(template.inventory_consumption_json or {})
    else:
        base_title = ""  # model_validator guarantees payload.title is set
        base_description = None
        base_priority = "normal"
        base_photo = "disabled"
        base_duration = None
        base_links = ()
        base_inventory = {}

    title = payload.title.strip() if payload.title is not None else base_title
    description_md = (
        payload.description_md
        if payload.description_md is not None
        else base_description
    )
    priority = payload.priority if payload.priority is not None else base_priority
    photo_evidence = (
        payload.photo_evidence if payload.photo_evidence is not None else base_photo
    )
    duration_minutes = (
        payload.duration_minutes
        if payload.duration_minutes is not None
        else base_duration
    )
    linked_instruction_ids = (
        tuple(payload.linked_instruction_ids)
        if payload.linked_instruction_ids is not None
        else base_links
    )
    inventory_consumption_json = (
        dict(payload.inventory_consumption_json)
        if payload.inventory_consumption_json is not None
        else base_inventory
    )

    return _Resolved(
        title=title,
        description_md=description_md,
        priority=priority,
        photo_evidence=photo_evidence,
        duration_minutes=duration_minutes,
        linked_instruction_ids=linked_instruction_ids,
        inventory_consumption_json=inventory_consumption_json,
    )


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_template(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str,
) -> TaskTemplate:
    """Load a live template scoped to the caller's workspace.

    Raises :class:`TaskTemplateNotFound` (``LookupError``) when the
    template is missing or soft-deleted — matches
    :func:`app.domain.tasks.templates.read` so the router maps both
    to the same 404.
    """
    stmt = (
        select(TaskTemplate)
        .where(TaskTemplate.id == template_id)
        .where(TaskTemplate.workspace_id == ctx.workspace_id)
        .where(TaskTemplate.deleted_at.is_(None))
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise TaskTemplateNotFound(template_id)
    return row


# ---------------------------------------------------------------------------
# Service entry point
# ---------------------------------------------------------------------------


def create_oneoff(
    session: Session,
    ctx: WorkspaceContext,
    *,
    payload: TaskCreate,
    clock: Clock | None = None,
    rule_repo: PermissionRuleRepository | None = None,
    expand_checklist: ChecklistExpansionHook | None = None,
    assign: AssignmentHook | None = None,
    event_bus: EventBus | None = None,
) -> TaskView:
    """Create a single one-off task and record one audit + one or two events.

    Flow:

    1. Re-assert ``tasks.create`` on the payload's scope (§05
       action catalog). Defence-in-depth against a router that
       forgot to wire the :class:`Permission` dep.
    2. Enforce the ``is_personal`` / ``assigned_user_id`` pairing:
       personal tasks must be self-assigned to ``ctx.actor_id``.
    3. Load the template (if any); copy its fields under the
       payload's overrides.
    4. Project ``scheduled_for_local`` to UTC via
       ``property.timezone``.
    5. Choose ``state``: ``'pending'`` when
       ``scheduled_for_local <= now`` (immediately actionable), else
       ``'scheduled'`` (the scheduler worker flips it later).
    6. Insert the row; run the checklist hook (``is_ad_hoc=True``).
    7. If ``assigned_user_id is None and expected_role_id`` is set,
       call the assignment hook. A returned user id is applied to
       the row.
    8. Write one ``task.create_oneoff`` audit row.
    9. Publish ``task.created``; publish ``task.assigned`` iff the
       final assignee is not ``None``.

    Returns the :class:`TaskView` the router echoes back. The
    function never commits — the caller's Unit-of-Work owns
    transaction boundaries.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    if now.tzinfo is None:
        raise ValueError("clock.now() must return an aware UTC datetime")

    # --- Permission (defence-in-depth). ---------------------------
    _assert_can_create(
        session, ctx, property_id=payload.property_id, rule_repo=rule_repo
    )

    # --- Personal-assignment pairing. -----------------------------
    if payload.is_personal and payload.assigned_user_id != ctx.actor_id:
        raise PersonalAssignmentError(
            "is_personal=True requires assigned_user_id == ctx.actor_id; "
            f"got assigned_user_id={payload.assigned_user_id!r}, "
            f"ctx.actor_id={ctx.actor_id!r}"
        )

    # --- Template copy + payload override. -----------------------
    template: TaskTemplate | None = None
    if payload.template_id is not None:
        template = _load_template(session, ctx, template_id=payload.template_id)
    resolved = _resolve_fields(payload, template=template)

    # --- Scheduling (local → UTC). --------------------------------
    candidate_local = _parse_local_datetime(payload.scheduled_for_local)
    zone = _resolve_property_zone(session, payload.property_id)
    scheduled_for_utc = _to_utc(candidate_local, zone)
    scheduled_for_local_iso = _iso_local(candidate_local)

    # --- State machine. §06: ``scheduled`` when future, ``pending``
    # when the start is already past. A one-off with past
    # ``scheduled_for_local`` is immediately actionable — the
    # scheduler worker's ``scheduled → pending`` flip would otherwise
    # never fire for a past task.
    state: Literal["scheduled", "pending"] = (
        "pending" if scheduled_for_utc <= now else "scheduled"
    )

    duration_minutes = resolved.duration_minutes
    # ``Occurrence.ends_at`` is NOT NULL and CHECK > ``starts_at``;
    # a missing or zero duration still needs a strictly-later
    # ``ends_at`` to satisfy the CHECK. Per §06 "Task row" readers
    # fall back to ``ends_at - starts_at`` when ``duration_minutes``
    # is NULL — picking a 30-minute placeholder (matching the
    # :class:`app.domain.tasks.templates.TaskTemplateCreate` default)
    # keeps that fallback meaningful; a 1-minute placeholder would
    # look like a fat-finger on every reader surface.
    if duration_minutes is not None and duration_minutes > 0:
        effective_duration = duration_minutes
    else:
        effective_duration = 30
    ends_at = scheduled_for_utc + timedelta(minutes=effective_duration)

    # --- Insert. --------------------------------------------------
    occurrence_id = new_ulid()
    row = Occurrence(
        id=occurrence_id,
        workspace_id=ctx.workspace_id,
        schedule_id=None,
        template_id=template.id if template is not None else None,
        property_id=payload.property_id,
        assignee_user_id=payload.assigned_user_id,
        starts_at=scheduled_for_utc,
        ends_at=ends_at,
        scheduled_for_local=scheduled_for_local_iso,
        originally_scheduled_for=scheduled_for_local_iso,
        state=state,
        cancellation_reason=None,
        title=resolved.title,
        description_md=resolved.description_md,
        priority=resolved.priority,
        photo_evidence=resolved.photo_evidence,
        duration_minutes=duration_minutes,
        area_id=payload.area_id,
        unit_id=payload.unit_id,
        expected_role_id=payload.expected_role_id,
        linked_instruction_ids=list(resolved.linked_instruction_ids),
        inventory_consumption_json=dict(resolved.inventory_consumption_json),
        is_personal=payload.is_personal,
        created_by_user_id=ctx.actor_id,
        created_at=now,
    )
    session.add(row)
    session.flush()

    # --- Seed checklist items. ``is_ad_hoc=True`` matches §06
    # "Ad-hoc tasks always include every item". Template-less tasks
    # have no checklist payload to seed, so the hook is only called
    # when a template was loaded.
    if template is not None:
        resolved_expand = (
            expand_checklist if expand_checklist is not None else _noop_expand_checklist
        )
        resolved_expand(session, ctx, row.id, template, True)

    # --- Assignment fallback. Only runs when the caller left
    # ``assigned_user_id`` null AND an ``expected_role_id`` is
    # available. The hook returns the chosen user id or ``None``.
    resolved_assign = assign if assign is not None else _noop_assign
    if payload.assigned_user_id is None and payload.expected_role_id is not None:
        chosen = resolved_assign(session, ctx, row.id)
        if chosen is not None:
            row.assignee_user_id = chosen
            session.flush()

    view = _row_to_view(row)

    # --- Audit. --------------------------------------------------
    write_audit(
        session,
        ctx,
        entity_kind="task",
        entity_id=row.id,
        action="task.create_oneoff",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )

    # --- Events. -------------------------------------------------
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    resolved_bus.publish(
        TaskCreated(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            task_id=row.id,
        )
    )
    if view.assigned_user_id is not None:
        resolved_bus.publish(
            TaskAssigned(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=resolved_clock.now(),
                task_id=row.id,
                assigned_to=view.assigned_user_id,
            )
        )

    return view


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------


def _assert_can_create(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None,
    rule_repo: PermissionRuleRepository | None,
) -> None:
    """Re-assert the ``tasks.create`` action catalog entry.

    The router's :class:`app.authz.dep.Permission` dependency
    already gates the endpoint; this re-check is defence-in-depth
    for service-layer callers (CLI, agent runtime, tests) that
    don't flow through the router. Scope is ``property`` when the
    payload carries one, ``workspace`` otherwise — matching the
    action's ``valid_scope_kinds = ("workspace", "property")``.
    """
    repo = rule_repo if rule_repo is not None else EmptyPermissionRuleRepository()
    if property_id is not None:
        require(
            session,
            ctx,
            action_key="tasks.create",
            scope_kind="property",
            scope_id=property_id,
            rule_repo=repo,
        )
    else:
        require(
            session,
            ctx,
            action_key="tasks.create",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
            rule_repo=repo,
        )


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


_TaskStateName = Literal[
    "scheduled",
    "pending",
    "in_progress",
    "done",
    "skipped",
    "cancelled",
    "overdue",
]


def _narrow_task_state(value: str) -> _TaskStateName:
    """Narrow a loaded ``occurrence.state`` string to the §06 enum.

    Kept local to this module (rather than imported from
    :mod:`app.domain.tasks.completion`) so the one-off / read path
    does not pick up a circular import through the completion
    module. The CHECK constraint on the column rules out new values
    in practice; a row that slips past it is a schema-drift bug and
    warrants loud failure rather than a silent default.
    """
    if value == "scheduled":
        return "scheduled"
    if value == "pending":
        return "pending"
    if value == "in_progress":
        return "in_progress"
    if value == "done":
        return "done"
    if value == "skipped":
        return "skipped"
    if value == "cancelled":
        return "cancelled"
    if value == "overdue":
        return "overdue"
    raise ValueError(f"unexpected occurrence.state {value!r} on loaded row")


def _row_to_view(row: Occurrence) -> TaskView:
    """Project an :class:`Occurrence` row into a :class:`TaskView`.

    Narrowing the enum columns to their :class:`Literal` types goes
    through the templates module's :func:`_narrow_priority` /
    :func:`_narrow_photo_evidence` helpers so the one-off service
    doesn't reimplement the same per-value ``if`` chain; the state
    narrowing is module-local via :func:`_narrow_task_state`.

    Accepts every §06 state (not just ``'scheduled'`` / ``'pending'``)
    because the HTTP read path (:func:`read_task` + :func:`update_task`)
    calls through for tasks that have moved through the completion
    state machine.
    """
    narrowed_state = _narrow_task_state(row.state)
    return TaskView(
        id=row.id,
        workspace_id=row.workspace_id,
        template_id=row.template_id,
        schedule_id=row.schedule_id,
        property_id=row.property_id,
        area_id=row.area_id,
        unit_id=row.unit_id,
        title=row.title if row.title is not None else "",
        description_md=row.description_md,
        priority=_narrow_priority(row.priority),
        state=narrowed_state,
        scheduled_for_local=row.scheduled_for_local or "",
        scheduled_for_utc=_ensure_utc(row.starts_at),
        duration_minutes=row.duration_minutes,
        photo_evidence=_narrow_photo_evidence(row.photo_evidence),
        linked_instruction_ids=tuple(row.linked_instruction_ids or []),
        inventory_consumption_json=dict(row.inventory_consumption_json or {}),
        expected_role_id=row.expected_role_id,
        assigned_user_id=row.assignee_user_id,
        created_by=row.created_by_user_id or "",
        is_personal=row.is_personal,
        created_at=_ensure_utc(row.created_at),
        overdue_since=_ensure_utc(row.overdue_since)
        if row.overdue_since is not None
        else None,
    )


def _ensure_utc(value: datetime) -> datetime:
    """Narrow a round-tripped ``DateTime(timezone=True)`` to aware UTC.

    SQLite strips tzinfo off ``DateTime(timezone=True)`` columns on
    read; Postgres preserves it. The column is always written as
    aware UTC, so a naive read is a UTC value that lost its zone.
    The event validators require aware UTC; coerce here before the
    view escapes.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Read + partial update (HTTP-layer fallback for cd-sn26)
# ---------------------------------------------------------------------------


class TaskPatch(BaseModel):
    """Partial update DTO for ``PATCH /api/v1/tasks/{id}``.

    Carries the full §06 "Task row" mutable set (cd-43wv widened
    cd-sn26's narrow title-only DTO):

    * ``title`` — the one-liner the worker reads in the card.
    * ``description_md`` — the long-form body rendered below it.
    * ``scheduled_for_local`` — property-local ISO-8601 timestamp;
      :func:`update_task` projects to ``scheduled_for_utc`` via the
      property's IANA timezone and re-runs the §06
      ``scheduled ↔ pending`` state gate so the row matches its
      new clock. Dedicated reschedule paths
      (``/scheduler/tasks/{id}/reschedule``) handle the cross-cutting
      availability + rota re-resolution; the generic PATCH simply
      moves the task and emits ``task.updated``.
    * ``property_id`` / ``area_id`` / ``unit_id`` — the location
      tuple. Each field is validated individually but their
      relationship is also enforced (area must belong to the
      resolved property; unit must belong to the resolved property).
      Reassigning the task to the new property's worker pool is a
      separate verb (``/scheduler/tasks/{id}/reassign``); PATCH
      does not silently re-resolve the assignee.
    * ``expected_role_id`` — must be a live :class:`WorkRole` in
      the caller's workspace.
    * ``priority`` — `low | normal | high | urgent`.
    * ``duration_minutes`` — clamped to ``[1, 1440]``.
    * ``photo_evidence`` — `disabled | optional | required`.

    Omitted fields keep their current value; an explicit ``null`` on a
    nullable column clears it. The ``model_fields_set`` introspection
    the router uses preserves "field not sent" vs "field sent as
    null", so the service can differentiate the two shapes.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=_MAX_TITLE_LEN)
    description_md: str | None = Field(default=None, max_length=_MAX_DESC_LEN)
    # ISO-8601 property-local timestamp; ``min_length=1`` rejects an
    # empty string. The service rejects tz-aware values via
    # :func:`_parse_local_datetime`, matching the create path.
    scheduled_for_local: str | None = Field(default=None, min_length=1, max_length=32)
    # Each location id is nullable on the Occurrence row, so an
    # explicit ``null`` clears the column.
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    area_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    unit_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    expected_role_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    priority: Priority | None = None
    # Matches :class:`TaskCreate.duration_minutes`: same caps so a
    # patched ad-hoc task carries the same field-length ceilings as
    # a freshly created one.
    duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    photo_evidence: PhotoEvidence | None = None

    @model_validator(mode="after")
    def _validate_title_non_blank(self) -> TaskPatch:
        """Reject a whitespace-only ``title`` patch.

        Mirrors :meth:`TaskCreate._validate_branches`: an explicit
        ``title="   "`` would survive ``min_length=1`` (three chars)
        and the service would then ``.strip()`` it down to an empty
        string, silently overwriting the row's title with ``''``.
        Raise here so the SPA pins the validation to the input
        rather than the row landing in an unrenderable state.
        """
        if self.title is not None and not self.title.strip():
            raise ValueError("title must be a non-blank string when provided")
        return self


def read_task(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
) -> TaskView:
    """Return the :class:`TaskView` for ``task_id`` in the caller's workspace.

    Cross-tenant lookups collapse to :class:`TaskNotFound` (404) so
    the router never leaks the mere existence of another workspace's
    row. The personal-task gate is enforced by the §15 read layer on
    list endpoints; for a direct ``GET`` the spec treats
    "I created it" / "owner member" as the visibility rule, so we
    re-apply it here defensively.
    """
    row = session.scalar(
        select(Occurrence).where(
            Occurrence.id == task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise TaskNotFound(task_id)
    if (
        row.is_personal
        and not ctx.actor_was_owner_member
        and row.created_by_user_id != ctx.actor_id
    ):
        raise TaskNotFound(task_id)
    return _row_to_view(row)


def update_task(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
    body: TaskPatch,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> TaskView:
    """Rewrite the §06 mutable fields on ``task_id``, audit, and emit.

    Walks ``body.model_fields_set`` to distinguish "omit" from "null",
    applies the sent fields under per-field validation, recomputes
    ``scheduled_for_utc`` + the ``scheduled ↔ pending`` state gate
    when the local timestamp moves, and publishes
    :class:`TaskUpdated` on every successful row mutation so SSE
    subscribers refresh.

    The §06 mutable set covered here:

    * ``title`` / ``description_md`` — text fields (cd-sn26 narrow
      slice; preserved verbatim for callers that only patch text).
    * ``scheduled_for_local`` — recomputed against the resolved
      property's timezone (the row's existing ``property_id`` if the
      patch leaves it unset, else the patch's new ``property_id``).
      The §06 state gate runs after the recompute: a task whose new
      local timestamp is past now flips to ``pending``; a task
      moved into the future flips back to ``scheduled``. Tasks that
      have left the auto-flip range (``in_progress`` / ``done`` /
      ``skipped`` / ``cancelled``) keep their state — the worker
      already started or the task is closed; a passive PATCH must
      not undo a deliberate state move.
    * ``property_id`` / ``area_id`` / ``unit_id`` — validated
      against :class:`PropertyWorkspace` (workspace linkage),
      :class:`Area.property_id`, and :class:`Unit.property_id`.
      Reassignment / availability re-resolution lives on the
      dedicated reschedule + reassign verbs (§12); PATCH only
      validates and writes through.
    * ``expected_role_id`` — must reference a live
      :class:`WorkRole` in the caller's workspace.
    * ``priority`` / ``duration_minutes`` / ``photo_evidence`` —
      direct column writes; pydantic narrows the enum and clamps
      the duration on the DTO.

    Raises:

    * :class:`TaskNotFound` when the id is not visible in the
      caller's workspace.
    * :class:`TaskFieldInvalid` on a field-level violation
      (cross-workspace property, area outside property, unit
      outside property, role outside workspace). The router maps
      this to ``422 invalid_task_field``.
    * :class:`ValueError` (from the local-datetime parser) on a
      malformed ``scheduled_for_local`` payload — preserved as a
      separate exception so the router can branch.

    Permission gating is the router's job; the service defends
    against cross-tenant reads via the loader below.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    if now.tzinfo is None:
        raise ValueError("clock.now() must return an aware UTC datetime")
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    row = session.scalar(
        select(Occurrence).where(
            Occurrence.id == task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise TaskNotFound(task_id)

    before = _row_to_view(row)
    sent = body.model_fields_set

    # --- Resolve the location tuple first. The downstream area /
    # unit checks need the *post-patch* property_id (else patching
    # property + area in one call would validate the area against
    # the OLD property and silently let through a mismatch).
    if "property_id" in sent:
        new_property_id = body.property_id
        if new_property_id is not None:
            _assert_property_in_workspace(session, ctx, property_id=new_property_id)
    else:
        new_property_id = row.property_id

    # Resolve the post-patch area / unit too — a property-only PATCH
    # must not strand an existing area_id / unit_id pointing at the
    # *old* property. The caller has to either clear them in the same
    # call or repoint them to a row under the new property; either is
    # OK, silent inconsistency is not.
    new_area_id = body.area_id if "area_id" in sent else row.area_id
    new_unit_id = body.unit_id if "unit_id" in sent else row.unit_id

    if new_area_id is not None and ("area_id" in sent or "property_id" in sent):
        _assert_area_belongs_to_property(
            session, area_id=new_area_id, property_id=new_property_id
        )
    if new_unit_id is not None and ("unit_id" in sent or "property_id" in sent):
        _assert_unit_belongs_to_property(
            session, unit_id=new_unit_id, property_id=new_property_id
        )

    # --- expected_role_id validation (§05 work_role catalogue is
    # workspace-scoped; cross-workspace borrowing is forbidden).
    if "expected_role_id" in sent and body.expected_role_id is not None:
        _assert_work_role_in_workspace(session, ctx, work_role_id=body.expected_role_id)

    # --- Apply the writes. ---------------------------------------
    if "title" in sent and body.title is not None:
        row.title = body.title.strip()
    if "description_md" in sent:
        row.description_md = body.description_md
    if "property_id" in sent:
        row.property_id = body.property_id
    if "area_id" in sent:
        row.area_id = body.area_id
    if "unit_id" in sent:
        row.unit_id = body.unit_id
    if "expected_role_id" in sent:
        row.expected_role_id = body.expected_role_id
    if "priority" in sent and body.priority is not None:
        row.priority = body.priority
    if "photo_evidence" in sent and body.photo_evidence is not None:
        row.photo_evidence = body.photo_evidence
    if "duration_minutes" in sent:
        row.duration_minutes = body.duration_minutes

    # --- Schedule-related recompute. ``scheduled_for_local`` drives
    # both the row's local string and the UTC mirror; the state
    # machine flips between ``scheduled`` and ``pending`` based on
    # the new UTC anchor. A property change without a
    # ``scheduled_for_local`` patch ALSO has to recompute the UTC
    # mirror because the timezone the local string projects through
    # has moved (a task at "2026-04-19T14:00 local" reads 13:00 UTC
    # under Europe/London but 12:00 UTC under Europe/Paris).
    schedule_recomputed = False
    if "scheduled_for_local" in sent and body.scheduled_for_local is not None:
        candidate_local = _parse_local_datetime(body.scheduled_for_local)
        zone = _resolve_property_zone(session, new_property_id)
        new_starts_at = _to_utc(candidate_local, zone)
        new_local_iso = _iso_local(candidate_local)
        row.scheduled_for_local = new_local_iso
        _apply_starts_at(row, new_starts_at)
        schedule_recomputed = True
    elif "property_id" in sent and row.scheduled_for_local:
        # Property changed without a scheduled_for_local patch — the
        # local wall-clock string stays put but the UTC mirror has
        # to follow the new timezone. Bypass the parser failure
        # branch: the row's existing ``scheduled_for_local`` was
        # validated when it was first written, so a parse error here
        # is a data bug, not a caller bug.
        try:
            existing_local = _parse_local_datetime(row.scheduled_for_local)
        except ValueError:
            existing_local = None
        if existing_local is not None:
            zone = _resolve_property_zone(session, new_property_id)
            _apply_starts_at(row, _to_utc(existing_local, zone))
            schedule_recomputed = True

    if schedule_recomputed:
        _maybe_flip_schedule_state(row, now=now)

    session.flush()
    after = _row_to_view(row)

    # Audit only when a field genuinely changed. A PATCH that lands
    # with an explicit null on an already-null column (or the same
    # string on an unchanged column) would otherwise spam the audit
    # log with zero-delta rows. Skipping the empty-sent case on its
    # own is insufficient — ``model_fields_set`` tracks *sent*, not
    # *changed*.
    before_dict = _view_to_diff_dict(before)
    after_dict = _view_to_diff_dict(after)
    if before_dict == after_dict:
        return after

    write_audit(
        session,
        ctx,
        entity_kind="task",
        entity_id=row.id,
        action="task.update",
        diff={
            "before": before_dict,
            "after": after_dict,
        },
        clock=resolved_clock,
    )

    changed = _changed_fields(before_dict, after_dict)
    resolved_bus.publish(
        TaskUpdated(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            task_id=row.id,
            changed_fields=changed,
        )
    )
    return after


# ---------------------------------------------------------------------------
# update_task helpers
# ---------------------------------------------------------------------------


# §06 mutable columns surfaced on :class:`TaskUpdated.changed_fields`.
# The set excludes derived / read-only fields (``id``, ``workspace_id``,
# ``created_at``, ``created_by``) so a no-op caller-driven PATCH that
# happens to round-trip a derived value cannot pollute the SSE
# payload.
_MUTABLE_DIFF_FIELDS: tuple[str, ...] = (
    "title",
    "description_md",
    "scheduled_for_local",
    "scheduled_for_utc",
    "property_id",
    "area_id",
    "unit_id",
    "expected_role_id",
    "priority",
    "duration_minutes",
    "photo_evidence",
    "state",
)


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    """Return the §06 mutable columns whose value moved during the patch."""
    return tuple(
        field for field in _MUTABLE_DIFF_FIELDS if before.get(field) != after.get(field)
    )


def _apply_starts_at(row: Occurrence, new_starts_at: datetime) -> None:
    """Move ``starts_at`` and slide ``ends_at`` to preserve duration.

    The DB CHECK ``ends_at > starts_at`` rejects an out-of-order pair,
    so we re-derive ``ends_at`` from the row's existing duration
    (falling back to the create-path's 30-minute placeholder when
    ``duration_minutes`` is unset). Rewriting ``ends_at`` from the new
    UTC anchor keeps the window length stable across reschedules.
    """
    if row.duration_minutes is not None and row.duration_minutes > 0:
        effective_duration = row.duration_minutes
    else:
        effective_duration = 30
    row.starts_at = new_starts_at
    row.ends_at = new_starts_at + timedelta(minutes=effective_duration)


def _maybe_flip_schedule_state(row: Occurrence, *, now: datetime) -> None:
    """Re-run the §06 ``scheduled ↔ pending`` gate after a reschedule.

    The state machine only auto-flips between ``scheduled`` and
    ``pending`` on the schedule axis: a task that has moved to
    ``in_progress`` / ``done`` / ``skipped`` / ``cancelled`` /
    ``overdue`` keeps its state across a PATCH (the worker has
    started it, the task is closed, or the sweeper soft-flipped it).
    A ``scheduled`` task whose new ``starts_at`` is past now becomes
    ``pending``; a ``pending`` task whose new ``starts_at`` is in
    the future returns to ``scheduled`` so the SPA sorts it back
    into the schedule view.

    Per §06: "``scheduled`` → ``pending`` happens at
    ``scheduled_for_utc - 1h`` (or immediately for one-offs created
    with a past ``scheduled_for``)". Reproducing the 1-hour lead
    time on a generic PATCH would couple the route to the
    scheduler-worker's tick cadence; we keep the boundary at
    ``starts_at <= now`` here (matching the create path) and rely
    on the scheduler tick to do the lead-time flip on un-patched
    tasks.
    """
    if row.state == "scheduled" and row.starts_at <= now:
        row.state = "pending"
    elif row.state == "pending" and row.starts_at > now:
        row.state = "scheduled"


def _assert_property_in_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> None:
    """Raise :class:`TaskFieldInvalid` if the property isn't workspace-linked.

    Mirrors
    :func:`app.domain.identity.role_grants._assert_scope_property_in_workspace`
    and
    :func:`app.domain.places.property_work_role_assignments._assert_property_in_workspace`
    but lifts to the task-domain exception so the router can map
    every per-field failure under one HTTP shape.
    """
    row = session.scalar(
        select(PropertyWorkspace).where(
            PropertyWorkspace.property_id == property_id,
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise TaskFieldInvalid(
            field="property_id",
            value=property_id,
            message=(f"property {property_id!r} is not linked to this workspace"),
        )


def _assert_area_belongs_to_property(
    session: Session,
    *,
    area_id: str,
    property_id: str | None,
) -> None:
    """Raise :class:`TaskFieldInvalid` when the area is for a different property.

    A patch that sets ``area_id`` without a resolved property is a
    domain bug — areas are scoped to a property in the v1 schema
    (``area.property_id`` is NOT NULL). Surface the violation as a
    422 with the offending ``area_id`` rather than letting the row
    write a dangling reference.
    """
    if property_id is None:
        raise TaskFieldInvalid(
            field="area_id",
            value=area_id,
            message=(
                "area_id requires a resolved property_id; the task has none "
                "and the patch did not set one"
            ),
        )
    row = session.scalar(
        select(Area).where(
            Area.id == area_id,
            Area.property_id == property_id,
        )
    )
    if row is None:
        raise TaskFieldInvalid(
            field="area_id",
            value=area_id,
            message=(f"area {area_id!r} does not belong to property {property_id!r}"),
        )


def _assert_unit_belongs_to_property(
    session: Session,
    *,
    unit_id: str,
    property_id: str | None,
) -> None:
    """Raise :class:`TaskFieldInvalid` when the unit is for a different property.

    The §04 spec leaves room for ``unit.area_id`` (cd-8u5) and the
    PATCH spec wording reads "unit belongs to area or property"; the
    v1 schema only models ``unit.property_id``, so the property
    membership check is the live invariant. When the area-scoped
    units land we extend this helper rather than introducing a
    parallel one.
    """
    if property_id is None:
        raise TaskFieldInvalid(
            field="unit_id",
            value=unit_id,
            message=(
                "unit_id requires a resolved property_id; the task has none "
                "and the patch did not set one"
            ),
        )
    row = session.scalar(
        select(Unit).where(
            Unit.id == unit_id,
            Unit.property_id == property_id,
        )
    )
    if row is None:
        raise TaskFieldInvalid(
            field="unit_id",
            value=unit_id,
            message=(f"unit {unit_id!r} does not belong to property {property_id!r}"),
        )


def _assert_work_role_in_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    work_role_id: str,
) -> None:
    """Raise :class:`TaskFieldInvalid` when the role isn't a live workspace row.

    Soft-deleted roles (``deleted_at IS NOT NULL``) are also rejected:
    a patched task pointing at a retired role would never resolve an
    assignee through the §06 algorithm and the SPA would render an
    empty role chip indefinitely. The §05 archive flow leaves
    historical tasks pointing at the retired role; opting NEW
    references back into the row is what the workspace owner has to
    explicitly avoid.
    """
    row = session.scalar(
        select(WorkRole).where(
            WorkRole.id == work_role_id,
            WorkRole.workspace_id == ctx.workspace_id,
            WorkRole.deleted_at.is_(None),
        )
    )
    if row is None:
        raise TaskFieldInvalid(
            field="expected_role_id",
            value=work_role_id,
            message=(f"work_role {work_role_id!r} is not a live row in this workspace"),
        )


def _view_to_diff_dict(view: TaskView) -> dict[str, Any]:
    """Flatten a :class:`TaskView` into a JSON-safe audit payload."""
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "template_id": view.template_id,
        "schedule_id": view.schedule_id,
        "property_id": view.property_id,
        "area_id": view.area_id,
        "unit_id": view.unit_id,
        "title": view.title,
        "description_md": view.description_md,
        "priority": view.priority,
        "state": view.state,
        "scheduled_for_local": view.scheduled_for_local,
        "scheduled_for_utc": view.scheduled_for_utc.isoformat(),
        "duration_minutes": view.duration_minutes,
        "photo_evidence": view.photo_evidence,
        "linked_instruction_ids": list(view.linked_instruction_ids),
        "inventory_consumption_json": dict(view.inventory_consumption_json),
        "expected_role_id": view.expected_role_id,
        "assigned_user_id": view.assigned_user_id,
        "created_by": view.created_by,
        "is_personal": view.is_personal,
        "created_at": view.created_at.isoformat(),
    }
