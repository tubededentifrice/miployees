"""Tasks context router — templates, schedules, occurrences, comments, evidence.

Mounted by the app factory under ``/w/<slug>/api/v1/tasks``. The tag
``tasks`` is declared on the module-level router so every route the
factory's per-resource sub-routers attach inherits it without having to
repeat the declaration (parity with the ``identity`` + ``time`` routers
that each sit under one tag).

Surface (spec §12 "Tasks / templates / schedules"):

**Templates** — ``app.domain.tasks.templates``

* ``GET    /task_templates`` (cursor-paginated)
* ``POST   /task_templates``
* ``GET    /task_templates/{id}``
* ``PATCH  /task_templates/{id}``
* ``DELETE /task_templates/{id}`` — soft-delete; 409 ``template_in_use``.

**Schedules** — ``app.domain.tasks.schedules``

* ``GET    /schedules`` (cursor-paginated)
* ``POST   /schedules``
* ``GET    /schedules/{id}``
* ``PATCH  /schedules/{id}``
* ``DELETE /schedules/{id}``
* ``GET    /schedules/{id}/preview?for=30d``
* ``POST   /schedules/{id}/pause``
* ``POST   /schedules/{id}/resume``

**Occurrences (product-label "tasks")** — ``app.domain.tasks.{oneoff,
completion,assignment,comments}``

* ``GET    /tasks`` (cursor-paginated; filters: ``state``,
  ``assignee_user_id``, ``property_id``,
  ``scheduled_for_utc_gte`` / ``scheduled_for_utc_lt``).
* ``POST   /tasks`` — ad-hoc create.
* ``GET    /tasks/{id}`` — 404 on cross-tenant.
* ``PATCH  /tasks/{id}`` — narrow PATCH (title + description_md) —
  the full mutable set lands with cd-task-patch-wider.
* ``POST   /tasks/{id}/assign`` — assign to an explicit user.
* ``POST   /tasks/{id}/start``
* ``POST   /tasks/{id}/complete``
* ``POST   /tasks/{id}/skip``
* ``POST   /tasks/{id}/cancel``
* ``POST   /tasks/{id}/comments``
* ``GET    /tasks/{id}/comments``
* ``PATCH  /tasks/{id}/comments/{comment_id}``
* ``DELETE /tasks/{id}/comments/{comment_id}``
* ``GET    /tasks/{id}/evidence``
* ``POST   /tasks/{id}/evidence`` — accepts ``multipart/form-data``;
  every §06 evidence kind (``note`` / ``photo`` / ``voice`` / ``gps``)
  is wired end-to-end. ``note`` rides the inline ``note_md`` form
  field; ``photo`` / ``voice`` / ``gps`` route through the
  content-addressed :class:`Storage` port (SHA-256 blob hash +
  server-side MIME sniff + per-kind size cap per spec
  §15 "Input validation"). The sniffed MIME (not the multipart
  header) is what the per-kind allow-list rejects against; ``gps``
  carries a small JSON document with ``lat`` / ``lon`` / optional
  ``accuracy_m``.

**Idempotency.** The replay cache on ``POST`` routes is driven by the
process-wide :mod:`app.api.middleware.idempotency` middleware — no
per-route plumbing needed. A replayed POST returns the original
response verbatim with ``Idempotency-Replay: true``; a mismatched body
hash under the same key returns 409 ``idempotency_conflict``.

**Approvals.** The §12 ``/approvals`` surface (HITL agent-mediated) is
not in this slice — see :mod:`app.api.v1.admin` and the LLM context
follow-up. The "return 409 ``approval_required``" branch on a capped
action is not wired at the domain layer today (no
``requires_approval=True`` action in the catalog); when that lands it
fits in the existing error-mapping helper below.

**Scope boundaries for cd-sn26** (see ``bd show cd-sn26``):

* NL intake (``/tasks/from_nl*``) belongs to the LLM context.
* Schedule rulesets (``/schedule_rulesets``) and the scheduler feed
  (``/scheduler/*``) are separate features and not in this slice.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task template",
§"Schedule", §"State machine", §"Task notes are the agent inbox";
``docs/specs/12-rest-api.md`` §"Tasks / templates / schedules".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence
from app.adapters.storage.ports import MimeSniffer, Storage
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_mime_sniffer,
    get_storage,
)
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    encode_cursor,
    paginate,
)
from app.domain.tasks.assignment import (
    TaskAlreadyAssigned,
    assign_task,
)
from app.domain.tasks.assignment import (
    TaskNotFound as AssignTaskNotFound,
)
from app.domain.tasks.comments import (
    CommentAttachmentInvalid,
    CommentCreate,
    CommentEditWindowExpired,
    CommentKindForbidden,
    CommentMentionAmbiguous,
    CommentMentionInvalid,
    CommentNotEditable,
    CommentNotFound,
    CommentView,
    delete_comment,
    edit_comment,
    list_comments,
    post_comment,
)
from app.domain.tasks.completion import (
    EvidenceContentTypeNotAllowed,
    EvidenceGpsPayloadInvalid,
    EvidenceRequired,
    EvidenceTooLarge,
    EvidenceView,
    FileEvidenceKind,
    InvalidStateTransition,
    PhotoForbidden,
    RequiredChecklistIncomplete,
    SkipNotPermitted,
    TaskState,
    add_file_evidence,
    add_note_evidence,
    list_evidence,
)
from app.domain.tasks.completion import (
    PermissionDenied as CompletionPermissionDenied,
)
from app.domain.tasks.completion import (
    TaskNotFound as CompletionTaskNotFound,
)
from app.domain.tasks.completion import (
    cancel as cancel_task,
)
from app.domain.tasks.completion import (
    complete as complete_task,
)
from app.domain.tasks.completion import (
    skip as skip_task,
)
from app.domain.tasks.completion import (
    start as start_task,
)
from app.domain.tasks.oneoff import (
    InvalidLocalDatetime,
    PersonalAssignmentError,
    TaskCreate,
    TaskFieldInvalid,
    TaskPatch,
    TaskView,
    create_oneoff,
    read_task,
    update_task,
)
from app.domain.tasks.oneoff import (
    TaskNotFound as OneOffTaskNotFound,
)
from app.domain.tasks.schedules import (
    InvalidBackupWorkRole,
    InvalidRRule,
    ScheduleCreate,
    ScheduleNotFound,
    ScheduleUpdate,
    ScheduleView,
    list_schedules,
    preview_occurrences,
)
from app.domain.tasks.schedules import (
    create as create_schedule,
)
from app.domain.tasks.schedules import (
    delete as delete_schedule,
)
from app.domain.tasks.schedules import (
    pause as pause_schedule,
)
from app.domain.tasks.schedules import (
    read as read_schedule,
)
from app.domain.tasks.schedules import (
    resume as resume_schedule,
)
from app.domain.tasks.schedules import (
    update as update_schedule,
)
from app.domain.tasks.templates import (
    ScopeInconsistent,
    TaskTemplateCreate,
    TaskTemplateNotFound,
    TaskTemplateUpdate,
    TaskTemplateView,
    TemplateInUseError,
    list_templates,
)
from app.domain.tasks.templates import (
    create as create_template,
)
from app.domain.tasks.templates import (
    delete as delete_template,
)
from app.domain.tasks.templates import (
    read as read_template,
)
from app.domain.tasks.templates import (
    read_many as read_many_templates,
)
from app.domain.tasks.templates import (
    update as update_template,
)
from app.tenancy import WorkspaceContext

__all__ = ["router"]


router = APIRouter(tags=["tasks"])


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]
_MimeSniffer = Annotated[MimeSniffer, Depends(get_mime_sniffer)]


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------
#
# Each resource gets a named Pydantic model so FastAPI's OpenAPI
# generator emits a stable component schema — the SPA pattern-matches
# on the name ("TaskTemplatePayload", not "Read_0") and the generated
# TS types stay readable.


class InventoryEffectPayload(BaseModel):
    """One entry of a task template's :attr:`TaskTemplatePayload.inventory_effects`.

    Mirrors §08 "Inventory effects on task completion" — a list of
    ``{item_ref, kind, qty}`` declaring what the task **uses** and what
    it **produces**. The wire shape is the canonical projection per the
    spec; the v1 storage column (``inventory_consumption_json``, a flat
    SKU → positive int map) is a consume-only subset and is preserved on
    the wire alongside this richer projection while the storage migration
    lands. See :func:`TaskTemplatePayload.from_view` for the derivation.
    """

    item_ref: str
    kind: Literal["consume", "produce"]
    qty: int


class TaskTemplatePayload(BaseModel):
    """HTTP projection of :class:`TaskTemplateView`.

    The checklist items ride through as plain dicts (rather than the
    domain :class:`ChecklistTemplateItemPayload`) so the OpenAPI schema
    matches the shape the caller POSTed — a round-trip-identical wire
    format means the SPA can post a template and echo the response
    back on PATCH without a reshape step.

    ``inventory_effects`` is the spec-canonical projection of the
    template's inventory rules — a list of ``{item_ref, kind, qty}``
    entries (§08). Today the v1 storage column is a flat
    ``inventory_consumption_json`` map (consume-only, integer qty); the
    derived array re-projects each entry as ``kind="consume"`` so the
    SPA can render the spec shape directly. ``inventory_consumption_json``
    is kept on the wire as the authoring shape until the storage widens
    to ``inventory_effects_json`` per spec §06.

    Round-trip note: the request bodies (:class:`TaskTemplateCreate` /
    :class:`TaskTemplateUpdate`) accept ``inventory_consumption_json``
    only — ``inventory_effects`` is read-only on the wire. A SPA that
    POSTs back the response shape must drop ``inventory_effects`` (and
    the audit fields ``id``, ``workspace_id``, ``created_at``,
    ``deleted_at``) before sending; ``model_config = extra="forbid"``
    on the body rejects the fuller projection. Once storage widens to
    ``inventory_effects_json`` the request body will accept the array
    directly and the asymmetry resolves.
    """

    id: str
    workspace_id: str
    name: str
    description_md: str
    role_id: str | None
    duration_minutes: int
    property_scope: str
    listed_property_ids: list[str]
    area_scope: str
    listed_area_ids: list[str]
    checklist_template_json: list[dict[str, Any]]
    photo_evidence: str
    linked_instruction_ids: list[str]
    priority: str
    inventory_consumption_json: dict[str, int]
    inventory_effects: list[InventoryEffectPayload]
    llm_hints_md: str | None
    created_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: TaskTemplateView) -> TaskTemplatePayload:
        """Copy a :class:`TaskTemplateView` into its HTTP payload."""
        consumption = dict(view.inventory_consumption_json)
        effects = [
            InventoryEffectPayload(item_ref=sku, kind="consume", qty=qty)
            for sku, qty in consumption.items()
        ]
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            name=view.name,
            description_md=view.description_md,
            role_id=view.role_id,
            duration_minutes=view.duration_minutes,
            property_scope=view.property_scope,
            listed_property_ids=list(view.listed_property_ids),
            area_scope=view.area_scope,
            listed_area_ids=list(view.listed_area_ids),
            checklist_template_json=[
                item.model_dump(mode="json") for item in view.checklist_template_json
            ],
            photo_evidence=view.photo_evidence,
            linked_instruction_ids=list(view.linked_instruction_ids),
            priority=view.priority,
            inventory_consumption_json=consumption,
            inventory_effects=effects,
            llm_hints_md=view.llm_hints_md,
            created_at=view.created_at,
            deleted_at=view.deleted_at,
        )


class SchedulePayload(BaseModel):
    """HTTP projection of :class:`ScheduleView`.

    Two fields are derived on the wire to spare the SPA a fan-out join:

    * ``default_assignee_id`` mirrors the domain's
      :attr:`ScheduleView.default_assignee` under a clearer wire name
      (it's a user id; the SPA's ``Schedule`` TS type matches).
    * ``rrule_human`` is a short English summary of the recurrence —
      "Every Monday at 10:00" — composed from the schedule's RRULE +
      ``dtstart_local`` so the manager Schedules page can render the
      cadence column without re-implementing the parser in TypeScript.
    """

    id: str
    workspace_id: str
    name: str
    template_id: str
    property_id: str | None
    area_id: str | None
    default_assignee_id: str | None
    backup_assignee_user_ids: list[str]
    rrule: str
    rrule_human: str
    dtstart_local: str
    duration_minutes: int | None
    rdate_local: str
    exdate_local: str
    active_from: str | None
    active_until: str | None
    paused_at: datetime | None
    created_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: ScheduleView) -> SchedulePayload:
        """Copy a :class:`ScheduleView` into its HTTP payload."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            name=view.name,
            template_id=view.template_id,
            property_id=view.property_id,
            area_id=view.area_id,
            default_assignee_id=view.default_assignee,
            backup_assignee_user_ids=list(view.backup_assignee_user_ids),
            rrule=view.rrule,
            rrule_human=_humanize_rrule(view.rrule, view.dtstart_local),
            dtstart_local=view.dtstart_local,
            duration_minutes=view.duration_minutes,
            rdate_local=view.rdate_local,
            exdate_local=view.exdate_local,
            active_from=(
                view.active_from.isoformat() if view.active_from is not None else None
            ),
            active_until=(
                view.active_until.isoformat() if view.active_until is not None else None
            ),
            paused_at=view.paused_at,
            created_at=view.created_at,
            deleted_at=view.deleted_at,
        )


class TaskPayload(BaseModel):
    """HTTP projection of :class:`TaskView` with two derived fields.

    * ``overdue`` — boolean: the task is past its scheduled UTC
      anchor and not yet in a terminal state. Mirrors §06's soft-
      overdue rule. The cd-hurw column ``overdue_since`` (set by the
      sweeper worker) wins when present; for rows the sweeper has
      not yet visited (between the slip and the next 5-minute tick)
      we fall back to the time-derived projection so the manager
      surface does not show a stale "on time" chip.
    * ``time_window_local`` — ``"HH:MM-HH:MM"`` in the property
      timezone, computed from ``scheduled_for_utc`` + the task's
      ``duration_minutes`` (fallback to 30 minutes when the column
      is ``NULL``, matching the :func:`create_oneoff` default). Only
      populated when the task carries a resolvable ``property_id``;
      workspace-scoped (personal) tasks render as ``None``.
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
    priority: str
    state: str
    scheduled_for_local: str
    scheduled_for_utc: datetime
    duration_minutes: int | None
    photo_evidence: str
    linked_instruction_ids: list[str]
    inventory_consumption_json: dict[str, int]
    expected_role_id: str | None
    assigned_user_id: str | None
    created_by: str
    is_personal: bool
    created_at: datetime
    overdue: bool
    time_window_local: str | None

    @classmethod
    def from_view(
        cls,
        view: TaskView,
        *,
        property_timezone: str | None = None,
        now_utc: datetime | None = None,
    ) -> TaskPayload:
        """Copy a :class:`TaskView` into its HTTP payload.

        ``property_timezone`` resolves ``time_window_local`` — callers
        that already know the zone pass it in (saving a redundant
        property lookup); an omitted zone leaves the window unrendered.
        ``now_utc`` drives the ``overdue`` bool; defaults to
        :func:`datetime.now` in UTC so unit tests can pin a fixed clock
        without mocking module state.
        """
        moment = now_utc if now_utc is not None else datetime.now(tz=ZoneInfo("UTC"))
        overdue = _compute_overdue(view, moment)
        window = _compute_time_window_local(view, property_timezone)
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            template_id=view.template_id,
            schedule_id=view.schedule_id,
            property_id=view.property_id,
            area_id=view.area_id,
            unit_id=view.unit_id,
            title=view.title,
            description_md=view.description_md,
            priority=view.priority,
            state=view.state,
            scheduled_for_local=view.scheduled_for_local,
            scheduled_for_utc=view.scheduled_for_utc,
            duration_minutes=view.duration_minutes,
            photo_evidence=view.photo_evidence,
            linked_instruction_ids=list(view.linked_instruction_ids),
            inventory_consumption_json=dict(view.inventory_consumption_json),
            expected_role_id=view.expected_role_id,
            assigned_user_id=view.assigned_user_id,
            created_by=view.created_by,
            is_personal=view.is_personal,
            created_at=view.created_at,
            overdue=overdue,
            time_window_local=window,
        )


class TaskStatePayload(BaseModel):
    """HTTP projection of :class:`TaskState`.

    Returned by ``start`` / ``complete`` / ``skip`` / ``cancel``. The
    router echoes every field the SPA renders to draw the toast + chip
    after a state transition.
    """

    task_id: str
    state: str
    completed_at: datetime | None
    completed_by_user_id: str | None
    reason: str | None

    @classmethod
    def from_view(cls, view: TaskState) -> TaskStatePayload:
        """Copy a :class:`TaskState` into its HTTP payload."""
        return cls(
            task_id=view.task_id,
            state=view.state,
            completed_at=view.completed_at,
            completed_by_user_id=view.completed_by_user_id,
            reason=view.reason,
        )


class AssignmentPayload(BaseModel):
    """HTTP projection of :class:`AssignmentResult`.

    Returned by ``POST /tasks/{id}/assign``. Keeps the shape distinct
    from :class:`TaskStatePayload` so callers don't confuse "state
    transition" with "assignee changed" — two different events on
    the bus (``task.assigned`` vs ``task.state_changed``).

    ``state`` echoes the task's current :attr:`Occurrence.state` after
    the assignment so the SPA can refresh the chip without a second
    round-trip; assignment never changes the state machine, so this
    mirrors the pre-call value.
    """

    task_id: str
    assigned_user_id: str | None
    assignment_source: str
    candidate_count: int
    backup_index: int | None
    state: str


class CommentPayload(BaseModel):
    """HTTP projection of :class:`CommentView`."""

    id: str
    occurrence_id: str
    kind: str
    author_user_id: str | None
    body_md: str
    mentioned_user_ids: list[str]
    attachments: list[dict[str, Any]]
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    llm_call_id: str | None

    @classmethod
    def from_view(cls, view: CommentView) -> CommentPayload:
        """Copy a :class:`CommentView` into its HTTP payload."""
        return cls(
            id=view.id,
            occurrence_id=view.occurrence_id,
            kind=view.kind,
            author_user_id=view.author_user_id,
            body_md=view.body_md,
            mentioned_user_ids=list(view.mentioned_user_ids),
            attachments=[dict(item) for item in view.attachments],
            created_at=view.created_at,
            edited_at=view.edited_at,
            deleted_at=view.deleted_at,
            llm_call_id=view.llm_call_id,
        )


class EvidencePayload(BaseModel):
    """HTTP projection of :class:`EvidenceView`."""

    id: str
    workspace_id: str
    occurrence_id: str
    kind: str
    blob_hash: str | None
    note_md: str | None
    created_at: datetime
    created_by_user_id: str | None

    @classmethod
    def from_view(cls, view: EvidenceView) -> EvidencePayload:
        """Copy an :class:`EvidenceView` into its HTTP payload."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            occurrence_id=view.occurrence_id,
            kind=view.kind,
            blob_hash=view.blob_hash,
            note_md=view.note_md,
            created_at=view.created_at,
            created_by_user_id=view.created_by_user_id,
        )


class TaskTemplateListResponse(BaseModel):
    """Collection envelope for ``GET /task_templates``."""

    data: list[TaskTemplatePayload]
    next_cursor: str | None = None
    has_more: bool = False


class ScheduleListResponse(BaseModel):
    """Collection envelope for ``GET /schedules``.

    Carries the standard cursor-paginated ``{data, next_cursor, has_more}``
    envelope plus a ``templates_by_id`` sidecar — a one-call shape the
    SPA's manager Schedules page joins against without a second
    ``GET /task_templates`` round-trip. Schedules and their parent
    templates are tightly coupled (a Schedule rotates a template), so
    bundling them is semantically appropriate; the sidecar only carries
    templates referenced on *this page* (pagination-respecting), so the
    payload size scales with the page, not the workspace. See
    ``docs/specs/12-rest-api.md`` §"Tasks / templates / schedules".
    """

    data: list[SchedulePayload]
    next_cursor: str | None = None
    has_more: bool = False
    templates_by_id: dict[str, TaskTemplatePayload] = Field(default_factory=dict)


class TaskListResponse(BaseModel):
    """Collection envelope for ``GET /tasks``."""

    data: list[TaskPayload]
    next_cursor: str | None = None
    has_more: bool = False


class CommentListResponse(BaseModel):
    """Collection envelope for ``GET /tasks/{id}/comments``.

    Cursor is a base64-url-encoded ``(created_at_iso, id)`` tuple so
    two comments sharing a clock tick still paginate deterministically
    per the service's tuple-cursor contract.
    """

    data: list[CommentPayload]
    next_cursor: str | None = None
    has_more: bool = False


class EvidenceListResponse(BaseModel):
    """Collection envelope for ``GET /tasks/{id}/evidence``."""

    data: list[EvidencePayload]
    next_cursor: str | None = None
    has_more: bool = False


class OccurrencePreviewItem(BaseModel):
    """One occurrence in :class:`SchedulePreviewResponse.occurrences`."""

    starts_local: str


class SchedulePreviewResponse(BaseModel):
    """Response body for ``GET /schedules/{id}/preview``."""

    occurrences: list[OccurrencePreviewItem]


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


class AssignRequest(BaseModel):
    """Body for ``POST /tasks/{id}/assign``."""

    model_config = ConfigDict(extra="forbid")

    assignee_user_id: str = Field(..., min_length=1, max_length=64)


class ReasonRequest(BaseModel):
    """Shared body for ``/skip`` and ``/cancel``."""

    model_config = ConfigDict(extra="forbid")

    reason_md: str = Field(..., min_length=1, max_length=20_000)


class CompleteRequest(BaseModel):
    """Body for ``POST /tasks/{id}/complete``."""

    model_config = ConfigDict(extra="forbid")

    note_md: str | None = Field(default=None, max_length=20_000)
    photo_evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class CommentEditRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}/comments/{comment_id}``."""

    model_config = ConfigDict(extra="forbid")

    body_md: str = Field(..., min_length=1, max_length=20_000)


# ---------------------------------------------------------------------------
# Derived-field helpers
# ---------------------------------------------------------------------------


_TERMINAL_STATES: frozenset[str] = frozenset({"done", "skipped", "cancelled"})


def _compute_overdue(view: TaskView, now_utc: datetime) -> bool:
    """``True`` when the task is past its UTC anchor + not terminal.

    The §06 sweeper (:mod:`app.worker.tasks.overdue`) is the canonical
    writer of the soft state: it flips ``state='overdue'`` and stamps
    ``overdue_since`` once ``ends_at + grace`` is past. The column
    therefore takes priority — when the sweeper has visited the row
    we trust its verdict regardless of the comparison below. For
    rows the sweeper hasn't reached yet (between the slip and the
    next 5-minute tick), fall back to the time-derived projection
    so the manager surface doesn't show a stale "on time" chip until
    the sweeper catches up.
    """
    if view.state in _TERMINAL_STATES:
        return False
    # Column wins when present — the sweeper has already decided this
    # row is overdue. The state itself is also ``'overdue'`` in that
    # case (the sweeper writes both fields atomically) but the column
    # check is the explicit signal; checking it first lets a future
    # divergence (e.g. a manual ``revert_overdue`` that cleared the
    # column without flipping ``state`` for some reason) lean on the
    # column rather than a stale state name.
    if view.overdue_since is not None or view.state == "overdue":
        return True
    anchor = view.scheduled_for_utc
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=ZoneInfo("UTC"))
    return anchor < now_utc


def _compute_time_window_local(
    view: TaskView, property_timezone: str | None
) -> str | None:
    """Render ``HH:MM-HH:MM`` in the property's timezone.

    Returns ``None`` for workspace-scoped (personal) tasks without a
    property, or when the zone is unknown / junk. The window width
    falls back to 30 minutes when ``duration_minutes`` is ``NULL`` —
    matching the :func:`create_oneoff` default so the UI never shows
    a zero-minute window.
    """
    if property_timezone is None:
        return None
    try:
        zone = ZoneInfo(property_timezone)
    except ZoneInfoNotFoundError, ValueError:
        return None
    anchor = view.scheduled_for_utc
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=ZoneInfo("UTC"))
    local_start = anchor.astimezone(zone)
    duration = view.duration_minutes if view.duration_minutes is not None else 30
    # ``datetime + timedelta`` keeps the zone; the minutes come out in
    # the property frame.
    from datetime import timedelta

    local_end = local_start + timedelta(minutes=duration)
    return f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"


def _property_timezone(session: Session, property_id: str | None) -> str | None:
    """Return the IANA timezone for ``property_id`` or ``None``.

    ``None`` on unknown id keeps the caller from rendering a stale
    window; a junk zone string is surfaced up so the caller can
    decide (we let :func:`_compute_time_window_local` swallow it,
    so the window collapses to ``None``). One query per task list
    would be noisy; callers fetching a page pre-resolve zones via
    :func:`_resolve_zones_for_views` below.
    """
    if property_id is None:
        return None
    zone = session.scalar(select(Property.timezone).where(Property.id == property_id))
    return zone


def _resolve_zones_for_views(session: Session, views: list[TaskView]) -> dict[str, str]:
    """Fetch the ``property.timezone`` for every property in ``views``.

    One SELECT per page, keyed by ``property_id``, so the
    ``TaskPayload.from_view`` factory can pick the zone out of a dict
    instead of firing a query per task. Rows without a property (personal
    tasks) are filtered at the call site.
    """
    ids = {v.property_id for v in views if v.property_id is not None}
    if not ids:
        return {}
    rows = session.execute(
        select(Property.id, Property.timezone).where(Property.id.in_(list(ids)))
    ).all()
    return {row[0]: row[1] for row in rows}


# Weekday names indexed by ``dateutil.rrule._byweekday`` (Mon=0..Sun=6).
# Short forms — three letters — match the manager-Schedules mock copy
# ("Weekly on Mon, Thu at 10:30"). The English week starts on Monday to
# line up with ISO 8601 + the dateutil convention.
_WEEKDAY_SHORT: tuple[str, ...] = (
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
)
_WEEKDAY_LONG: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAYS_MON_FRI: frozenset[int] = frozenset({0, 1, 2, 3, 4})
_WEEKDAYS_SAT_SUN: frozenset[int] = frozenset({5, 6})

# Stable Monday used as a sentinel ``dtstart`` when the schedule row
# has no parseable ``dtstart_local``. Picking a fixed date (rather
# than ``datetime.now()``, which dateutil falls back to) keeps the
# rendered label deterministic — a crucial property for cache keys
# and for screenshot tests, even if the real-world write path always
# supplies a dtstart.
_SENTINEL_ANCHOR = datetime(1970, 1, 5)


def _rrule_has_clause(rrule_text: str, clause: str) -> bool:
    """Return ``True`` iff ``rrule_text`` carries an explicit ``CLAUSE=``.

    Used to distinguish a value the source line actually set from one
    dateutil derived from ``dtstart``. Matching is on the raw RRULE
    text rather than the parsed object, since the parsed object
    doesn't expose provenance.
    """
    needle = f"{clause}="
    # Case-insensitive: RFC-5545 keywords are case-insensitive, but
    # crew.day always emits uppercase. Normalising once is cheap and
    # spares us a regex.
    return needle in rrule_text.upper()


def _humanize_rrule(rrule_text: str, dtstart_local: str) -> str:
    """Return a short English summary of an RRULE + DTSTART pair.

    Examples (every shape exercised in the unit tests):

    * ``RRULE:FREQ=DAILY`` + ``2026-04-20T09:00`` → ``"Every day at 09:00"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=SA`` + ``…T09:00`` →
      ``"Every Saturday at 09:00"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=MO,TH`` + ``…T10:30`` →
      ``"Weekly on Mon, Thu at 10:30"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR`` + ``…T07:00`` →
      ``"Weekdays at 07:00"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=SA,SU`` + ``…T11:00`` →
      ``"Weekends at 11:00"``
    * ``RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO`` →
      ``"Every 2 weeks on Mon at HH:MM"``
    * ``RRULE:FREQ=MONTHLY`` → ``"Monthly at HH:MM"``
    * ``RRULE:FREQ=YEARLY`` → ``"Yearly at HH:MM"``

    Anything the parser can't reason about — a tampered body, an
    HOURLY / MINUTELY frequency, an unrecognised attribute combo —
    collapses to ``"Custom recurrence"`` rather than raising. The
    schedule's RRULE has already been validated by the schedules
    domain at write time, so a parse failure here would be a stored-
    row regression; surfacing a friendly fallback keeps the listing
    endpoint responsive while the underlying issue is investigated.

    Determinism note: ``dateutil.rrule`` defaults a missing
    ``dtstart`` to ``datetime.now()`` and back-fills ``_byweekday`` /
    ``_bymonthday`` from it. To keep the label stable for stored
    rows that somehow lost their ``dtstart_local`` (a write-time
    bug, but we must not render a label that drifts day-to-day),
    we only consult those parsed values when the BYDAY / BYMONTHDAY
    clause was explicit in the source text.
    """
    anchor = _parse_local_anchor(dtstart_local)
    time_label = anchor.strftime("%H:%M") if anchor is not None else None
    # Sentinel anchor when dtstart_local is missing/unparseable: a
    # fixed Monday so dateutil's parse succeeds without leaking
    # ``datetime.now()`` into the byweekday / bymonthday tuples we
    # surface below.
    parse_anchor = anchor if anchor is not None else _SENTINEL_ANCHOR
    try:
        rule = rrulestr(rrule_text, dtstart=parse_anchor)
    except ValueError, TypeError:
        return "Custom recurrence"

    has_byday = _rrule_has_clause(rrule_text, "BYDAY")
    has_bymonthday = _rrule_has_clause(rrule_text, "BYMONTHDAY")
    use_dtstart_derived = anchor is not None

    freq: int | None = getattr(rule, "_freq", None)
    interval: int = getattr(rule, "_interval", 1) or 1
    byweekday_raw: tuple[int, ...] | None = getattr(rule, "_byweekday", None)
    bymonthday_raw: tuple[int, ...] | None = getattr(rule, "_bymonthday", None)
    byweekday = (
        tuple(byweekday_raw)
        if byweekday_raw and (has_byday or use_dtstart_derived)
        else ()
    )
    bymonthday = (
        tuple(bymonthday_raw)
        if bymonthday_raw and (has_bymonthday or use_dtstart_derived)
        else ()
    )
    suffix = f" at {time_label}" if time_label is not None else ""

    # ``dateutil.rrule`` exposes the FREQ ints under module constants
    # (``DAILY = 3``, ``WEEKLY = 2``, ``MONTHLY = 1``, ``YEARLY = 0``,
    # ``HOURLY = 4``, ``MINUTELY = 5``, ``SECONDLY = 6``). Comparing
    # against the ints keeps this helper free of a runtime import-cycle
    # with the constants module — and matches how dateutil documents
    # the attribute (``_freq`` is a public-by-convention int).
    if freq == 3:  # DAILY
        if interval == 1:
            return f"Every day{suffix}"
        return f"Every {interval} days{suffix}"
    if freq == 2:  # WEEKLY
        return _humanize_weekly(byweekday, interval, suffix)
    if freq == 1:  # MONTHLY
        return _humanize_monthly(bymonthday, interval, suffix)
    if freq == 0:  # YEARLY
        if interval == 1:
            return f"Yearly{suffix}"
        return f"Every {interval} years{suffix}"

    return "Custom recurrence"


def _parse_local_anchor(dtstart_local: str) -> datetime | None:
    """Parse ``dtstart_local`` into a naive datetime, ``None`` on failure.

    Mirrors :func:`app.domain.tasks.schedules._parse_local_datetime`
    but swallows parse errors — the rrule humanizer is best-effort and
    must not raise out of the response projection. A tz-aware suffix
    is dropped (the column contract is naive); a malformed body
    returns ``None`` and the caller renders the recurrence without a
    time component.
    """
    text = dtstart_local.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _humanize_weekly(byweekday: tuple[int, ...], interval: int, suffix: str) -> str:
    """Render the WEEKLY branch of :func:`_humanize_rrule`.

    Single weekday → ``"Every Monday at HH:MM"``;
    ``MO,TU,WE,TH,FR`` → ``"Weekdays at HH:MM"``;
    ``SA,SU`` → ``"Weekends at HH:MM"``;
    other multi-day sets → ``"Weekly on Mon, Thu at HH:MM"``.
    Interval > 1 forces the explicit ``"Every N weeks on …"`` form so
    the cadence is unambiguous.
    """
    if not byweekday:
        # Defensive: dateutil populates ``_byweekday`` from dtstart for
        # a bare ``FREQ=WEEKLY``, so this branch is unreachable in
        # practice. Surface a sensible fallback rather than the empty
        # string a join would produce.
        return f"Weekly{suffix}"

    days_set = frozenset(byweekday)
    sorted_days = sorted(days_set)

    if interval > 1:
        joined = ", ".join(_WEEKDAY_SHORT[d] for d in sorted_days)
        return f"Every {interval} weeks on {joined}{suffix}"

    if len(sorted_days) == 1:
        return f"Every {_WEEKDAY_LONG[sorted_days[0]]}{suffix}"
    if days_set == _WEEKDAYS_MON_FRI:
        return f"Weekdays{suffix}"
    if days_set == _WEEKDAYS_SAT_SUN:
        return f"Weekends{suffix}"
    joined = ", ".join(_WEEKDAY_SHORT[d] for d in sorted_days)
    return f"Weekly on {joined}{suffix}"


def _humanize_monthly(bymonthday: tuple[int, ...], interval: int, suffix: str) -> str:
    """Render the MONTHLY branch of :func:`_humanize_rrule`.

    A single BYMONTHDAY → ``"Monthly on the Nth at HH:MM"``; multiple
    BYMONTHDAYs collapse to ``"Monthly on days 1, 15 at HH:MM"`` so
    the label stays readable. Interval > 1 forces ``"Every N months
    …"``. Without BYMONTHDAY (anchored only on dtstart) we render the
    plain ``"Monthly at HH:MM"`` shape — the day is implicit in the
    next-occurrences preview.
    """
    every = "Every" if interval == 1 else f"Every {interval}"
    unit = "month" if interval == 1 else "months"
    days = sorted({d for d in bymonthday if d > 0})

    if not days:
        if interval == 1:
            return f"Monthly{suffix}"
        return f"{every} {unit}{suffix}"
    if len(days) == 1:
        ordinal = _ordinal(days[0])
        if interval == 1:
            return f"Monthly on the {ordinal}{suffix}"
        return f"{every} {unit} on the {ordinal}{suffix}"
    joined = ", ".join(str(d) for d in days)
    if interval == 1:
        return f"Monthly on days {joined}{suffix}"
    return f"{every} {unit} on days {joined}{suffix}"


def _ordinal(day: int) -> str:
    """Return ``"1st"`` / ``"2nd"`` / ``"3rd"`` / ``"Nth"`` for a 1..31 day.

    Matches the English-language convention the SPA renders elsewhere
    on the manager surface; the helper is private because it only
    services the monthly recurrence label.
    """
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    suffix_by_last = {1: "st", 2: "nd", 3: "rd"}
    return f"{day}{suffix_by_last.get(day % 10, 'th')}"


# ---------------------------------------------------------------------------
# Cursor helpers (comments — tuple cursor)
# ---------------------------------------------------------------------------


def _encode_comment_cursor(created_at: datetime, comment_id: str) -> str:
    """Encode the tuple ``(created_at, id)`` as an opaque cursor."""
    return encode_cursor(f"{created_at.isoformat()}|{comment_id}")


def _decode_comment_cursor(
    cursor: str | None,
) -> tuple[datetime | None, str | None]:
    """Decode an opaque comment cursor into ``(created_at, id)`` or the empty
    pair when ``cursor`` is ``None``."""
    if cursor is None or cursor == "":
        return None, None
    raw = decode_cursor(cursor)
    if raw is None:
        return None, None
    # "<iso>|<id>" — a missing pipe is tampered input; collapse to 422
    # via the same envelope as :func:`app.api.pagination.decode_cursor`.
    if "|" not in raw:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "comment cursor missing separator",
            },
        )
    iso, comment_id = raw.split("|", 1)
    try:
        created_at = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "comment cursor timestamp is not ISO-8601",
            },
        ) from exc
    return created_at, comment_id


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
#
# Domain exceptions → HTTP shape. Every route that can raise maps
# through one of the helpers below; mixing ``isinstance`` ladders into
# each handler would let a new error class slip through with the wrong
# status. Keeping one table per resource keeps the mapping auditable.


def _http(status_code: int, error: str, **extra: object) -> HTTPException:
    """Construct the ``{"error": "<code>", ...}`` detail envelope."""
    detail: dict[str, object] = {"error": error}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _template_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "task_template_not_found")


def _schedule_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "schedule_not_found")


def _task_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "task_not_found")


def _comment_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "comment_not_found")


def _http_for_template_mutation(exc: Exception) -> HTTPException:
    """Map a template-domain exception to its HTTP shape."""
    if isinstance(exc, TaskTemplateNotFound):
        return _template_not_found()
    if isinstance(exc, TemplateInUseError):
        return _http(
            status.HTTP_409_CONFLICT,
            "template_in_use",
            schedule_ids=list(exc.schedule_ids),
            stay_lifecycle_rule_ids=list(exc.stay_lifecycle_rule_ids),
        )
    if isinstance(exc, ScopeInconsistent):
        return _http(422, "scope_inconsistent", message=str(exc))
    return _http(500, "internal")


def _http_for_schedule_mutation(exc: Exception) -> HTTPException:
    """Map a schedule-domain exception to its HTTP shape."""
    if isinstance(exc, ScheduleNotFound):
        return _schedule_not_found()
    if isinstance(exc, InvalidRRule):
        return _http(422, "invalid_rrule", message=str(exc))
    if isinstance(exc, InvalidBackupWorkRole):
        return _http(
            422,
            "backup_invalid_work_role",
            invalid_user_ids=list(exc.invalid_user_ids),
            role_id=exc.role_id,
        )
    if isinstance(exc, ValueError):
        # Fallthrough ValueError covers the "template_id unknown"
        # case the service raises during create/update (see
        # :func:`app.domain.tasks.schedules._load_template`). Surface
        # as 422 with a dedicated code so the SPA can branch.
        return _http(422, "invalid_schedule_payload", message=str(exc))
    return _http(500, "internal")


def _http_for_task_mutation(exc: Exception) -> HTTPException:
    """Map a task-domain exception (state machine + evidence) to HTTP."""
    if isinstance(
        exc, OneOffTaskNotFound | CompletionTaskNotFound | AssignTaskNotFound
    ):
        return _task_not_found()
    if isinstance(exc, TaskTemplateNotFound):
        return _template_not_found()
    if isinstance(exc, PersonalAssignmentError):
        return _http(422, "personal_assignment_invalid", message=str(exc))
    if isinstance(exc, TaskFieldInvalid):
        return _http(
            422,
            "invalid_task_field",
            field=exc.field,
            value=exc.value,
            message=str(exc),
        )
    if isinstance(exc, InvalidStateTransition):
        return _http(
            status.HTTP_409_CONFLICT,
            "invalid_state_transition",
            current=exc.current,
            target=exc.target,
        )
    if isinstance(exc, RequiredChecklistIncomplete):
        return _http(
            422,
            "required_checklist_incomplete",
            unchecked_ids=list(exc.unchecked_ids),
        )
    if isinstance(exc, PhotoForbidden):
        return _http(422, "photo_forbidden", message=str(exc))
    if isinstance(exc, EvidenceRequired):
        return _http(422, "evidence_required", message=str(exc))
    if isinstance(exc, SkipNotPermitted):
        return _http(status.HTTP_403_FORBIDDEN, "skip_not_permitted")
    if isinstance(exc, CompletionPermissionDenied):
        return _http(status.HTTP_403_FORBIDDEN, "permission_denied")
    if isinstance(exc, TaskAlreadyAssigned):
        return _http(422, "task_already_assigned", message=str(exc))
    return _http(500, "internal")


def _http_for_comment_mutation(exc: Exception) -> HTTPException:
    """Map a comment-domain exception to its HTTP shape."""
    if isinstance(exc, CommentNotFound):
        return _comment_not_found()
    if isinstance(exc, CommentKindForbidden):
        return _http(status.HTTP_403_FORBIDDEN, "comment_kind_forbidden")
    if isinstance(exc, CommentEditWindowExpired):
        return _http(status.HTTP_409_CONFLICT, "comment_edit_window_expired")
    if isinstance(exc, CommentNotEditable):
        return _http(status.HTTP_409_CONFLICT, "comment_not_editable")
    if isinstance(exc, CommentMentionInvalid):
        return _http(
            422,
            "comment_mention_invalid",
            unknown_slugs=list(exc.unknown_slugs),
        )
    if isinstance(exc, CommentMentionAmbiguous):
        return _http(
            422,
            "comment_mention_ambiguous",
            ambiguous_slugs=list(exc.ambiguous_slugs),
        )
    if isinstance(exc, CommentAttachmentInvalid):
        return _http(
            422,
            "comment_attachment_invalid",
            unknown_ids=list(exc.unknown_ids),
        )
    return _http(500, "internal")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@router.get(
    "/task_templates",
    response_model=TaskTemplateListResponse,
    operation_id="list_task_templates",
    summary="List task templates in the caller's workspace",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "templates-list"}},
)
def list_task_templates_route(
    ctx: _Ctx,
    session: _Db,
    q: Annotated[str | None, Query(max_length=200)] = None,
    role_id: Annotated[str | None, Query(max_length=64)] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> TaskTemplateListResponse:
    """Return a cursor-paginated page of live templates."""
    after_id = decode_cursor(cursor)
    # The service returns every row ordered (created_at, id); we pull
    # the whole set and slice client-side. Workspaces with >500
    # templates are not a realistic v1 shape; cd-template-pagination
    # tracks the proper DB-side cursor when that changes.
    views = list(list_templates(session, ctx, q=q, role_id=role_id))
    if after_id is not None:
        views = [v for v in views if v.id > after_id]
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return TaskTemplateListResponse(
        data=[TaskTemplatePayload.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "/task_templates",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskTemplatePayload,
    operation_id="create_task_template",
    summary="Create a task template",
)
def create_task_template_route(
    body: TaskTemplateCreate,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """Insert a fresh template row."""
    try:
        view = create_template(session, ctx, body=body)
    except ScopeInconsistent as exc:
        raise _http_for_template_mutation(exc) from exc
    return TaskTemplatePayload.from_view(view)


@router.get(
    "/task_templates/{template_id}",
    response_model=TaskTemplatePayload,
    operation_id="get_task_template",
    summary="Read a task template",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "template-show"}},
)
def get_task_template_route(
    template_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """Return the template identified by ``template_id``."""
    try:
        view = read_template(session, ctx, template_id=template_id)
    except TaskTemplateNotFound as exc:
        raise _template_not_found() from exc
    return TaskTemplatePayload.from_view(view)


@router.patch(
    "/task_templates/{template_id}",
    response_model=TaskTemplatePayload,
    operation_id="update_task_template",
    summary="Replace the mutable body of a task template",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "template-update"}},
)
def patch_task_template_route(
    template_id: str,
    body: TaskTemplateUpdate,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """PATCH = full-body replace per the v1 template contract."""
    try:
        view = update_template(session, ctx, template_id=template_id, body=body)
    except (TaskTemplateNotFound, ScopeInconsistent) as exc:
        raise _http_for_template_mutation(exc) from exc
    return TaskTemplatePayload.from_view(view)


@router.delete(
    "/task_templates/{template_id}",
    response_model=TaskTemplatePayload,
    operation_id="delete_task_template",
    summary="Soft-delete a task template",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "template-delete"}},
)
def delete_task_template_route(
    template_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """Soft-delete; 409 ``template_in_use`` when consumers remain."""
    try:
        view = delete_template(session, ctx, template_id=template_id)
    except (TaskTemplateNotFound, TemplateInUseError) as exc:
        raise _http_for_template_mutation(exc) from exc
    return TaskTemplatePayload.from_view(view)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@router.get(
    "/schedules",
    response_model=ScheduleListResponse,
    operation_id="list_schedules",
    summary="List schedules in the caller's workspace",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedules-list"}},
)
def list_schedules_route(
    ctx: _Ctx,
    session: _Db,
    template_id: Annotated[str | None, Query(max_length=64)] = None,
    property_id: Annotated[str | None, Query(max_length=64)] = None,
    paused: Annotated[bool | None, Query()] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> ScheduleListResponse:
    """Return a cursor-paginated page of live schedules.

    Each page also carries a ``templates_by_id`` sidecar holding every
    ``task_template`` the page's schedules reference — bundled in one
    SELECT so the SPA's Schedules page can join template metadata
    (name, role, …) without a second round-trip. The sidecar is
    pagination-scoped (only this page's templates), so payload size
    scales with the page rather than the workspace.
    """
    after_id = decode_cursor(cursor)
    views = list(
        list_schedules(
            session,
            ctx,
            template_id=template_id,
            property_id=property_id,
            paused=paused,
        )
    )
    if after_id is not None:
        views = [v for v in views if v.id > after_id]
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    schedule_payloads = [SchedulePayload.from_view(v) for v in page.items]
    template_ids = [s.template_id for s in schedule_payloads]
    templates_by_id = {
        view.id: TaskTemplatePayload.from_view(view)
        for view in read_many_templates(session, ctx, template_ids=template_ids)
    }
    return ScheduleListResponse(
        data=schedule_payloads,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        templates_by_id=templates_by_id,
    )


@router.post(
    "/schedules",
    status_code=status.HTTP_201_CREATED,
    response_model=SchedulePayload,
    operation_id="create_schedule",
    summary="Create a schedule",
)
def create_schedule_route(
    body: ScheduleCreate,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Insert a fresh schedule row; validates RRULE + DTSTART."""
    try:
        view = create_schedule(session, ctx, body=body)
    except (InvalidRRule, InvalidBackupWorkRole, ValueError) as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return SchedulePayload.from_view(view)


@router.get(
    "/schedules/{schedule_id}",
    response_model=SchedulePayload,
    operation_id="get_schedule",
    summary="Read a schedule",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-show"}},
)
def get_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Return the schedule identified by ``schedule_id``."""
    try:
        view = read_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


@router.patch(
    "/schedules/{schedule_id}",
    response_model=SchedulePayload,
    operation_id="update_schedule",
    summary="Replace the mutable body of a schedule",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-update"}},
)
def patch_schedule_route(
    schedule_id: str,
    body: ScheduleUpdate,
    ctx: _Ctx,
    session: _Db,
    apply_to_existing: Annotated[bool, Query()] = False,
) -> SchedulePayload:
    """PATCH = full-body replace; ``apply_to_existing`` cascades."""
    try:
        view = update_schedule(
            session,
            ctx,
            schedule_id=schedule_id,
            body=body,
            apply_to_existing=apply_to_existing,
        )
    except (ScheduleNotFound, InvalidRRule, InvalidBackupWorkRole, ValueError) as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return SchedulePayload.from_view(view)


@router.delete(
    "/schedules/{schedule_id}",
    response_model=SchedulePayload,
    operation_id="delete_schedule",
    summary="Soft-delete a schedule and cancel scheduled occurrences",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-delete"}},
)
def delete_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Soft-delete the schedule."""
    try:
        view = delete_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


@router.get(
    "/schedules/{schedule_id}/preview",
    response_model=SchedulePreviewResponse,
    operation_id="preview_schedule",
    summary="Return the next N occurrences of a schedule",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-preview"}},
)
def preview_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
    n: Annotated[int, Query(ge=1, le=100)] = 5,
) -> SchedulePreviewResponse:
    """Return the next ``n`` local occurrences of the schedule's RRULE.

    The spec's ``?for=30d`` shape is a future enhancement — the
    current preview is ``n``-bounded so the UI's "next 5 occurrences"
    panel lines up with the domain helper today. A window-based
    ``?for=`` filter lands with the cd-schedule-preview-window
    follow-up once the scheduler UI needs it.
    """
    try:
        schedule = read_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    try:
        moments = preview_occurrences(
            schedule.rrule,
            schedule.dtstart_local,
            n=n,
            rdate_local=schedule.rdate_local,
            exdate_local=schedule.exdate_local,
        )
    except InvalidRRule as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return SchedulePreviewResponse(
        occurrences=[
            OccurrencePreviewItem(starts_local=m.isoformat(timespec="minutes"))
            for m in moments
        ]
    )


@router.post(
    "/schedules/{schedule_id}/pause",
    response_model=SchedulePayload,
    operation_id="pause_schedule",
    summary="Pause a schedule without cancelling materialised tasks",
)
def pause_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Set ``paused_at``; no cascade."""
    try:
        view = pause_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


@router.post(
    "/schedules/{schedule_id}/resume",
    response_model=SchedulePayload,
    operation_id="resume_schedule",
    summary="Resume a paused schedule",
)
def resume_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Clear ``paused_at``."""
    try:
        view = resume_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


# ---------------------------------------------------------------------------
# Occurrences ("tasks")
# ---------------------------------------------------------------------------


_OccurrenceState = Literal[
    "scheduled", "pending", "in_progress", "done", "skipped", "cancelled", "overdue"
]


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    operation_id="list_tasks",
    summary="List occurrences (tasks) with filters",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "list"}},
)
def list_tasks_route(
    ctx: _Ctx,
    session: _Db,
    state: Annotated[_OccurrenceState | None, Query()] = None,
    assignee_user_id: Annotated[str | None, Query(max_length=64)] = None,
    property_id: Annotated[str | None, Query(max_length=64)] = None,
    scheduled_for_utc_gte: Annotated[datetime | None, Query()] = None,
    scheduled_for_utc_lt: Annotated[datetime | None, Query()] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> TaskListResponse:
    """Cursor-paginated list with workspace-scoped filters.

    Personal tasks (``is_personal=True``) are visible to their creator
    and to workspace owners only — the §15 read layer's personal-task
    gate is applied inline so the §12 listing surface honours the same
    rule.
    """
    after_id = decode_cursor(cursor)
    now = datetime.now(tz=ZoneInfo("UTC"))
    stmt = select(Occurrence).where(Occurrence.workspace_id == ctx.workspace_id)
    if state is not None:
        if state == "overdue":
            # cd-hurw: ``overdue`` is now a real DB state, but the
            # sweeper only flips a row at most every 5 minutes — a
            # task that slipped 30 seconds ago is still
            # ``state='pending'`` until the next tick. Cover both:
            # rows the sweeper has already visited (``state='overdue'``
            # OR ``overdue_since IS NOT NULL``) and rows the sweeper
            # has not reached yet (``state IN (pending, in_progress)``
            # AND ``starts_at < now``). Mirror of
            # :func:`_compute_overdue`'s prefer-column-then-time logic.
            stmt = stmt.where(
                or_(
                    Occurrence.state == "overdue",
                    Occurrence.overdue_since.is_not(None),
                    (Occurrence.state.in_(("pending", "in_progress")))
                    & (Occurrence.starts_at < now),
                )
            )
        else:
            stmt = stmt.where(Occurrence.state == state)
    if assignee_user_id is not None:
        stmt = stmt.where(Occurrence.assignee_user_id == assignee_user_id)
    if property_id is not None:
        stmt = stmt.where(Occurrence.property_id == property_id)
    if scheduled_for_utc_gte is not None:
        stmt = stmt.where(Occurrence.starts_at >= scheduled_for_utc_gte)
    if scheduled_for_utc_lt is not None:
        stmt = stmt.where(Occurrence.starts_at < scheduled_for_utc_lt)
    if after_id is not None:
        stmt = stmt.where(Occurrence.id > after_id)
    stmt = stmt.order_by(Occurrence.id.asc()).limit(limit + 1)
    rows = list(session.scalars(stmt).all())
    # Personal-task visibility. Owners see everything; every other
    # caller sees only the tasks they created.
    if not ctx.actor_was_owner_member:
        rows = [
            r for r in rows if not r.is_personal or r.created_by_user_id == ctx.actor_id
        ]
    # Project and paginate. The domain read helper
    # :func:`app.domain.tasks.oneoff.read_task` builds the view shape
    # we want; re-using it keeps the projection path single-sourced.
    views = [read_task(session, ctx, task_id=row.id) for row in rows]
    zones = _resolve_zones_for_views(session, views)
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return TaskListResponse(
        data=[
            TaskPayload.from_view(
                v,
                property_timezone=zones.get(v.property_id)
                if v.property_id is not None
                else None,
                now_utc=now,
            )
            for v in page.items
        ],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "/tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskPayload,
    operation_id="create_task",
    summary="Create a one-off task",
)
def create_task_route(
    body: TaskCreate,
    ctx: _Ctx,
    session: _Db,
) -> TaskPayload:
    """Ad-hoc create — see :func:`app.domain.tasks.oneoff.create_oneoff`."""
    try:
        view = create_oneoff(session, ctx, payload=body)
    except (
        TaskTemplateNotFound,
        PersonalAssignmentError,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    zone = _property_timezone(session, view.property_id)
    return TaskPayload.from_view(view, property_timezone=zone)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskPayload,
    operation_id="get_task",
    summary="Read a single task",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "show"}},
)
def get_task_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskPayload:
    """Return the task identified by ``task_id``; 404 cross-tenant."""
    try:
        view = read_task(session, ctx, task_id=task_id)
    except OneOffTaskNotFound as exc:
        raise _task_not_found() from exc
    zone = _property_timezone(session, view.property_id)
    return TaskPayload.from_view(view, property_timezone=zone)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskPayload,
    operation_id="patch_task",
    summary="Partial update of a task (full §06 mutable set)",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "update"}},
)
def patch_task_route(
    task_id: str,
    body: TaskPatch,
    ctx: _Ctx,
    session: _Db,
) -> TaskPayload:
    """PATCH — see :class:`app.domain.tasks.oneoff.TaskPatch`.

    Carries the full §06 "Task row" mutable set (cd-43wv): title,
    description_md, scheduled_for_local, property_id, area_id,
    unit_id, expected_role_id, priority, duration_minutes,
    photo_evidence. Each field is independently validated; the
    relationship checks (area / unit must belong to the resolved
    property; property must belong to the workspace; role must be a
    live workspace row) surface as ``422 invalid_task_field``. A
    malformed ``scheduled_for_local`` lands as ``422 invalid_field``.

    Reassignment / availability re-resolution after a property or
    schedule change lives on the dedicated reschedule + reassign
    verbs (``/scheduler/tasks/{id}/reschedule``,
    ``/scheduler/tasks/{id}/reassign``). PATCH only writes through
    and emits :class:`~app.events.types.TaskUpdated`; the SPA's SSE
    reducer invalidates the affected caches.
    """
    try:
        view = update_task(session, ctx, task_id=task_id, body=body)
    except (OneOffTaskNotFound, TaskFieldInvalid) as exc:
        # ``_http_for_task_mutation`` is the single mapping table for
        # task-domain exceptions; routing through it keeps the
        # ``invalid_task_field`` envelope identical to every other
        # task verb (start / complete / skip / cancel) so the SPA's
        # error-toast renderer doesn't have to special-case PATCH.
        raise _http_for_task_mutation(exc) from exc
    except InvalidLocalDatetime as exc:
        # ``_parse_local_datetime`` raises ``InvalidLocalDatetime`` on a
        # malformed or tz-aware ``scheduled_for_local``. Distinct from
        # other ``ValueError``s the service may raise (e.g. clock
        # contract violations) so we don't accidentally squash an
        # internal bug under a 422.
        raise _http(422, "invalid_field", message=str(exc)) from exc
    zone = _property_timezone(session, view.property_id)
    return TaskPayload.from_view(view, property_timezone=zone)


@router.post(
    "/tasks/{task_id}/assign",
    response_model=AssignmentPayload,
    operation_id="assign_task",
    summary="Assign a task to a specific user",
)
def assign_task_route(
    task_id: str,
    body: AssignRequest,
    ctx: _Ctx,
    session: _Db,
) -> AssignmentPayload:
    """Write ``assigned_user_id=body.assignee_user_id`` through the algorithm.

    Delegates to :func:`app.domain.tasks.assignment.assign_task` with
    the override path (no auto-pool walk). The response echoes the
    :class:`AssignmentResult` shape — ``assigned_user_id``,
    ``assignment_source``, ``candidate_count``, ``backup_index``, and
    the task's current ``state`` so the SPA can refresh the chip
    without a follow-up GET.
    """
    try:
        result = assign_task(
            session, ctx, task_id, override_user_id=body.assignee_user_id
        )
    except AssignTaskNotFound as exc:
        raise _task_not_found() from exc
    current_state = session.scalar(
        select(Occurrence.state).where(
            Occurrence.id == result.task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    return AssignmentPayload(
        task_id=result.task_id,
        assigned_user_id=result.assigned_user_id,
        assignment_source=result.source,
        candidate_count=result.candidate_count,
        backup_index=result.backup_index,
        state=current_state or "",
    )


@router.post(
    "/tasks/{task_id}/start",
    response_model=TaskStatePayload,
    operation_id="start_task",
    summary="Drive a task from pending to in_progress",
)
def start_task_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.start`."""
    try:
        view = start_task(session, ctx, task_id)
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


@router.post(
    "/tasks/{task_id}/complete",
    response_model=TaskStatePayload,
    operation_id="complete_task",
    summary="Mark a task done — gated by evidence + checklist policy",
)
def complete_task_route(
    task_id: str,
    body: CompleteRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.complete`.

    ``Idempotency-Key`` replay is handled by the process-wide
    middleware; no per-route logic needed.
    """
    try:
        view = complete_task(
            session,
            ctx,
            task_id,
            note_md=body.note_md,
            photo_evidence_ids=body.photo_evidence_ids,
        )
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        PhotoForbidden,
        EvidenceRequired,
        RequiredChecklistIncomplete,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


@router.post(
    "/tasks/{task_id}/skip",
    response_model=TaskStatePayload,
    operation_id="skip_task",
    summary="Skip a task with a reason",
)
def skip_task_route(
    task_id: str,
    body: ReasonRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.skip`."""
    try:
        view = skip_task(session, ctx, task_id, reason=body.reason_md)
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        SkipNotPermitted,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskStatePayload,
    operation_id="cancel_task",
    summary="Cancel a task with a reason (manager / owner only)",
)
def cancel_task_route(
    task_id: str,
    body: ReasonRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.cancel`."""
    try:
        view = cancel_task(session, ctx, task_id, reason=body.reason_md)
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@router.post(
    "/tasks/{task_id}/comments",
    status_code=status.HTTP_201_CREATED,
    response_model=CommentPayload,
    operation_id="post_task_comment",
    summary="Append a comment to a task's agent thread",
)
def post_task_comment_route(
    task_id: str,
    body: CommentCreate,
    ctx: _Ctx,
    session: _Db,
) -> CommentPayload:
    """Delegate to :func:`app.domain.tasks.comments.post_comment`.

    ``kind`` is inferred from ``ctx.actor_kind``: a user / system
    actor posts ``kind='user'``; an agent token posts ``kind='agent'``.
    The ``system`` kind is internal-only and not reachable through the
    HTTP surface — state-change markers come from the completion /
    assignment services with ``internal_caller=True``.
    """
    kind: Literal["user", "agent"] = "agent" if ctx.actor_kind == "agent" else "user"
    try:
        view = post_comment(session, ctx, task_id, body, kind=kind)
    except CommentNotFound as exc:
        # ``post_comment`` raises :class:`CommentNotFound` when the
        # parent task is missing / cross-tenant / gated by the
        # personal-task rule — *not* when a comment id is unknown
        # (POST creates). Surface the actual missing entity so the
        # 404 envelope is truthful.
        raise _task_not_found() from exc
    except (
        CommentKindForbidden,
        CommentMentionInvalid,
        CommentMentionAmbiguous,
        CommentAttachmentInvalid,
    ) as exc:
        raise _http_for_comment_mutation(exc) from exc
    return CommentPayload.from_view(view)


@router.get(
    "/tasks/{task_id}/comments",
    response_model=CommentListResponse,
    operation_id="list_task_comments",
    summary="List comments on a task (oldest-first, cursor-paginated)",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "comments-list"}},
)
def list_task_comments_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> CommentListResponse:
    """Return a cursor-paginated page of comments.

    The cursor is a tuple ``(created_at, id)`` so two comments
    sharing a clock tick still paginate deterministically.
    """
    try:
        after_ts, after_id = _decode_comment_cursor(cursor)
        views = list(
            list_comments(
                session,
                ctx,
                task_id,
                after=after_ts,
                after_id=after_id,
                limit=limit + 1,
            )
        )
    except CommentNotFound as exc:
        raise _task_not_found() from exc
    has_more = len(views) > limit
    items = views[:limit]
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_comment_cursor(last.created_at, last.id)
    return CommentListResponse(
        data=[CommentPayload.from_view(v) for v in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.patch(
    "/tasks/{task_id}/comments/{comment_id}",
    response_model=CommentPayload,
    operation_id="patch_task_comment",
    summary="Edit a comment within the author grace window",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "comment-update"}},
)
def patch_task_comment_route(
    task_id: str,
    comment_id: str,
    body: CommentEditRequest,
    ctx: _Ctx,
    session: _Db,
) -> CommentPayload:
    """Delegate to :func:`app.domain.tasks.comments.edit_comment`.

    ``task_id`` in the URL is an addressing aid for the SPA / CLI —
    the service loads the comment by id and re-asserts the parent
    occurrence from the row, so a mismatched ``task_id`` does not
    allow cross-task rewrites. We still enforce the pairing defensively
    here so a caller that scraped the wrong id learns loudly.
    """
    try:
        view = edit_comment(session, ctx, comment_id, body.body_md)
    except (
        CommentNotFound,
        CommentKindForbidden,
        CommentEditWindowExpired,
        CommentNotEditable,
        CommentMentionInvalid,
        CommentMentionAmbiguous,
    ) as exc:
        raise _http_for_comment_mutation(exc) from exc
    if view.occurrence_id != task_id:
        # Cross-task request — collapse to 404 so we don't leak the
        # existence of the comment on a different task.
        raise _comment_not_found()
    return CommentPayload.from_view(view)


@router.delete(
    "/tasks/{task_id}/comments/{comment_id}",
    response_model=CommentPayload,
    operation_id="delete_task_comment",
    summary="Soft-delete a comment",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "comment-delete"}},
)
def delete_task_comment_route(
    task_id: str,
    comment_id: str,
    ctx: _Ctx,
    session: _Db,
) -> CommentPayload:
    """Delegate to :func:`app.domain.tasks.comments.delete_comment`."""
    try:
        view = delete_comment(session, ctx, comment_id)
    except (
        CommentNotFound,
        CommentKindForbidden,
        CommentNotEditable,
    ) as exc:
        raise _http_for_comment_mutation(exc) from exc
    if view.occurrence_id != task_id:
        raise _comment_not_found()
    return CommentPayload.from_view(view)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@router.get(
    "/tasks/{task_id}/evidence",
    response_model=EvidenceListResponse,
    operation_id="list_task_evidence",
    summary="List evidence rows on a task",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "evidence-list"}},
)
def list_task_evidence_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> EvidenceListResponse:
    """Return every evidence row anchored to ``task_id``.

    The response envelope carries ``next_cursor`` / ``has_more`` for
    forward compatibility with cd-evidence-pagination; today the
    helper returns the full set because the expected per-task
    evidence count (template checklist + a handful of ad-hoc photos)
    is well below a single page.
    """
    try:
        views = list_evidence(session, ctx, task_id=task_id)
    except CompletionTaskNotFound as exc:
        raise _task_not_found() from exc
    return EvidenceListResponse(
        data=[EvidencePayload.from_view(v) for v in views],
        next_cursor=None,
        has_more=False,
    )


_FILE_EVIDENCE_KINDS: frozenset[str] = frozenset({"photo", "voice", "gps"})

# Hard ceiling on the file part the multipart parser will ever buffer
# in memory before this route's domain seam runs the per-kind cap.
# Pinned at the largest per-kind cap (voice — 25 MiB per spec §15
# "Input validation") + 1 byte so a 25 MiB voice memo lands but a
# pathological 1 GiB upload short-circuits before we hash it. The
# domain seam re-enforces the per-kind cap so this is defence in depth,
# not the only gate.
_MAX_FILE_EVIDENCE_BYTES: int = 25 * 1024 * 1024 + 1


def _check_evidence_content_length(request: Request) -> None:
    """Raise 413 when the client advertises an oversized body.

    Mirrors :func:`app.api.v1.auth.me_avatar._check_content_length`.
    Exposed as a FastAPI dep (not an inline call) so it runs **before**
    Starlette's multipart body parser — otherwise FastAPI would buffer
    the entire upload to a :class:`SpooledTemporaryFile` to populate
    the :class:`UploadFile` parameter before the handler body could
    look at the header. Dependencies are resolved ahead of body
    params, so this dep is the first gate the router opens.

    Content-Length can be absent (chunked transfer) or lie; the
    streaming guard in :func:`_read_file_capped` is the authoritative
    check. This fast-path saves the buffering cost when the client
    admits to an oversized upload — the common well-behaved rejection
    shape.
    """
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        size = int(cl)
    except ValueError:
        # Malformed Content-Length — let Starlette's normal parsing
        # surface the underlying error rather than translating it
        # here. A non-numeric header isn't specifically a "too large"
        # condition.
        return
    if size > _MAX_FILE_EVIDENCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "error": "evidence_too_large",
                "message": (
                    f"upload exceeds the {_MAX_FILE_EVIDENCE_BYTES - 1}-byte "
                    "router-level cap"
                ),
            },
        )


_EvidenceContentLengthGuard = Annotated[None, Depends(_check_evidence_content_length)]


async def _read_file_capped(upload: UploadFile, *, kind: str) -> bytes:
    """Buffer the upload body, raising 413 past :data:`_MAX_FILE_EVIDENCE_BYTES`.

    Mirrors :func:`app.api.v1.auth.me_avatar._read_capped` — streams in
    64 KiB chunks so a client that lies about ``Content-Length`` can't
    exhaust memory. The per-kind cap re-checks inside the domain seam
    so a misconfigured router still can't admit a 30 MiB GPS payload.

    This is the second of the two router-level gates: the
    :func:`_check_evidence_content_length` dep rejects an oversized
    advertised body **before** the multipart parser runs; this
    function bounds an unadvertised / lying body during the read.
    """
    chunk_size = 64 * 1024
    total = 0
    pieces: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_FILE_EVIDENCE_BYTES:
            await upload.close()
            raise _http(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "evidence_too_large",
                kind=kind,
                message=(
                    f"upload exceeds the {_MAX_FILE_EVIDENCE_BYTES - 1}-byte "
                    "router-level cap"
                ),
            )
        pieces.append(chunk)
    await upload.close()
    return b"".join(pieces)


@router.post(
    "/tasks/{task_id}/evidence",
    status_code=status.HTTP_201_CREATED,
    response_model=EvidencePayload,
    operation_id="upload_task_evidence",
    summary="Attach evidence to a task",
)
async def upload_task_evidence_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
    storage: _Storage,
    mime_sniffer: _MimeSniffer,
    _: _EvidenceContentLengthGuard,
    kind: Annotated[str, Form(max_length=16)],
    note_md: Annotated[str | None, Form(max_length=20_000)] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> EvidencePayload:
    """Accept ``multipart/form-data``; wire every §06 evidence kind end-to-end.

    Routing by ``kind``:

    * ``note`` — :func:`~app.domain.tasks.completion.add_note_evidence`;
      the ``note_md`` form field is required and the upload body MUST
      be empty. Bridge until the ``completion_note_md`` task column
      lands.
    * ``photo`` / ``voice`` — :func:`~app.domain.tasks.completion.
      add_file_evidence`; the upload body is hashed (SHA-256), handed
      to the content-addressed :class:`Storage` port, and an
      :class:`Evidence` row points at the resulting blob. Per spec
      §15 "Input validation": the body is sniffed server-side via
      the injectable :class:`MimeSniffer` and the **sniffed** type
      is validated against the per-kind allow-list (the multipart
      header is informational only). Size cap per kind.
    * ``gps`` — :func:`~app.domain.tasks.completion.add_file_evidence`
      with the multipart-declared ``Content-Type`` (which the client
      MUST set to ``application/json`` per spec §06 "Evidence" — the
      §15 sniffer's JSON structural fallback is gated on a JSON-shaped
      hint, so a non-JSON declared type closes the gate and earns
      415). The upload body MUST be a small JSON document carrying
      ``lat`` / ``lon`` / optional ``accuracy_m``. Routes through
      Storage so every evidence row shares the same content-addressed
      pipeline.
    """
    if kind == "note":
        if file is not None:
            # A note carries no binary payload; reject the mix so a
            # confused client learns loudly.
            await file.close()
            raise _http(
                422,
                "evidence_note_with_file",
                message="kind='note' evidence must not carry a file upload",
            )
        if note_md is None or not note_md.strip():
            raise _http(
                422,
                "evidence_note_empty",
                message="kind='note' evidence requires a non-empty note_md",
            )
        try:
            view = add_note_evidence(session, ctx, task_id=task_id, note_md=note_md)
        except CompletionTaskNotFound as exc:
            raise _task_not_found() from exc
        except ValueError as exc:
            raise _http(422, "evidence_note_empty", message=str(exc)) from exc
        return EvidencePayload.from_view(view)

    if kind not in _FILE_EVIDENCE_KINDS:
        # Anything outside the §06 "Evidence" enum is caller error —
        # 422 ``evidence_invalid_kind``. Consume any uploaded stream
        # first so the multipart parser doesn't leak a tempfile.
        if file is not None:
            await file.close()
        raise _http(
            422,
            "evidence_invalid_kind",
            message=(
                f"kind={kind!r} is not a valid evidence kind; expected "
                "one of 'note', 'photo', 'voice', 'gps'"
            ),
        )

    # File-bearing kind. The upload body is required.
    if file is None:
        raise _http(
            422,
            "evidence_file_required",
            message=f"kind={kind!r} evidence requires a multipart file upload",
        )
    if note_md is not None:
        # A photo / voice / gps payload carries the body, not the
        # field. Any ``note_md`` (including whitespace-only) signals a
        # confused client; reject so the contract stays narrow and a
        # misuse never silently slips past as an empty string.
        await file.close()
        raise _http(
            422,
            "evidence_file_with_note",
            message=(
                f"kind={kind!r} evidence must not carry a 'note_md' form field; "
                "use kind='note' for notes"
            ),
        )
    declared_type = file.content_type
    if declared_type is None or declared_type == "":
        await file.close()
        raise _http(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "evidence_content_type_missing",
            kind=kind,
            message=(
                f"kind={kind!r} evidence requires a 'Content-Type' header on the "
                "uploaded file part"
            ),
        )

    payload = await _read_file_capped(file, kind=kind)
    # Narrow ``kind`` from the loose ``str`` form field to the typed
    # :data:`FileEvidenceKind` Literal the domain seam expects. The
    # earlier ``in _FILE_EVIDENCE_KINDS`` check guarantees membership;
    # the per-branch ``cast`` keeps mypy --strict honest without an
    # explicit ``cast(...)`` call.
    file_kind: FileEvidenceKind
    if kind == "photo":
        file_kind = "photo"
    elif kind == "voice":
        file_kind = "voice"
    else:
        file_kind = "gps"

    try:
        view = add_file_evidence(
            session,
            ctx,
            task_id=task_id,
            kind=file_kind,
            payload=payload,
            content_type=declared_type,
            storage=storage,
            mime_sniffer=mime_sniffer,
        )
    except CompletionTaskNotFound as exc:
        raise _task_not_found() from exc
    except EvidenceContentTypeNotAllowed as exc:
        # ``exc.content_type`` carries the **sniffed** type per spec
        # §15 ("MIME sniffed server-side; we trust the sniff, not the
        # header"). Surface both ``content_type`` (the sniff) and
        # ``sniffed_type`` (an explicit alias) so the operator
        # inspecting the audit envelope sees the actual shape of the
        # bytes — ``application/x-msdownload`` for a PE smuggled as
        # ``image/png`` — rather than the multipart-form lie.
        # ``declared_type`` is preserved alongside for the forensic
        # "client claimed X, sniff said Y" trail.
        raise _http(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "evidence_content_type_rejected",
            kind=exc.kind,
            content_type=exc.content_type,
            sniffed_type=exc.content_type,
            declared_type=declared_type,
            message=str(exc),
        ) from exc
    except EvidenceTooLarge as exc:
        raise _http(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "evidence_too_large",
            kind=exc.kind,
            size_bytes=exc.size_bytes,
            cap_bytes=exc.cap_bytes,
            message=str(exc),
        ) from exc
    except EvidenceGpsPayloadInvalid as exc:
        raise _http(
            422,
            "evidence_gps_payload_invalid",
            message=str(exc),
        ) from exc
    except ValueError as exc:
        # Remaining ValueErrors (empty payload, unknown kind that the
        # earlier branch let through somehow) collapse to 422 with a
        # generic envelope so the client still learns the rejection.
        raise _http(422, "evidence_invalid", message=str(exc)) from exc
    return EvidencePayload.from_view(view)
