"""SA-backed repositories implementing :mod:`app.domain.identity.availability_ports`.

The concrete classes here adapt SQLAlchemy ``Session`` work to the
Protocol surfaces the availability domain services consume (cd-r5j2):

* :class:`SqlAlchemyUserAvailabilityOverrideRepository` — wraps the
  ``user_availability_override`` table plus the ``user_weekly_availability``
  lookup the §06 hybrid-approval calculator needs. Consumed by
  :mod:`app.domain.identity.user_availability_overrides`.
* :class:`SqlAlchemyCapabilityChecker` — wraps :func:`app.authz.require`
  so the availability domain services don't transitively pull
  :mod:`app.adapters.db.authz.models` via :mod:`app.authz.membership`
  / :mod:`app.authz.owners` (the cd-7qxh stopgap rationale). Re-used
  by the future ``user_leaves`` seam (cd-2upg).

Reaches into :mod:`app.adapters.db.availability.models` (for the
override + weekly-pattern rows) and :mod:`app.authz` (for the
underlying :func:`require` enforcement). Adapter-to-adapter +
adapter-to-app.authz imports are allowed by the import-linter — only
``app.domain → app.adapters`` is forbidden.

The repo carries an open ``Session`` and never commits beyond what
the underlying statements require — the caller's UoW owns the
transaction boundary (§01 "Key runtime invariants" #3). Mutating
methods flush so a peer read in the same UoW (and the audit
writer's FK reference to ``entity_id``) sees the new row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date as _date_cls
from datetime import datetime, time
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserWeeklyAvailability,
)
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.domain.identity.availability_ports import (
    CapabilityChecker,
    SeamPermissionDenied,
    UserAvailabilityOverrideRepository,
    UserAvailabilityOverrideRow,
    UserWeeklyAvailabilityRow,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "SqlAlchemyCapabilityChecker",
    "SqlAlchemyUserAvailabilityOverrideRepository",
]


# ---------------------------------------------------------------------------
# Override repository
# ---------------------------------------------------------------------------


def _to_override_row(row: UserAvailabilityOverride) -> UserAvailabilityOverrideRow:
    """Project an ORM ``UserAvailabilityOverride`` into the seam-level row.

    Field-by-field copy — :class:`UserAvailabilityOverrideRow` is
    frozen so the domain never mutates the ORM-managed instance
    through a shared reference.
    """
    return UserAvailabilityOverrideRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        date=row.date,
        available=row.available,
        starts_local=row.starts_local,
        ends_local=row.ends_local,
        reason=row.reason,
        approval_required=row.approval_required,
        approved_at=row.approved_at,
        approved_by=row.approved_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _to_weekly_row(row: UserWeeklyAvailability) -> UserWeeklyAvailabilityRow:
    """Project an ORM ``UserWeeklyAvailability`` into the seam-level row."""
    return UserWeeklyAvailabilityRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        weekday=row.weekday,
        starts_local=row.starts_local,
        ends_local=row.ends_local,
        updated_at=row.updated_at,
    )


class SqlAlchemyUserAvailabilityOverrideRepository(UserAvailabilityOverrideRepository):
    """SA-backed concretion of :class:`UserAvailabilityOverrideRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    outside what the underlying statements require — the caller's
    UoW owns the transaction boundary (§01 "Key runtime invariants"
    #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        override_id: str,
        include_deleted: bool = False,
    ) -> UserAvailabilityOverrideRow | None:
        stmt = select(UserAvailabilityOverride).where(
            UserAvailabilityOverride.id == override_id,
            UserAvailabilityOverride.workspace_id == workspace_id,
        )
        if not include_deleted:
            stmt = stmt.where(UserAvailabilityOverride.deleted_at.is_(None))
        row = self._session.scalars(stmt).one_or_none()
        return _to_override_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        limit: int,
        after_id: str | None = None,
        user_id: str | None = None,
        status: Literal["approved", "pending"] | None = None,
        from_date: _date_cls | None = None,
        to_date: _date_cls | None = None,
    ) -> Sequence[UserAvailabilityOverrideRow]:
        stmt = select(UserAvailabilityOverride).where(
            UserAvailabilityOverride.workspace_id == workspace_id,
            UserAvailabilityOverride.deleted_at.is_(None),
        )
        if user_id is not None:
            stmt = stmt.where(UserAvailabilityOverride.user_id == user_id)
        if status == "approved":
            stmt = stmt.where(UserAvailabilityOverride.approved_at.is_not(None))
        elif status == "pending":
            stmt = stmt.where(UserAvailabilityOverride.approved_at.is_(None))
        if from_date is not None:
            stmt = stmt.where(UserAvailabilityOverride.date >= from_date)
        if to_date is not None:
            stmt = stmt.where(UserAvailabilityOverride.date <= to_date)
        if after_id is not None:
            stmt = stmt.where(UserAvailabilityOverride.id > after_id)
        stmt = stmt.order_by(UserAvailabilityOverride.id.asc()).limit(limit + 1)
        rows = self._session.scalars(stmt).all()
        return [_to_override_row(r) for r in rows]

    def find_weekly_pattern(
        self,
        *,
        workspace_id: str,
        user_id: str,
        weekday: int,
    ) -> UserWeeklyAvailabilityRow | None:
        row = self._session.scalars(
            select(UserWeeklyAvailability).where(
                UserWeeklyAvailability.workspace_id == workspace_id,
                UserWeeklyAvailability.user_id == user_id,
                UserWeeklyAvailability.weekday == weekday,
            )
        ).one_or_none()
        return _to_weekly_row(row) if row is not None else None

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        override_id: str,
        workspace_id: str,
        user_id: str,
        date: _date_cls,
        available: bool,
        starts_local: time | None,
        ends_local: time | None,
        reason: str | None,
        approval_required: bool,
        approved_at: datetime | None,
        approved_by: str | None,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        row = UserAvailabilityOverride(
            id=override_id,
            workspace_id=workspace_id,
            user_id=user_id,
            date=date,
            available=available,
            starts_local=starts_local,
            ends_local=ends_local,
            reason=reason,
            approval_required=approval_required,
            approved_at=approved_at,
            approved_by=approved_by,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_override_row(row)

    def update_fields(
        self,
        *,
        workspace_id: str,
        override_id: str,
        available: bool | None = None,
        starts_local: time | None = None,
        ends_local: time | None = None,
        reason: str | None = None,
        clear_starts_local: bool = False,
        clear_ends_local: bool = False,
        clear_reason: bool = False,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        # Caller has just confirmed the row exists via :meth:`get`;
        # use the same workspace-scoped SELECT shape so the caller's
        # UoW reuses the identity-map entry rather than spawning a
        # second instance for the same primary key. Tombstoned rows
        # are excluded — the caller's state-machine guard already
        # rejects approved / deleted rows from this path.
        row = self._session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.id == override_id,
                UserAvailabilityOverride.workspace_id == workspace_id,
                UserAvailabilityOverride.deleted_at.is_(None),
            )
        ).one()

        # Caller has already filtered the deltas (zero-delta calls
        # never reach us); apply each sent field. ``clear_*`` flags
        # distinguish "send JSON null to clear" from "field omitted"
        # for nullable columns. ``available`` is non-nullable; a
        # ``None`` argument is treated as "unchanged".
        changed = False
        if available is not None and available != row.available:
            row.available = available
            changed = True
        if clear_starts_local and row.starts_local is not None:
            row.starts_local = None
            changed = True
        elif starts_local is not None and starts_local != row.starts_local:
            row.starts_local = starts_local
            changed = True
        if clear_ends_local and row.ends_local is not None:
            row.ends_local = None
            changed = True
        elif ends_local is not None and ends_local != row.ends_local:
            row.ends_local = ends_local
            changed = True
        if clear_reason and row.reason is not None:
            row.reason = None
            changed = True
        elif reason is not None and reason != row.reason:
            row.reason = reason
            changed = True

        if changed:
            row.updated_at = now
            self._session.flush()
        return _to_override_row(row)

    def stamp_approved(
        self,
        *,
        workspace_id: str,
        override_id: str,
        approved_by: str,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        row = self._session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.id == override_id,
                UserAvailabilityOverride.workspace_id == workspace_id,
                UserAvailabilityOverride.deleted_at.is_(None),
            )
        ).one()
        row.approved_at = now
        row.approved_by = approved_by
        row.updated_at = now
        self._session.flush()
        return _to_override_row(row)

    def soft_delete(
        self,
        *,
        workspace_id: str,
        override_id: str,
        reason: str | None = None,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        row = self._session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.id == override_id,
                UserAvailabilityOverride.workspace_id == workspace_id,
                UserAvailabilityOverride.deleted_at.is_(None),
            )
        ).one()
        row.deleted_at = now
        row.updated_at = now
        if reason is not None:
            # Only overwrite ``reason`` when the caller (reject path)
            # has already prepared the post-rejection text. The
            # canonical ``delete_override`` withdraw path passes
            # ``None`` so the worker's original explanation survives.
            row.reason = reason
        self._session.flush()
        return _to_override_row(row)


# ---------------------------------------------------------------------------
# CapabilityChecker
# ---------------------------------------------------------------------------


class SqlAlchemyCapabilityChecker(CapabilityChecker):
    """SA-backed concretion of :class:`CapabilityChecker`.

    Wraps :func:`app.authz.require` for a fixed ``(session, ctx)``
    pair so callers don't have to thread the workspace scope through
    every ``require()`` call. The transitive walk via
    :mod:`app.authz.membership` / :mod:`app.authz.owners` reaches
    :mod:`app.adapters.db.authz.models` here at the adapter layer
    where the import is allowed — keeping it out of the domain
    service which would otherwise pick up the dependency through a
    bare ``from app.authz import require``.

    Catalog-misconfiguration errors (:class:`UnknownActionKey` /
    :class:`InvalidScope`) propagate as :class:`RuntimeError` so the
    router surfaces 500, not 403 — they are server bugs, not denials.
    """

    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        self._session = session
        self._ctx = ctx

    def require(self, action_key: str) -> None:
        try:
            require(
                self._session,
                self._ctx,
                action_key=action_key,
                scope_kind="workspace",
                scope_id=self._ctx.workspace_id,
            )
        except PermissionDenied as exc:
            raise SeamPermissionDenied(str(exc)) from exc
        except (UnknownActionKey, InvalidScope) as exc:
            raise RuntimeError(
                f"authz catalog misconfigured for {action_key!r}: {exc!s}"
            ) from exc

    def has(self, action_key: str) -> bool:
        try:
            require(
                self._session,
                self._ctx,
                action_key=action_key,
                scope_kind="workspace",
                scope_id=self._ctx.workspace_id,
            )
        except PermissionDenied:
            return False
        except (UnknownActionKey, InvalidScope) as exc:
            raise RuntimeError(
                f"authz catalog misconfigured for {action_key!r}: {exc!s}"
            ) from exc
        return True
