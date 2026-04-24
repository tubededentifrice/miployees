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
``tasks.create`` :class:`app.authz.enforce.Permission` dependency;
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

from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
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
from app.events.types import TaskAssigned, TaskCreated
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AssignmentHook",
    "ChecklistExpansionHook",
    "PersonalAssignmentError",
    "TaskCreate",
    "TaskTemplateNotFound",
    "TaskView",
    "create_oneoff",
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
    state: Literal["scheduled", "pending"]
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
        raise ValueError(
            f"scheduled_for_local must be an ISO-8601 local timestamp; got {value!r}"
        ) from exc
    if parsed.tzinfo is not None:
        raise ValueError(
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

    The router's :class:`app.authz.enforce.Permission` dependency
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


def _row_to_view(row: Occurrence) -> TaskView:
    """Project a freshly-inserted :class:`Occurrence` row into a view.

    Narrowing the enum columns to their :class:`Literal` types goes
    through the templates module's :func:`_narrow_priority` /
    :func:`_narrow_photo_evidence` helpers so the one-off service
    doesn't reimplement the same per-value ``if`` chain.
    """
    # The service only writes ``'scheduled'`` or ``'pending'``; any
    # other value on a freshly-inserted row is a schema-drift bug
    # that warrants loud failure rather than silent downgrade. The
    # per-value returns are what narrow ``str`` to the
    # :class:`Literal` without a ``cast`` or ``# type: ignore``.
    narrowed_state: Literal["scheduled", "pending"]
    if row.state == "scheduled":
        narrowed_state = "scheduled"
    elif row.state == "pending":
        narrowed_state = "pending"
    else:
        raise ValueError(f"unexpected state {row.state!r} on fresh one-off occurrence")
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
