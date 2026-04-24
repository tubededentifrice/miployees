"""``schedule`` CRUD service.

A :class:`~app.adapters.db.tasks.models.Schedule` row is the
recurrence rule that materialises concrete tasks from a
:class:`~app.adapters.db.tasks.models.TaskTemplate`. This module
is the only place that inserts, updates, pauses, resumes, soft-
deletes, or reads schedule rows at the domain layer (§01
"Handlers are thin").

Public surface:

* **DTOs** — :class:`ScheduleCreate`, :class:`ScheduleUpdate`,
  :class:`ScheduleView`. Fields per §06 "Schedule":
  ``name``, ``template_id``, ``property_id``, ``area_id``,
  ``default_assignee`` (user id), ``backup_assignee_user_ids``
  (ordered list), ``rrule``, ``dtstart_local``, ``duration_minutes``,
  ``rdate_local``, ``exdate_local``, ``active_from``,
  ``active_until``, ``paused_at``.
* **Service functions** — :func:`create` / :func:`read` /
  :func:`list_schedules` / :func:`update` / :func:`pause` /
  :func:`resume` / :func:`delete` / :func:`preview_occurrences`.
  Every mutation takes a :class:`~app.tenancy.WorkspaceContext` and
  writes one :mod:`app.audit` row in the same transaction.
* **Errors** — :class:`ScheduleNotFound` (``LookupError`` → 404),
  :class:`InvalidRRule` (``ValueError`` → 422),
  :class:`InvalidBackupWorkRole` (``ValueError`` → 422 with
  ``error = "backup_invalid_work_role"``).

**RRULE handling.** :mod:`dateutil.rrule.rrulestr` parses the body
against ``dtstart_local`` (anchored as a naive datetime — the
property timezone is applied at occurrence-generation time, not
here). We reject a rule that yields zero occurrences in its
bounded window; an unbounded rule (no ``COUNT`` / ``UNTIL``) with
no ``active_until`` is allowed — §06 expects open-ended weeklies.
RDATE / EXDATE are line-separated local ISO-8601 timestamps; we
store them verbatim and re-parse on ``preview_occurrences``.

**Backup-list validation.** §06 "Backup list validation" says
every user in ``backup_assignee_user_ids`` must hold a
``user_work_role`` matching the schedule's ``expected_role_id`` at
write time; 422 ``backup_invalid_work_role`` otherwise. The
``user_work_role`` table lands with cd-5kv4 but the real validator
— a query that asserts each user holds a matching active
``user_work_role`` — plugs in with the employees service (cd-dv2).
We expose an injectable validator hook
(:data:`BackupAssigneeValidator`) so the service, the router, and
cd-dv2 can wire the real check in without another service-wide
refactor. The default validator is a no-op — it returns an empty
list, mirroring the "hook exists, implementation lands with a
downstream task" pattern in :mod:`app.domain.tasks.templates`.

**Pause vs active range.** Per §06 "Pause vs active range" a
non-null ``paused_at`` always wins. :func:`pause` and
:func:`resume` toggle the column and write one audit row each;
active-range fields are untouched. Pause does not cancel already-
materialised tasks — §06 is explicit.

**Deleting and editing.** :func:`delete` soft-deletes and cancels
every linked task with ``state = scheduled`` to ``state =
cancelled`` + ``cancellation_reason = 'schedule deleted'`` (§06
"Deleting and editing").  :func:`update` takes an
``apply_to_existing`` flag — when ``True`` the service patches
tasks with ``state IN ('scheduled', 'pending')`` only (spec: "Apply
to existing tasks").

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3).

See ``docs/specs/06-tasks-and-scheduling.md`` §"Schedule",
§"UI expose-levels", §"Pause / resume", §"Deleting and editing",
§"Pause vs active range", §"Assignment algorithm".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from dateutil.rrule import rrulestr
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "BackupAssigneeValidator",
    "InvalidBackupWorkRole",
    "InvalidRRule",
    "ScheduleCreate",
    "ScheduleNotFound",
    "ScheduleUpdate",
    "ScheduleView",
    "create",
    "delete",
    "list_schedules",
    "pause",
    "preview_occurrences",
    "read",
    "resume",
    "update",
]


# ---------------------------------------------------------------------------
# Validator hook
# ---------------------------------------------------------------------------


BackupAssigneeValidator = Callable[
    [Session, WorkspaceContext, str, Sequence[str]], list[str]
]
"""Signature for the backup-assignee work-role validator hook.

Called with ``(session, ctx, role_id, user_ids)``; returns the
list of user ids that **do not** hold a matching
``user_work_role``. The default validator
(:func:`_default_validator`) returns an empty list — the
``user_work_role`` table lands with cd-5kv4, and the employees
service (cd-dv2) replaces the default with a real query. Until
cd-dv2 the service accepts any user id in the backup list.

Keeping this as an injectable hook rather than a hard-wired
dependency lets:

* the unit tests assert the 422 branch fires without needing the
  full ``user_work_role`` schema;
* cd-dv2 wire the real check without widening every call site;
* the router + CLI inject a context-aware validator that queries
  the actual table when the caller is a real user.
"""


def _default_validator(
    session: Session,
    ctx: WorkspaceContext,
    role_id: str,
    user_ids: Sequence[str],
) -> list[str]:
    """Return an empty list — the real check lands with cd-dv2.

    The ``user_work_role`` table lands with cd-5kv4, but the real
    validator query — "every user id holds an active user_work_role
    matching ``role_id`` in this workspace" — is owned by the
    employees service (cd-dv2). Accepting any id in the interim is
    safer than inventing a check that will drift from the eventual
    domain layer.
    """
    # Unused arguments kept in the signature so the contract stays
    # stable once cd-dv2 lands — flagging them to the linter.
    _ = session, ctx, role_id, user_ids
    return []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScheduleNotFound(LookupError):
    """The requested schedule does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`read`, :func:`update`,
    :func:`pause`, :func:`resume`, and :func:`delete` when the id
    is unknown or already soft-deleted. Soft-deleted rows are
    hidden from every non-admin read path; an explicit
    ``deleted=True`` filter on :func:`list_schedules` surfaces
    them.
    """


class InvalidRRule(ValueError):
    """The RRULE body, DTSTART, or RDATE/EXDATE payload is unparsable.

    422-equivalent. Fires when :mod:`dateutil.rrule.rrulestr`
    rejects the body, when the rule produces zero occurrences in
    its bounded window, or when an RDATE / EXDATE line is not a
    valid ISO-8601 local timestamp. The message identifies the
    offending payload so the UI can surface it next to the right
    input.
    """


class InvalidBackupWorkRole(ValueError):
    """Backup-list entry does not hold a matching ``user_work_role``.

    422-equivalent. Carries ``error = "backup_invalid_work_role"``
    per §06 "Backup list validation" and the offending user ids so
    the UI can highlight which rows are invalid. The caller maps
    this exception to the spec's error code in the HTTP layer.
    """

    error: str = "backup_invalid_work_role"

    def __init__(
        self,
        *,
        schedule_id: str | None,
        invalid_user_ids: Sequence[str],
        role_id: str | None,
    ) -> None:
        self.schedule_id = schedule_id
        self.invalid_user_ids: tuple[str, ...] = tuple(invalid_user_ids)
        self.role_id = role_id
        super().__init__(
            f"backup_invalid_work_role: {len(self.invalid_user_ids)} user(s) "
            f"do not hold a user_work_role matching role_id={role_id!r}"
        )


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps chosen to keep the DB + audit payload bounded. Schedule
# name tracks the template convention (200 chars). RRULE / RDATE /
# EXDATE bodies have real-world ceilings in the low KB; 8 KB is a
# friendly upper bound. The backup-assignee list cap mirrors the
# property-scope cap on templates.
_MAX_NAME_LEN = 200
_MAX_RRULE_LEN = 8_000
_MAX_RDATE_LEN = 8_000
_MAX_EXDATE_LEN = 8_000
_MAX_BACKUP_ASSIGNEES = 100
_MAX_ID_LEN = 64
# Preview cap matches the §06 mock ("Preview — next 7 days"):
# callers asking for 1k occurrences are almost always bugs.
_MAX_PREVIEW_COUNT = 500


class _ScheduleBody(BaseModel):
    """Shared body of the create + update DTOs.

    The DTO validates shape only — it does not touch the DB.
    Workspace scoping, the RRULE parse, and the backup-assignee
    work-role check all run inside the service (a service error is
    a 422 the router lifts to the client; a DTO error is a 422 the
    pydantic framework handles on ingress).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    template_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    area_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    default_assignee: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    backup_assignee_user_ids: list[str] = Field(
        default_factory=list, max_length=_MAX_BACKUP_ASSIGNEES
    )
    rrule: str = Field(..., min_length=1, max_length=_MAX_RRULE_LEN)
    # Property-local ISO-8601 (``2026-04-20T09:00`` or
    # ``2026-04-20T09:00:00``). The body is parsed in the service
    # via :func:`datetime.fromisoformat` after stripping any tz
    # suffix — the value is intentionally timezone-naive here; the
    # scheduler worker applies ``property.timezone`` at generation
    # time.
    dtstart_local: str = Field(..., min_length=1, max_length=32)
    duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    rdate_local: str = Field(default="", max_length=_MAX_RDATE_LEN)
    exdate_local: str = Field(default="", max_length=_MAX_EXDATE_LEN)
    active_from: date
    active_until: date | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> _ScheduleBody:
        """Enforce the shape rules pydantic doesn't catch on its own.

        * ``active_until`` must not precede ``active_from`` — a
          backwards window would never generate anything.
        * ``backup_assignee_user_ids`` must not contain duplicates
          and must not repeat ``default_assignee`` (the spec walks
          the list ``[default, *backups]`` in order; a duplicate is
          dead weight that also confuses the assignment-audit
          payload).
        * Empty strings in the backup list are rejected — caller
          must pass real user ids.
        """
        if self.active_until is not None and self.active_until < self.active_from:
            raise ValueError("active_until must be on or after active_from")
        if len(set(self.backup_assignee_user_ids)) != len(
            self.backup_assignee_user_ids
        ):
            raise ValueError("backup_assignee_user_ids must not contain duplicates")
        for uid in self.backup_assignee_user_ids:
            if not uid:
                raise ValueError("backup_assignee_user_ids entries must be non-empty")
        if (
            self.default_assignee is not None
            and self.default_assignee in self.backup_assignee_user_ids
        ):
            raise ValueError(
                "default_assignee must not appear in backup_assignee_user_ids"
            )
        return self


class ScheduleCreate(_ScheduleBody):
    """Request body for ``POST /api/v1/schedules``."""


class ScheduleUpdate(_ScheduleBody):
    """Request body for ``PATCH /api/v1/schedules/{id}``.

    v1 treats update as a full replacement of the mutable body,
    matching the :class:`~app.domain.tasks.templates.TaskTemplateUpdate`
    convention. A per-field PATCH can follow once the UI needs it.
    """


@dataclass(frozen=True, slots=True)
class ScheduleView:
    """Immutable read projection of a ``schedule`` row.

    ``active_from`` is ``Optional[date]`` because the cd-k4l migration
    landed the column as ``NULL``-able (existing cd-chd rows survive
    without a backfill). Every write that flows through :func:`create`
    / :func:`update` populates it (the DTO requires it), so in practice
    the value is ``None`` only on pre-migration survivors; matching
    the DB nullability here keeps the view honest rather than
    surfacing a misleading ``date.min`` placeholder.
    """

    id: str
    workspace_id: str
    name: str
    template_id: str
    property_id: str | None
    area_id: str | None
    default_assignee: str | None
    backup_assignee_user_ids: tuple[str, ...]
    rrule: str
    dtstart_local: str
    duration_minutes: int | None
    rdate_local: str
    exdate_local: str
    active_from: date | None
    active_until: date | None
    paused_at: datetime | None
    created_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# RRULE + date parsing helpers
# ---------------------------------------------------------------------------


def _parse_local_datetime(value: str) -> datetime:
    """Parse a property-local ISO-8601 timestamp.

    The value must be timezone-naive — these fields live in the
    property frame and the scheduler worker applies
    ``property.timezone`` at occurrence-generation time. A tz-aware
    input (``2026-04-20T09:00+02:00`` or ``…Z``) is rejected
    explicitly rather than silently stripped: silently coercing a
    ``+02:00`` local-clock into naive ``09:00`` would hide authoring
    errors and produce occurrences at the wrong wall-clock. Raises
    :class:`InvalidRRule` with a descriptive message on any parse
    failure so the service can route every malformed-input path
    through the same 422 surface.
    """
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidRRule(
            f"dtstart_local must be an ISO-8601 local timestamp; got {value!r}"
        ) from exc
    if parsed.tzinfo is not None:
        raise InvalidRRule(
            "dtstart_local must be timezone-naive (property-local); "
            f"got tz-aware {value!r} — strip the zone and use the "
            "property's wall-clock time"
        )
    return parsed


def _split_rdate_lines(payload: str) -> list[str]:
    """Split an RDATE / EXDATE payload into individual ISO-8601 lines.

    Accepts either line-separated (``\\n``) or semicolon-separated
    input; empty segments are dropped. The caller is free to pass
    the empty string; an empty payload returns an empty list.
    """
    if not payload.strip():
        return []
    separators = ("\n", ";")
    parts: list[str] = [payload]
    for sep in separators:
        parts = [piece for chunk in parts for piece in chunk.split(sep)]
    return [piece.strip() for piece in parts if piece.strip()]


def _parse_rdate_payload(payload: str, *, field: str) -> list[datetime]:
    """Parse every line in an RDATE / EXDATE payload.

    Raises :class:`InvalidRRule` with a message that names the
    offending field (``rdate_local`` / ``exdate_local``) so the UI
    can surface the error next to the right input.
    """
    lines = _split_rdate_lines(payload)
    out: list[datetime] = []
    for line in lines:
        try:
            parsed = _parse_local_datetime(line)
        except InvalidRRule as exc:
            raise InvalidRRule(
                f"{field} contains invalid ISO-8601 entry {line!r}"
            ) from exc
        out.append(parsed)
    return out


def _validate_rrule(
    rrule: str, *, dtstart_local: str, rdate_local: str, exdate_local: str
) -> None:
    """Sanity-check the RRULE + DTSTART + RDATE / EXDATE shape.

    * Parse the RRULE with :mod:`dateutil.rrule.rrulestr` anchored
      at ``dtstart_local``.
    * Iterate the first occurrence — this catches bounded rules
      that produce zero results (e.g. ``COUNT=0``, ``UNTIL`` before
      ``DTSTART``).
    * Parse RDATE / EXDATE lines.

    An unbounded rule (no ``COUNT`` / ``UNTIL``) with no
    ``active_until`` is legal — §06 expects open-ended weeklies.
    """
    anchor = _parse_local_datetime(dtstart_local)
    try:
        parsed = rrulestr(rrule, dtstart=anchor)
    except (ValueError, TypeError) as exc:
        raise InvalidRRule(f"invalid rrule: {exc}") from exc

    # An unbounded rule will yield the anchor immediately. A bounded
    # rule that produces nothing (``COUNT=0`` or ``UNTIL`` <
    # ``DTSTART``) raises here via ``next(iter(parsed), None)``.
    first = next(iter(parsed), None)
    if first is None:
        raise InvalidRRule("rrule produces zero occurrences — check COUNT / UNTIL")

    _parse_rdate_payload(rdate_local, field="rdate_local")
    _parse_rdate_payload(exdate_local, field="exdate_local")


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _coerce_str_list(raw: object) -> tuple[str, ...]:
    """Narrow the JSON-column payload into a ``tuple[str, ...]``.

    SQLAlchemy's ``JSON`` surface types as :class:`Any` on load; the
    ORM column is declared as ``list[str]`` but the runtime value
    can be ``None`` on a row that predates the cd-k4l migration.
    Narrowing here keeps :func:`_row_to_view` free of per-call
    guards.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"expected list payload on schedule row, got {type(raw)!r}")
    out: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ValueError(
                "backup_assignee_user_ids contains non-string entries on "
                "a loaded schedule row"
            )
        out.append(value)
    return tuple(out)


def _row_to_view(row: Schedule) -> ScheduleView:
    """Project a loaded :class:`Schedule` row into a read view.

    Reads fall back to the cd-chd columns (``rrule_text``, ``dtstart``)
    when the cd-k4l columns are ``NULL`` on a pre-migration row. New
    writes through the service always fill both names, so this
    fallback only matters for rows created before the migration.
    """
    return ScheduleView(
        id=row.id,
        workspace_id=row.workspace_id,
        # Pre-migration rows have no ``name`` — fall back to a
        # deterministic placeholder so callers don't NPE. New writes
        # always populate this column.
        name=row.name if row.name is not None else "",
        template_id=row.template_id,
        property_id=row.property_id,
        area_id=row.area_id,
        default_assignee=row.assignee_user_id,
        backup_assignee_user_ids=_coerce_str_list(row.backup_assignee_user_ids),
        rrule=row.rrule_text,
        # Pre-migration rows only carry the UTC ``dtstart``; project
        # it back to naive ISO-8601 so the view stays shape-stable.
        dtstart_local=(
            row.dtstart_local
            if row.dtstart_local is not None
            else row.dtstart.replace(tzinfo=None).isoformat(timespec="minutes")
        ),
        duration_minutes=row.duration_minutes,
        rdate_local=row.rdate_local or "",
        exdate_local=row.exdate_local or "",
        # ``active_from`` is nullable on the DB (pre-migration rows
        # carry NULL); new writes always populate it. Surface ``None``
        # rather than a misleading ``date.min`` so callers can detect
        # the pre-migration case explicitly.
        active_from=(
            date.fromisoformat(row.active_from) if row.active_from is not None else None
        ),
        active_until=(
            date.fromisoformat(row.active_until)
            if row.active_until is not None
            else None
        ),
        paused_at=row.paused_at,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _view_to_diff_dict(view: ScheduleView) -> dict[str, Any]:
    """Flatten a :class:`ScheduleView` into a JSON-safe dict."""
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "name": view.name,
        "template_id": view.template_id,
        "property_id": view.property_id,
        "area_id": view.area_id,
        "default_assignee": view.default_assignee,
        "backup_assignee_user_ids": list(view.backup_assignee_user_ids),
        "rrule": view.rrule,
        "dtstart_local": view.dtstart_local,
        "duration_minutes": view.duration_minutes,
        "rdate_local": view.rdate_local,
        "exdate_local": view.exdate_local,
        "active_from": (
            view.active_from.isoformat() if view.active_from is not None else None
        ),
        "active_until": (
            view.active_until.isoformat() if view.active_until is not None else None
        ),
        "paused_at": view.paused_at.isoformat() if view.paused_at is not None else None,
        "created_at": view.created_at.isoformat(),
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
    }


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    include_deleted: bool = False,
) -> Schedule:
    """Load ``schedule_id`` scoped to the caller's workspace.

    Matches the convention on
    :func:`app.domain.tasks.templates._load_row`: the ORM tenant
    filter already constrains SELECTs to ``ctx.workspace_id``; the
    explicit predicate below is defence-in-depth against a
    misconfigured context.
    """
    stmt = select(Schedule).where(
        Schedule.id == schedule_id,
        Schedule.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(Schedule.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ScheduleNotFound(schedule_id)
    return row


def _load_template(
    session: Session, ctx: WorkspaceContext, *, template_id: str
) -> TaskTemplate:
    """Load the parent template (live only) — raises on unknown id.

    A schedule cannot exist without a live parent template. The
    error is ``ScheduleNotFound``-adjacent but semantically a 422:
    the template doesn't exist in this workspace. We raise
    :class:`ValueError` so the router maps it to 422.
    """
    stmt = select(TaskTemplate).where(
        TaskTemplate.id == template_id,
        TaskTemplate.workspace_id == ctx.workspace_id,
        TaskTemplate.deleted_at.is_(None),
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ValueError(
            f"template_id {template_id!r} is not a live template in this workspace"
        )
    return row


def _apply_body(
    row: Schedule,
    body: _ScheduleBody,
    *,
    template: TaskTemplate,
) -> None:
    """Copy every mutable DTO field onto ``row``.

    Writes through to both the new cd-k4l columns and the legacy
    cd-chd pair (``rrule_text``, ``dtstart``, ``assignee_user_id``,
    ``enabled``) so the row stays readable by existing cd-chd
    adapters until a follow-up migration drops the legacy pair.
    """
    row.name = body.name
    row.template_id = body.template_id
    row.property_id = body.property_id
    row.area_id = body.area_id
    row.assignee_user_id = body.default_assignee
    row.backup_assignee_user_ids = list(body.backup_assignee_user_ids)
    row.rrule_text = body.rrule
    row.dtstart_local = body.dtstart_local
    # Legacy ``dtstart`` column is NOT NULL; mirror from the local
    # anchor. The column carries a naive UTC read (tz-aware on
    # Postgres, stripped by SQLite) — we store the local anchor as
    # if it were UTC so existing cd-chd adapters that query ``dtstart``
    # still get a usable value. The scheduler worker resolves to
    # the real UTC via ``property.timezone`` at generation time.
    anchor_naive = _parse_local_datetime(body.dtstart_local)
    row.dtstart = anchor_naive
    row.duration_minutes = (
        body.duration_minutes
        if body.duration_minutes is not None
        else template.duration_minutes
    )
    row.rdate_local = body.rdate_local or ""
    row.exdate_local = body.exdate_local or ""
    row.active_from = body.active_from.isoformat()
    row.active_until = (
        body.active_until.isoformat() if body.active_until is not None else None
    )
    # Preserve cd-chd legacy ``enabled`` — callers use ``paused_at``
    # in cd-k4l; ``enabled = True`` by default and flipped only via
    # :func:`pause` / :func:`resume` below.
    if row.enabled is None:
        row.enabled = True


# ---------------------------------------------------------------------------
# Backup-list validation
# ---------------------------------------------------------------------------


def _assert_backup_work_roles(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str | None,
    role_id: str | None,
    backup_user_ids: Sequence[str],
    validator: BackupAssigneeValidator,
) -> None:
    """Run the injectable validator; raise :class:`InvalidBackupWorkRole` on failure.

    Skipped when the schedule has no ``role_id`` to validate
    against — without a role the spec's "matching ``user_work_role``"
    check has no reference point. The backup list still exists; the
    assignment algorithm walks it as-is and the UI is free to show
    the unresolved entries.
    """
    if not backup_user_ids or role_id is None:
        return
    invalid = validator(session, ctx, role_id, list(backup_user_ids))
    if invalid:
        raise InvalidBackupWorkRole(
            schedule_id=schedule_id,
            invalid_user_ids=invalid,
            role_id=role_id,
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    include_deleted: bool = False,
) -> ScheduleView:
    """Return the live schedule identified by ``schedule_id``."""
    row = _load_row(
        session, ctx, schedule_id=schedule_id, include_deleted=include_deleted
    )
    return _row_to_view(row)


def list_schedules(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str | None = None,
    property_id: str | None = None,
    paused: bool | None = None,
    deleted: bool = False,
) -> Sequence[ScheduleView]:
    """Return every schedule in the caller's workspace, optionally filtered.

    Filter semantics:

    * ``template_id`` — strict equality; returns every schedule
      linked to that template.
    * ``property_id`` — strict equality; ``None`` returns every
      schedule regardless of property. A caller who wants
      workspace-wide schedules (``property_id IS NULL`` on the row)
      can filter client-side.
    * ``paused`` — ``True`` returns only paused schedules,
      ``False`` returns only live ones, ``None`` (the default)
      returns both.
    * ``deleted`` — mirrors :func:`app.domain.tasks.templates.list_templates`:
      ``False`` returns only live rows, ``True`` returns only soft-
      deleted rows.

    Ordering: ``created_at`` ascending with ``id`` tiebreaker.
    """
    stmt = select(Schedule).where(Schedule.workspace_id == ctx.workspace_id)
    if deleted:
        stmt = stmt.where(Schedule.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Schedule.deleted_at.is_(None))
    if template_id is not None:
        stmt = stmt.where(Schedule.template_id == template_id)
    if property_id is not None:
        stmt = stmt.where(Schedule.property_id == property_id)
    if paused is True:
        stmt = stmt.where(Schedule.paused_at.is_not(None))
    elif paused is False:
        stmt = stmt.where(Schedule.paused_at.is_(None))
    stmt = stmt.order_by(Schedule.created_at.asc(), Schedule.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: ScheduleCreate,
    clock: Clock | None = None,
    backup_validator: BackupAssigneeValidator | None = None,
) -> ScheduleView:
    """Insert a fresh schedule row and record one audit entry.

    Validates the RRULE + DTSTART + RDATE/EXDATE shape, loads the
    parent template for the role-id check, runs the backup-
    assignee validator, and writes the row. Returns the full
    :class:`ScheduleView`.
    """
    now = (clock if clock is not None else SystemClock()).now()
    validator = backup_validator if backup_validator is not None else _default_validator

    template = _load_template(session, ctx, template_id=body.template_id)
    _validate_rrule(
        body.rrule,
        dtstart_local=body.dtstart_local,
        rdate_local=body.rdate_local,
        exdate_local=body.exdate_local,
    )
    _assert_backup_work_roles(
        session,
        ctx,
        schedule_id=None,
        role_id=template.role_id,
        backup_user_ids=body.backup_assignee_user_ids,
        validator=validator,
    )

    row = Schedule(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        created_at=now,
        # ``_apply_body`` fills the rest. ``enabled`` defaults to
        # ``True`` so the cd-chd columns stay legal.
        enabled=True,
    )
    _apply_body(row, body, template=template)
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="schedule",
        entity_id=row.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=clock,
    )
    return view


def update(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    body: ScheduleUpdate,
    apply_to_existing: bool = False,
    clock: Clock | None = None,
    backup_validator: BackupAssigneeValidator | None = None,
) -> ScheduleView:
    """Replace the mutable body of ``schedule_id``.

    When ``apply_to_existing=True`` the service patches every
    linked task with ``state IN ('scheduled', 'pending')`` to
    reflect the new ``duration_minutes`` / ``assignee`` /
    ``property_id`` / ``area_id``. Materialised tasks in other
    states are untouched (spec: "Apply to existing tasks").

    Writes one audit row for the schedule diff; when
    ``apply_to_existing=True`` fires, a second
    ``schedule.apply_to_existing`` audit row records the patched
    task ids.
    """
    validator = backup_validator if backup_validator is not None else _default_validator
    row = _load_row(session, ctx, schedule_id=schedule_id)
    before = _row_to_view(row)

    template = _load_template(session, ctx, template_id=body.template_id)
    _validate_rrule(
        body.rrule,
        dtstart_local=body.dtstart_local,
        rdate_local=body.rdate_local,
        exdate_local=body.exdate_local,
    )
    _assert_backup_work_roles(
        session,
        ctx,
        schedule_id=row.id,
        role_id=template.role_id,
        backup_user_ids=body.backup_assignee_user_ids,
        validator=validator,
    )

    _apply_body(row, body, template=template)
    # Reset the scheduler's hot-path key: the RRULE / DTSTART / active-
    # range body has just been replaced wholesale, so any pre-computed
    # ``next_generation_at`` is stale. ``NULL`` tells the worker to
    # treat the row as due immediately (matches the docstring on
    # :class:`Schedule.next_generation_at`).
    row.next_generation_at = None
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="schedule",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )

    if apply_to_existing:
        patched = _apply_update_to_existing_tasks(
            session, ctx, schedule_id=row.id, new_view=after
        )
        write_audit(
            session,
            ctx,
            entity_kind="schedule",
            entity_id=row.id,
            action="apply_to_existing",
            diff={
                "patched_task_ids": patched,
                "new": {
                    "duration_minutes": after.duration_minutes,
                    "default_assignee": after.default_assignee,
                    "property_id": after.property_id,
                    "area_id": after.area_id,
                },
            },
            clock=clock,
        )
    return after


def _apply_update_to_existing_tasks(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    new_view: ScheduleView,
) -> list[str]:
    """Patch linked tasks in ``state IN ('scheduled', 'pending')``.

    Per §06 "Deleting and editing". Returns the ids of the rows
    that were updated, in ascending order, so the audit row is
    deterministic.
    """
    stmt = (
        select(Occurrence)
        .where(
            Occurrence.workspace_id == ctx.workspace_id,
            Occurrence.schedule_id == schedule_id,
            Occurrence.state.in_(("scheduled", "pending")),
        )
        .order_by(Occurrence.id.asc())
    )
    rows = session.scalars(stmt).all()
    patched: list[str] = []
    for occ in rows:
        # Patch the pointers the v1 :class:`Occurrence` schema
        # carries: assignee + property. ``area_id`` / ``duration_
        # minutes`` land on the occurrence row with cd-22e + cd-sn26
        # and will extend this loop. ``starts_at`` / ``ends_at`` stay
        # untouched — the spec does not re-schedule already-
        # materialised rows (§06 "Deleting and editing").
        occ.assignee_user_id = new_view.default_assignee
        if new_view.property_id is not None:
            occ.property_id = new_view.property_id
        patched.append(occ.id)
    session.flush()
    return patched


def pause(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    clock: Clock | None = None,
) -> ScheduleView:
    """Set ``paused_at`` on ``schedule_id``; record one audit row.

    Idempotent-ish: pausing an already-paused schedule writes a
    fresh audit row (the caller explicitly asked) but leaves the
    ``paused_at`` timestamp untouched so the pause anchor stays
    the original pause instant. Does not cancel materialised
    tasks (§06 "Pause / resume").
    """
    now = (clock if clock is not None else SystemClock()).now()
    row = _load_row(session, ctx, schedule_id=schedule_id)
    before = _row_to_view(row)
    if row.paused_at is None:
        row.paused_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="schedule",
        entity_id=row.id,
        action="pause",
        diff={
            "before": {"paused_at": _iso(before.paused_at)},
            "after": {"paused_at": _iso(after.paused_at)},
        },
        clock=clock,
    )
    return after


def resume(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    clock: Clock | None = None,
) -> ScheduleView:
    """Clear ``paused_at`` on ``schedule_id``; record one audit row.

    Idempotent: resuming an already-live schedule writes a fresh
    audit row but leaves the column untouched.
    """
    row = _load_row(session, ctx, schedule_id=schedule_id)
    before = _row_to_view(row)
    row.paused_at = None
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="schedule",
        entity_id=row.id,
        action="resume",
        diff={
            "before": {"paused_at": _iso(before.paused_at)},
            "after": {"paused_at": _iso(after.paused_at)},
        },
        clock=clock,
    )
    return after


def delete(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    clock: Clock | None = None,
) -> ScheduleView:
    """Soft-delete ``schedule_id`` and cancel its pre-materialised tasks.

    Per §06 "Deleting and editing": sets ``deleted_at`` on the
    schedule row and cancels every linked task with ``state =
    scheduled`` (sets ``state = cancelled``, ``cancellation_reason
    = 'schedule deleted'``). Already-actionable or in-progress
    tasks (``state IN ('pending', 'in_progress', 'done', ...)``)
    are **not** cancelled — the spec is explicit about
    ``scheduled`` only.

    Raises :class:`ScheduleNotFound` if the id is unknown or
    already soft-deleted. The schedule's own audit row is
    ``schedule.delete``; the cascade records one
    ``schedule.delete_cascade`` audit entry listing the cancelled
    task ids.
    """
    now = (clock if clock is not None else SystemClock()).now()
    row = _load_row(session, ctx, schedule_id=schedule_id)
    before = _row_to_view(row)
    row.deleted_at = now
    # Defence-in-depth against any scheduler code that only checks the
    # legacy ``enabled`` flag and ``next_generation_at`` without
    # consulting ``deleted_at``: make the tombstone unambiguously
    # inert for both the cd-chd and cd-k4l generator paths.
    row.enabled = False
    row.next_generation_at = None
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="schedule",
        entity_id=row.id,
        action="delete",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )

    cancelled = _cancel_scheduled_tasks(
        session, ctx, schedule_id=row.id, reason="schedule deleted"
    )
    if cancelled:
        write_audit(
            session,
            ctx,
            entity_kind="schedule",
            entity_id=row.id,
            action="delete_cascade",
            diff={
                "cancelled_task_ids": cancelled,
                "cancellation_reason": "schedule deleted",
            },
            clock=clock,
        )
    return after


def _cancel_scheduled_tasks(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    reason: str,
) -> list[str]:
    """Cancel every linked task with ``state = scheduled``.

    Matches §06 "Deleting and editing" ("cancels all
    ``state = scheduled`` tasks linked to it"). Tasks in every
    other state are untouched — explicitly scoped to the pre-
    materialised queue. Returns the ids of the rows that were
    cancelled, in ascending order.
    """
    stmt = (
        select(Occurrence)
        .where(
            Occurrence.workspace_id == ctx.workspace_id,
            Occurrence.schedule_id == schedule_id,
            Occurrence.state == "scheduled",
        )
        .order_by(Occurrence.id.asc())
    )
    rows = session.scalars(stmt).all()
    cancelled: list[str] = []
    for occ in rows:
        occ.state = "cancelled"
        occ.cancellation_reason = reason
        cancelled.append(occ.id)
    session.flush()
    return cancelled


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def preview_occurrences(
    rrule: str,
    dtstart_local: str,
    *,
    n: int = 5,
    rdate_local: str = "",
    exdate_local: str = "",
) -> list[datetime]:
    """Return the next ``n`` local occurrences of ``rrule``.

    Drives the `/schedules` "Preview — next 7 days" panel and the
    editor's "next 5 occurrences" preview. RDATE lines are merged
    in, EXDATE lines subtract; the result is sorted ascending and
    truncated to ``n``.

    This function is pure — no DB access, no side effects — so the
    router can call it as a validation helper during schedule-editor
    live preview without allocating a transaction.

    Raises :class:`InvalidRRule` on any parse failure so the router
    can map to 422.
    """
    if n <= 0 or n > _MAX_PREVIEW_COUNT:
        raise ValueError(f"n must be in 1..{_MAX_PREVIEW_COUNT}; got {n}")

    anchor = _parse_local_datetime(dtstart_local)
    try:
        parsed = rrulestr(rrule, dtstart=anchor)
    except (ValueError, TypeError) as exc:
        raise InvalidRRule(f"invalid rrule: {exc}") from exc

    rdates = _parse_rdate_payload(rdate_local, field="rdate_local")
    exdates_set = set(_parse_rdate_payload(exdate_local, field="exdate_local"))

    # Walk the bounded side first: an unbounded rule will yield
    # forever, so we stop as soon as we have enough *unique* dates
    # once EXDATE + RDATE are applied.
    merged: list[datetime] = []
    seen: set[datetime] = set()
    # Include every RDATE regardless of whether it would otherwise
    # fall on an RRULE occurrence — RDATE is additive.
    for extra in rdates:
        if extra not in seen:
            seen.add(extra)
            merged.append(extra)

    # Pull RRULE occurrences, skipping excluded ones, until we have
    # at least ``n`` post-filter results. A bounded RRULE stops
    # naturally; we cap at ``_MAX_PREVIEW_COUNT`` for an unbounded
    # rule whose RDATE / EXDATE interaction would otherwise loop
    # without producing ``n`` survivors.
    rrule_iter: Iterable[datetime] = iter(parsed)
    pulled = 0
    for occ in rrule_iter:
        pulled += 1
        if pulled > _MAX_PREVIEW_COUNT * 4:
            break
        if occ in exdates_set or occ in seen:
            continue
        seen.add(occ)
        merged.append(occ)
        if len(merged) >= max(n * 2, n + len(rdates)):
            break

    # Sort, then truncate — RDATE may predate the anchor.
    return sorted(merged)[:n]


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Stringify a ``datetime`` for JSON-safe audit payloads.

    Extracted so the audit payload shape on :func:`pause` /
    :func:`resume` stays compact; the full
    :func:`_view_to_diff_dict` would re-emit every column on every
    toggle, which is noise in the audit feed.
    """
    return value.isoformat() if value is not None else None
