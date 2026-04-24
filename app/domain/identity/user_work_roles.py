"""User-work-role CRUD domain service (§05 "User work role").

The :class:`~app.adapters.db.workspace.models.UserWorkRole` row links
a user to a :class:`~app.adapters.db.workspace.models.WorkRole` inside
a workspace. This module is the only place that inserts, updates, or
soft-deletes rows at the domain layer; the HTTP router in
:mod:`app.api.v1.identity.user_work_roles` is a thin DTO passthrough.

Public surface:

* **DTOs** — :class:`UserWorkRoleCreate` / :class:`UserWorkRoleUpdate` /
  :class:`UserWorkRoleView`. Update is explicit-sparse; create takes
  the full body.
* **Service functions** — :func:`list_user_work_roles` (per-user,
  cursor-paginated), :func:`get_user_work_role`,
  :func:`create_user_work_role`, :func:`update_user_work_role`,
  :func:`delete_user_work_role`.
* **Errors** — :class:`UserWorkRoleNotFound`,
  :class:`UserWorkRoleInvariantViolated`.

**Invariants enforced here.** §05 "User work role" pins two:

1. The assigned :class:`WorkRole` must belong to the same workspace
   as the user link. :func:`create_user_work_role` rejects
   cross-workspace work-role references with
   :class:`UserWorkRoleInvariantViolated`.
2. A given ``(user_id, workspace_id, work_role_id, started_on)``
   tuple is unique (DB constraint). The service catches the resulting
   :class:`IntegrityError` and surfaces it as a typed error.

**Tenancy.** The ORM tenant filter auto-narrows every SELECT on the
workspace-scoped ``user_work_role`` table; the service re-asserts the
``workspace_id = ctx.workspace_id`` predicate explicitly as
defence-in-depth.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries. Every mutation writes one :mod:`app.audit` row in the
same transaction.

See ``docs/specs/05-employees-and-roles.md`` §"User work role",
``docs/specs/02-domain-model.md`` §"People, work roles, engagements".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import UserWorkRole, UserWorkspace, WorkRole
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "UserWorkRoleCreate",
    "UserWorkRoleInvariantViolated",
    "UserWorkRoleNotFound",
    "UserWorkRoleUpdate",
    "UserWorkRoleView",
    "create_user_work_role",
    "delete_user_work_role",
    "get_user_work_role",
    "list_user_work_roles",
    "update_user_work_role",
]


_MAX_ID_LEN = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UserWorkRoleNotFound(LookupError):
    """The target ``user_work_role`` row is invisible to the caller.

    404-equivalent. Fired when the id is unknown, soft-deleted, or
    lives in a different workspace — all three collapse to the same
    surface per §01 "tenant surface is not enumerable".
    """


class UserWorkRoleInvariantViolated(ValueError):
    """Write would violate a §05 "User work role" invariant.

    422-equivalent. Thrown when:

    * the referenced ``work_role`` lives in a different workspace
      from the caller (cross-workspace borrow is forbidden);
    * the user is not a member of the caller's workspace
      (``user_workspace`` row missing — no linking work-role for a
      stranger);
    * the ``(user, workspace, role, started_on)`` tuple already
      exists (DB unique violation collapsed here).
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class UserWorkRoleCreate(BaseModel):
    """Request body for :func:`create_user_work_role`."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    work_role_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    started_on: date
    ended_on: date | None = None
    pay_rule_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)

    @model_validator(mode="after")
    def _validate_dates(self) -> UserWorkRoleCreate:
        """Reject ``ended_on < started_on``."""
        if self.ended_on is not None and self.ended_on < self.started_on:
            raise ValueError("ended_on must be on or after started_on")
        return self


class UserWorkRoleUpdate(BaseModel):
    """Partial update body for :func:`update_user_work_role`.

    Explicit-sparse — only sent fields land. ``started_on`` and
    ``work_role_id`` / ``user_id`` are deliberately excluded because
    mutating them after create re-keys the row, which is a
    "delete + re-create" operation the UI surfaces separately. The
    spec §05 "User work role" only pins ``ended_on`` + ``pay_rule_id``
    as mutable fields.
    """

    model_config = ConfigDict(extra="forbid")

    ended_on: date | None = Field(default=None)
    pay_rule_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)


@dataclass(frozen=True, slots=True)
class UserWorkRoleView:
    """Immutable read projection of a ``user_work_role`` row."""

    id: str
    user_id: str
    workspace_id: str
    work_role_id: str
    started_on: date
    ended_on: date | None
    pay_rule_id: str | None
    created_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: UserWorkRole) -> UserWorkRoleView:
    """Project a SQLAlchemy row into :class:`UserWorkRoleView`."""
    return UserWorkRoleView(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        work_role_id=row.work_role_id,
        started_on=row.started_on,
        ended_on=row.ended_on,
        pay_rule_id=row.pay_rule_id,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_work_role_id: str,
    include_deleted: bool = False,
) -> UserWorkRole:
    """Return the row or raise :class:`UserWorkRoleNotFound`."""
    stmt = select(UserWorkRole).where(
        UserWorkRole.id == user_work_role_id,
        UserWorkRole.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(UserWorkRole.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise UserWorkRoleNotFound(user_work_role_id)
    return row


def _assert_user_member(
    session: Session, ctx: WorkspaceContext, *, user_id: str
) -> None:
    """Raise if ``user_id`` is not a member of the caller's workspace.

    §05 invariant: a user cannot hold a work-role in a workspace they
    don't belong to. The check looks up the ``user_workspace``
    junction directly so the failure mode is a clean 422 rather than
    an FK error at flush time.
    """
    row = session.get(UserWorkspace, (user_id, ctx.workspace_id))
    if row is None:
        raise UserWorkRoleInvariantViolated(
            f"user {user_id!r} is not a member of this workspace"
        )


def _assert_work_role_belongs(
    session: Session,
    ctx: WorkspaceContext,
    *,
    work_role_id: str,
) -> None:
    """Raise if ``work_role_id`` belongs to a different workspace or is deleted.

    §05 "User work role" invariant: the referenced work-role must
    live in the same workspace. The tenant filter on ``work_role``
    already narrows to the caller's workspace, so a foreign-workspace
    id simply fails to resolve — but the explicit error keeps the
    surface consistent with the user-membership check above.
    """
    row = session.scalar(
        select(WorkRole).where(
            WorkRole.id == work_role_id,
            WorkRole.workspace_id == ctx.workspace_id,
            WorkRole.deleted_at.is_(None),
        )
    )
    if row is None:
        raise UserWorkRoleInvariantViolated(
            f"work_role {work_role_id!r} does not exist in this workspace"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_user_work_roles(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    limit: int,
    after_id: str | None = None,
    include_deleted: bool = False,
) -> Sequence[UserWorkRoleView]:
    """Return up to ``limit + 1`` rows for ``(user_id, ctx.workspace_id)``.

    The service returns ``limit + 1`` so the router's
    :func:`~app.api.pagination.paginate` helper can compute
    ``has_more`` without a second query. Rows ordered by ``id ASC``
    (ULID → time-ordered) so the forward cursor is deterministic.
    """
    stmt = select(UserWorkRole).where(
        UserWorkRole.user_id == user_id,
        UserWorkRole.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(UserWorkRole.deleted_at.is_(None))
    if after_id is not None:
        stmt = stmt.where(UserWorkRole.id > after_id)
    stmt = stmt.order_by(UserWorkRole.id.asc()).limit(limit + 1)
    rows = session.scalars(stmt).all()
    return [_row_to_view(r) for r in rows]


def get_user_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_work_role_id: str,
) -> UserWorkRoleView:
    """Return a single :class:`UserWorkRoleView` or raise on miss."""
    return _row_to_view(_load_row(session, ctx, user_work_role_id=user_work_role_id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_user_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: UserWorkRoleCreate,
    clock: Clock | None = None,
) -> UserWorkRoleView:
    """Insert a new user_work_role row.

    Runs the §05 "User work role" invariants (user + work_role
    membership) before attempting the flush. An IntegrityError on
    the ``(user, workspace, role, started_on)`` unique is collapsed
    into :class:`UserWorkRoleInvariantViolated`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _assert_user_member(session, ctx, user_id=body.user_id)
    _assert_work_role_belongs(session, ctx, work_role_id=body.work_role_id)

    row_id = new_ulid(clock=clock)
    row = UserWorkRole(
        id=row_id,
        user_id=body.user_id,
        workspace_id=ctx.workspace_id,
        work_role_id=body.work_role_id,
        started_on=body.started_on,
        ended_on=body.ended_on,
        pay_rule_id=body.pay_rule_id,
        created_at=now,
        deleted_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise UserWorkRoleInvariantViolated(
            f"user_work_role for user={body.user_id!r} "
            f"work_role={body.work_role_id!r} started_on={body.started_on!r} "
            "already exists"
        ) from exc

    write_audit(
        session,
        ctx,
        entity_kind="user_work_role",
        entity_id=row_id,
        action="user_work_role.created",
        diff={
            "user_id": body.user_id,
            "work_role_id": body.work_role_id,
            "started_on": body.started_on.isoformat(),
            "ended_on": body.ended_on.isoformat() if body.ended_on else None,
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)


def update_user_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_work_role_id: str,
    body: UserWorkRoleUpdate,
    clock: Clock | None = None,
) -> UserWorkRoleView:
    """Partial update of ``ended_on`` + ``pay_rule_id``.

    Only fields in :attr:`body.model_fields_set` are touched. A
    zero-delta call (every sent field matches the current value)
    skips the audit write — matches the employees-service convention.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    _ = resolved_clock.now()  # reserved for future updated_at column

    row = _load_row(session, ctx, user_work_role_id=user_work_role_id)

    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if "ended_on" in sent:
        # Reject ended_on < started_on here as the DTO does not know
        # the row's ``started_on``.
        if body.ended_on is not None and body.ended_on < row.started_on:
            raise UserWorkRoleInvariantViolated(
                "ended_on must be on or after started_on"
            )
        if body.ended_on != row.ended_on:
            before["ended_on"] = row.ended_on.isoformat() if row.ended_on else None
            after["ended_on"] = body.ended_on.isoformat() if body.ended_on else None
            row.ended_on = body.ended_on

    if "pay_rule_id" in sent and body.pay_rule_id != row.pay_rule_id:
        before["pay_rule_id"] = row.pay_rule_id
        after["pay_rule_id"] = body.pay_rule_id
        row.pay_rule_id = body.pay_rule_id

    if not after:
        return _row_to_view(row)

    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="user_work_role",
        entity_id=row.id,
        action="user_work_role.updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def delete_user_work_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_work_role_id: str,
    clock: Clock | None = None,
) -> UserWorkRoleView:
    """Soft-delete a user_work_role row — idempotent.

    Stamps ``deleted_at`` + ``ended_on`` (when ``ended_on`` is still
    null). A repeated call on an already-deleted row is a no-op but
    still raises :class:`UserWorkRoleNotFound` — the row is invisible
    to the default tenancy-scoped lookup. Callers that need to
    distinguish "already deleted" from "never existed" must pass
    ``include_deleted=True`` through a dedicated path (not exposed
    on the HTTP surface; the spec §12 ``DELETE`` returns 204 for
    both).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    today = now.date()

    row = _load_row(session, ctx, user_work_role_id=user_work_role_id)

    row.deleted_at = now
    # Don't clobber an explicit prior ``ended_on`` — the operator may
    # have recorded a historical last-day that we want to preserve.
    if row.ended_on is None:
        row.ended_on = today
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="user_work_role",
        entity_id=row.id,
        action="user_work_role.deleted",
        diff={
            "user_id": row.user_id,
            "work_role_id": row.work_role_id,
            "ended_on": row.ended_on.isoformat(),
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)
