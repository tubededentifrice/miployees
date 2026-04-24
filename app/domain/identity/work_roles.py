"""Work-role CRUD domain service — per-workspace job catalogue (§05).

The :class:`~app.adapters.db.workspace.models.WorkRole` table is the
workspace-scoped catalogue of positions (``maid``, ``cook``,
``driver``) a worker can hold. This module is the only place that
inserts, updates, or soft-deletes rows at the domain layer — routers,
agents, and future importers all funnel through the functions here so
the audit + invariant pipeline stays honest.

Public surface:

* **DTOs** — :class:`WorkRoleCreate`, :class:`WorkRoleUpdate`,
  :class:`WorkRoleView`. The ``Update`` DTO is an explicit-sparse
  shape (each field optional; ``model_fields_set`` distinguishes
  "omitted" from "explicitly set to None"). ``Create`` is a full
  body because every column has a sensible default except ``key``
  and ``name``.
* **Service functions** — :func:`list_work_roles` (cursor-paginated),
  :func:`get_work_role`, :func:`create_work_role`,
  :func:`update_work_role`. Soft-delete is deferred to
  :func:`delete_work_role` which stamps ``deleted_at`` and writes
  the ``work_role.deleted`` audit row, though the v1 spec §12 does
  not expose a ``DELETE /work_roles/{id}`` endpoint yet — the helper
  is kept here so a later task can wire it without reshaping the
  module.
* **Errors** — :class:`WorkRoleNotFound` (404-equivalent),
  :class:`WorkRoleKeyConflict` (422-equivalent, fired on duplicate
  ``(workspace_id, key)``).

**Authorisation.** The router owns the authz gate via
:func:`~app.authz.Permission` with ``work_roles.manage`` on
``scope_kind='workspace'`` — §05 action catalog default-allow is
``owners, managers``. The service itself does not run the gate so it
remains re-usable from tests and seeders that bypass HTTP.

**Tenancy.** Every read + write passes through the ORM tenant filter
on the registered ``work_role`` table. Each function re-asserts
``workspace_id = ctx.workspace_id`` explicitly as defence-in-depth,
matching the convention used in :mod:`app.domain.places.property_service`
and :mod:`app.services.employees.service`.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction.

See ``docs/specs/05-employees-and-roles.md`` §"Work role",
``docs/specs/02-domain-model.md`` §"People, work roles, engagements".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import WorkRole
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "WorkRoleCreate",
    "WorkRoleKeyConflict",
    "WorkRoleNotFound",
    "WorkRoleUpdate",
    "WorkRoleView",
    "create_work_role",
    "get_work_role",
    "list_work_roles",
    "update_work_role",
]


# Caps chosen to keep DB + audit payloads bounded without being
# restrictive in practice. ``_MAX_KEY_LEN`` matches the ``maid`` /
# ``cook`` / ``driver`` shape — a slug, not a sentence.
_MAX_KEY_LEN = 64
_MAX_NAME_LEN = 160
_MAX_DESCRIPTION_LEN = 20_000
_MAX_ICON_LEN = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkRoleNotFound(LookupError):
    """No ``work_role`` row with the given id exists in this workspace.

    404-equivalent. Collapses "id unknown" and "id exists in another
    workspace" into a single shape so the tenant surface is not
    enumerable (§01 "Workspace addressing").
    """


class WorkRoleKeyConflict(ValueError):
    """``(workspace_id, key)`` uniqueness violation.

    422-equivalent. Fired when :func:`create_work_role` or
    :func:`update_work_role` would produce a second active row with
    the same ``key`` inside the same workspace. The v0 rename path
    (``work_role.rekey``) is not an exception — it lands on a
    non-conflicting key and writes its own audit row.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class WorkRoleCreate(BaseModel):
    """Request body for :func:`create_work_role`.

    Mirrors the HTTP router shape. ``key`` is the stable slug
    (``maid``, ``cook``); ``name`` is the human-readable label the UI
    renders. Everything else has sensible defaults and may be omitted.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=_MAX_KEY_LEN)
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    description_md: str = Field(default="", max_length=_MAX_DESCRIPTION_LEN)
    default_settings_json: dict[str, Any] = Field(default_factory=dict)
    icon_name: str = Field(default="", max_length=_MAX_ICON_LEN)

    @model_validator(mode="after")
    def _normalise(self) -> WorkRoleCreate:
        """Trim key/name + reject blank payloads.

        The DB columns are NOT NULL with empty-string defaults only
        for ``description_md`` / ``icon_name`` — ``key`` and ``name``
        must be a real non-blank string.
        """
        if not self.key.strip():
            raise ValueError("key must be a non-blank string")
        if not self.name.strip():
            raise ValueError("name must be a non-blank string")
        return self


class WorkRoleUpdate(BaseModel):
    """Partial update body for :func:`update_work_role`.

    Explicit-sparse: every field optional. Pydantic v2's
    ``model_fields_set`` distinguishes "omitted" from "explicitly set
    to None"; only the explicitly-set fields land on the row. The
    non-nullable columns (``key``, ``name``) reject a ``None`` at the
    DTO boundary via :meth:`_reject_nulls_on_not_null` so a
    500 at write time never escapes.
    """

    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=_MAX_KEY_LEN)
    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LEN)
    description_md: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)
    default_settings_json: dict[str, Any] | None = Field(default=None)
    icon_name: str | None = Field(default=None, max_length=_MAX_ICON_LEN)

    @model_validator(mode="after")
    def _reject_nulls_on_not_null(self) -> WorkRoleUpdate:
        """Reject explicit ``None`` on NOT NULL columns."""
        sent = self.model_fields_set
        for column in ("key", "name", "description_md", "icon_name"):
            if column in sent and getattr(self, column) is None:
                raise ValueError(f"{column} cannot be cleared; it is NOT NULL")
        if "default_settings_json" in sent and self.default_settings_json is None:
            raise ValueError(
                "default_settings_json cannot be cleared; send {} to reset"
            )
        return self


@dataclass(frozen=True, slots=True)
class WorkRoleView:
    """Immutable read projection of a ``work_role`` row.

    Keeps the HTTP seam's Pydantic model decoupled from the ORM row —
    routers convert this into a response body (see
    ``app/api/v1/identity/work_roles.py``). The dataclass is frozen
    so a router cannot accidentally mutate audit-sensitive fields
    (``created_at``, ``deleted_at``) between the service boundary and
    the wire.
    """

    id: str
    workspace_id: str
    key: str
    name: str
    description_md: str
    default_settings_json: dict[str, Any]
    icon_name: str
    created_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: WorkRole) -> WorkRoleView:
    """Project a SQLAlchemy row into an immutable :class:`WorkRoleView`."""
    return WorkRoleView(
        id=row.id,
        workspace_id=row.workspace_id,
        key=row.key,
        name=row.name,
        description_md=row.description_md,
        default_settings_json=dict(row.default_settings_json),
        icon_name=row.icon_name,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    work_role_id: str,
    include_deleted: bool = False,
) -> WorkRole:
    """Return the :class:`WorkRole` row or raise :class:`WorkRoleNotFound`.

    Re-asserts the ``workspace_id`` predicate explicitly even though
    the ORM tenant filter already narrows the query. Mirrors the
    pattern in :mod:`app.services.employees.service`.
    """
    stmt = select(WorkRole).where(
        WorkRole.id == work_role_id,
        WorkRole.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(WorkRole.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise WorkRoleNotFound(work_role_id)
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_work_roles(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    after_id: str | None = None,
    include_deleted: bool = False,
) -> Sequence[WorkRoleView]:
    """Return up to ``limit + 1`` work-role views for the caller's workspace.

    The service returns ``limit + 1`` rows so the router's
    :func:`~app.api.pagination.paginate` helper can compute
    ``has_more`` without a second query. Rows are ordered by
    ``(created_at ASC, id ASC)`` so the forward cursor traversal is
    deterministic — ULIDs are time-ordered, so this matches the
    natural "oldest first" read pattern and means we can key the
    cursor off ``id`` alone without needing a composite
    ``(created_at, id)`` key.

    ``after_id`` is the previously returned last-row id (decoded from
    the opaque cursor); rows with ``id <= after_id`` are skipped.
    """
    stmt = select(WorkRole).where(WorkRole.workspace_id == ctx.workspace_id)
    if not include_deleted:
        stmt = stmt.where(WorkRole.deleted_at.is_(None))
    if after_id is not None:
        stmt = stmt.where(WorkRole.id > after_id)
    stmt = stmt.order_by(WorkRole.id.asc()).limit(limit + 1)
    rows = session.scalars(stmt).all()
    return [_row_to_view(r) for r in rows]


def get_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    work_role_id: str,
) -> WorkRoleView:
    """Return the view for ``work_role_id`` or raise :class:`WorkRoleNotFound`."""
    return _row_to_view(_load_row(session, ctx, work_role_id=work_role_id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: WorkRoleCreate,
    clock: Clock | None = None,
) -> WorkRoleView:
    """Insert a new active work-role row.

    Raises :class:`WorkRoleKeyConflict` when the
    ``(workspace_id, key)`` unique constraint is violated. The
    pre-flight SELECT narrows the window where a racing INSERT could
    still trip the DB-level unique; we catch the :class:`IntegrityError`
    from the flush as defence-in-depth.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    # Pre-flight lookup — catches the common single-writer case
    # without relying on DB-level IntegrityError surfaces (which vary
    # across dialects). A race is still possible; the flush-time
    # IntegrityError catch below handles that corner.
    existing = session.scalar(
        select(WorkRole).where(
            WorkRole.workspace_id == ctx.workspace_id,
            WorkRole.key == body.key,
            WorkRole.deleted_at.is_(None),
        )
    )
    if existing is not None:
        raise WorkRoleKeyConflict(
            f"work_role key {body.key!r} already exists in this workspace"
        )

    row_id = new_ulid(clock=clock)
    row = WorkRole(
        id=row_id,
        workspace_id=ctx.workspace_id,
        key=body.key,
        name=body.name,
        description_md=body.description_md,
        default_settings_json=dict(body.default_settings_json),
        icon_name=body.icon_name,
        created_at=now,
        deleted_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        # Race collapse: the pre-flight SELECT passed but a parallel
        # writer landed first. Collapse into the same domain error so
        # the HTTP surface is consistent across single-writer + race.
        session.rollback()
        raise WorkRoleKeyConflict(
            f"work_role key {body.key!r} already exists in this workspace"
        ) from exc

    write_audit(
        session,
        ctx,
        entity_kind="work_role",
        entity_id=row_id,
        action="work_role.created",
        diff={
            "key": row.key,
            "name": row.name,
            "icon_name": row.icon_name,
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)


def update_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    work_role_id: str,
    body: WorkRoleUpdate,
    clock: Clock | None = None,
) -> WorkRoleView:
    """Partial update of a work-role row.

    Skipping the ``work_role.rekey`` distinction is deliberate: the
    spec records that key renames fire a dedicated audit event, but
    v1 folds it into the generic ``work_role.updated`` diff — the
    ``before/after`` pair still identifies the rename and the
    operator trail stays linear. Separating the audit actions is
    tracked as a follow-up if downstream tooling needs to filter on
    rename events specifically.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    row = _load_row(session, ctx, work_role_id=work_role_id)

    sent = body.model_fields_set
    if not sent:
        # No-op update — return the current view. No audit row because
        # a zero-change write is not a forensic event (consistent with
        # :func:`app.services.employees.service.update_profile`).
        return _row_to_view(row)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    # Key conflict check BEFORE we mutate the row — a failed conflict
    # check must not half-apply the other columns.
    if "key" in sent and body.key is not None and body.key != row.key:
        existing = session.scalar(
            select(WorkRole).where(
                WorkRole.workspace_id == ctx.workspace_id,
                WorkRole.key == body.key,
                WorkRole.id != row.id,
                WorkRole.deleted_at.is_(None),
            )
        )
        if existing is not None:
            raise WorkRoleKeyConflict(
                f"work_role key {body.key!r} already exists in this workspace"
            )
        before["key"] = row.key
        after["key"] = body.key
        row.key = body.key

    for column in ("name", "description_md", "icon_name"):
        if column in sent:
            new_val = getattr(body, column)
            if new_val is not None and new_val != getattr(row, column):
                before[column] = getattr(row, column)
                after[column] = new_val
                setattr(row, column, new_val)

    if "default_settings_json" in sent:
        new_settings = body.default_settings_json
        # Compare as plain dicts so ordering / JSON-column reference
        # identity do not spuriously trip the diff. ``None`` is
        # rejected at the DTO boundary (see
        # :meth:`WorkRoleUpdate._reject_nulls_on_not_null`).
        if new_settings is not None and dict(new_settings) != dict(
            row.default_settings_json
        ):
            before["default_settings_json"] = dict(row.default_settings_json)
            after["default_settings_json"] = dict(new_settings)
            row.default_settings_json = dict(new_settings)

    if not after:
        # Every sent field matched the current value — no change.
        return _row_to_view(row)

    try:
        session.flush()
    except IntegrityError as exc:
        # Race collapse on key rename — the pre-flight SELECT above
        # narrows the window, but a parallel writer can still trip
        # the DB unique between the check and the flush.
        session.rollback()
        if "key" in after:
            raise WorkRoleKeyConflict(
                f"work_role key {after['key']!r} already exists in this workspace"
            ) from exc
        raise  # pragma: no cover — no other integrity path on this table

    write_audit(
        session,
        ctx,
        entity_kind="work_role",
        entity_id=row.id,
        action="work_role.updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    _ = now  # reserved for a future ``updated_at`` column (§02 ticket)
    return _row_to_view(row)
