"""Employees domain service — workspace-scoped CRUD for worker profiles.

Owns four operations on a user *as seen inside a single workspace*:

* :func:`get_employee` — read a user's profile projection scoped to
  the caller's workspace (workspace-scoped membership required;
  cross-workspace lookups collapse to 404 per §01 "tenant surface is
  not enumerable").
* :func:`update_profile` — partial update of the identity-level
  profile fields that matter for employees. Callers who target
  themselves pass through without an authz check (identity-scoped
  self-edit); callers who target someone else must hold
  ``users.edit_profile_other`` (default ``owners``, ``managers`` per
  the action catalog).
* :func:`archive_employee` — soft-archive the user's
  :class:`~app.adapters.db.workspace.models.WorkEngagement` row for
  this workspace AND every active
  :class:`~app.adapters.db.workspace.models.UserWorkRole` row in the
  same workspace. Idempotent — re-archiving a row that is already
  archived is a no-op that still writes an audit entry so the trail
  remains linear.
* :func:`reinstate_employee` — reverse archive. Clears
  ``WorkEngagement.archived_on`` and ``UserWorkRole.deleted_at`` for
  the workspace. Idempotent. **Does NOT clear** ``users.archived_at``
  in v1 — cross-workspace reinstate is deferred to a follow-up
  (``cd-dv2-note`` in the docstring below). The default behaviour is
  workspace-local.
* :func:`seed_pending_work_engagement` — called from the invite
  accept-side path (:func:`app.domain.identity.membership._activate_invite`)
  to insert a minimal pending :class:`WorkEngagement` row at the
  moment the invitee completes their passkey challenge. Nothing
  workspace-scoped is seeded until accept time (§03 "Additional
  users (invite → click-to-accept)").

**Tenancy.** Every read / write passes through the ORM tenant
filter on the registered workspace-scoped tables
(``work_engagement``, ``user_work_role``). Each function also
re-asserts the ``workspace_id`` predicate as defence-in-depth,
matching the convention used in :mod:`app.domain.places.property_service`
and :mod:`app.domain.identity.role_grants`.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation
writes one :mod:`app.audit` row in the same transaction.

**Audit.** Every mutation emits one ``employee.*`` audit row with
PII-safe payloads. Email + display_name go through the same
redaction seam as the rest of :mod:`app.audit` so a profile update
that lands an email value in a diff cannot survive into on-disk
logs.

**Cross-workspace reinstate (deferred — cd-pb8p).** §05 "Archive /
reinstate" describes a deployment-wide reinstate that also clears
``users.archived_at``. The ORM :class:`User` model does not carry
an ``archived_at`` column yet (Phase 1 ships without it) and the
cross-workspace active-engagement scan needs a deployment-level
owner check that does not exist in the action catalog. The v1
implementation here handles the workspace-local path only; the
cross-workspace branch is tracked as **cd-pb8p** (filed alongside
cd-dv2). Until that lands, :func:`reinstate_employee` performs a
workspace-local reinstate only — it never touches the identity-
level ``users`` row.

**Reinstate sweep overreach (follow-up — cd-9vi3).** v1
:func:`reinstate_employee` clears ``deleted_at`` on every archived
:class:`UserWorkRole` in the (user, workspace) pair rather than
only the rows the paired archive marked. A role ended manually
before the archive will come back on reinstate. Accepted for MVP
scope; tightening the sweep to the archive-time window is tracked
as cd-9vi3.

See ``docs/specs/05-employees-and-roles.md`` §"User (as worker)",
§"Work engagement", §"Archive / reinstate",
``docs/specs/02-domain-model.md`` §"users", §"user_workspace",
§"work_engagement", §"role_grants", and
``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
)
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "EmployeeNotFound",
    "EmployeeProfileUpdate",
    "EmployeeView",
    "ProfileFieldForbidden",
    "archive_employee",
    "get_employee",
    "reinstate_employee",
    "seed_pending_work_engagement",
    "update_profile",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmployeeNotFound(LookupError):
    """The target user is not visible as an employee in this workspace.

    404-equivalent. Raised when ``user_id`` is unknown, or when the
    user exists but holds no :class:`UserWorkspace` membership row in
    the caller's workspace — the cross-tenant collapse to "not found"
    is deliberate (§01 "tenant surface is not enumerable").
    """


class ProfileFieldForbidden(PermissionError):
    """Caller tried to touch a field they may not edit.

    403-equivalent. Fires when a non-self caller attempts to update a
    user they do not hold ``users.edit_profile_other`` on. The router
    maps this to :class:`~app.domain.errors.Forbidden`.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps lifted verbatim from :mod:`app.adapters.db.identity.models` and
# the existing invite body shape so API callers never straddle a
# mismatched limit between the two surfaces.
_MAX_DISPLAY_NAME_LEN = 160
_MAX_LOCALE_LEN = 35
_MAX_TIMEZONE_LEN = 64


class EmployeeProfileUpdate(BaseModel):
    """Partial update body for :func:`update_profile`.

    Every field is optional; an omitted field keeps its current value.
    A field set to ``None`` explicitly is treated as "clear it" (for
    nullable columns only — ``display_name`` is NOT NULL and rejects
    ``None`` at the DTO boundary via :meth:`_reject_display_name_null`).

    The shape is intentionally narrow: §02 ``users`` lists richer
    columns (``full_legal_name``, ``phone_e164``, ``emergency_contact``,
    ``notes_md``, ``agent_approval_mode``, ``preferred_locale``,
    ``languages``, ``avatar_file_id``) but the ORM model today only
    carries ``display_name`` / ``locale`` / ``timezone`` plus the
    avatar-hash column (avatar writes live on ``/api/v1/me/avatar`` —
    cd-6vq5, out of scope here). Later tasks that widen the ORM must
    extend this DTO in lockstep.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(
        default=None, min_length=1, max_length=_MAX_DISPLAY_NAME_LEN
    )
    locale: str | None = Field(default=None, max_length=_MAX_LOCALE_LEN)
    timezone: str | None = Field(default=None, max_length=_MAX_TIMEZONE_LEN)

    @model_validator(mode="after")
    def _reject_display_name_null(self) -> EmployeeProfileUpdate:
        """Reject an explicit ``display_name=None`` at the DTO boundary.

        Pydantic's ``min_length`` constraint only fires when the value
        is a string, so ``display_name=None`` would otherwise slip
        through and hit the :class:`User` column's NOT NULL contract
        as a 500. Raising here surfaces the mistake as a 422 validation
        error alongside the rest of the field-shape violations.
        """
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be cleared; it is NOT NULL")
        return self


@dataclass(frozen=True, slots=True)
class EmployeeView:
    """Immutable read projection of a user as seen inside a workspace.

    Carries the identity-level fields plus a boolean ``is_archived``
    derived from whether the user holds any active
    :class:`WorkEngagement` in this workspace. The richer engagement
    columns (kind, started_on, supplier_org_id, …) ride on
    :class:`WorkEngagement` directly; this projection is deliberately
    minimal until the engagements service (future) lands.
    """

    id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    avatar_blob_hash: str | None
    engagement_archived_on: date | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or SystemClock."""
    return (clock if clock is not None else SystemClock()).now()


def _assert_membership(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> UserWorkspace:
    """Return the caller-scoped membership row or raise.

    Workspace-scoped membership is the authority check for "is this
    user visible as an employee here?". No row → 404 (not 403), per
    §01. The ORM tenant filter auto-constrains the lookup to
    ``ctx.workspace_id``; we re-assert the predicate explicitly as
    defence-in-depth against a misconfigured context.
    """
    row = session.get(UserWorkspace, (user_id, ctx.workspace_id))
    if row is None or row.workspace_id != ctx.workspace_id:
        raise EmployeeNotFound(user_id)
    return row


def _load_user(session: Session, *, user_id: str) -> User:
    """Load a :class:`User` row by id under :func:`tenant_agnostic`.

    ``user`` is identity-scoped, not workspace-scoped. The caller
    must have already verified workspace membership via
    :func:`_assert_membership` before reaching this helper.
    """
    with tenant_agnostic():
        row = session.get(User, user_id)
    if row is None:
        raise EmployeeNotFound(user_id)
    return row


def _load_active_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> WorkEngagement | None:
    """Return the user's active engagement for this workspace, if any.

    "Active" = ``archived_on IS NULL``. The partial UNIQUE index on
    ``(user_id, workspace_id) WHERE archived_on IS NULL`` (§02
    "work_engagement") guarantees at most one row matches.
    """
    stmt = select(WorkEngagement).where(
        WorkEngagement.user_id == user_id,
        WorkEngagement.workspace_id == ctx.workspace_id,
        WorkEngagement.archived_on.is_(None),
    )
    return session.scalars(stmt).one_or_none()


def _load_any_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> WorkEngagement | None:
    """Return the most recent engagement row (active OR archived).

    Reinstate targets the most recent engagement — archived or not —
    and flips it back to active. The ordering by ``created_at``
    descending + ``id`` descending gives a deterministic pick when a
    user has stacked historical rows.
    """
    stmt = (
        select(WorkEngagement)
        .where(
            WorkEngagement.user_id == user_id,
            WorkEngagement.workspace_id == ctx.workspace_id,
        )
        .order_by(WorkEngagement.created_at.desc(), WorkEngagement.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).one_or_none()


def _load_user_work_roles(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    active_only: bool,
) -> list[UserWorkRole]:
    """Return every ``user_work_role`` row for the (user, workspace) pair.

    ``active_only`` narrows the result to rows whose ``deleted_at`` is
    NULL — the set the archive path targets. ``active_only=False``
    returns the full history for the reinstate path so the reverse
    sweep can clear the tombstone on every row the archive touched.
    """
    stmt = select(UserWorkRole).where(
        UserWorkRole.user_id == user_id,
        UserWorkRole.workspace_id == ctx.workspace_id,
    )
    if active_only:
        stmt = stmt.where(UserWorkRole.deleted_at.is_(None))
    return list(session.scalars(stmt).all())


def _row_to_view(
    user: User,
    *,
    engagement: WorkEngagement | None,
) -> EmployeeView:
    """Project a :class:`User` + optional engagement into an :class:`EmployeeView`."""
    return EmployeeView(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        locale=user.locale,
        timezone=user.timezone,
        avatar_blob_hash=user.avatar_blob_hash,
        engagement_archived_on=(
            engagement.archived_on if engagement is not None else None
        ),
        created_at=user.created_at,
    )


def _require_edit_other(
    session: Session,
    ctx: WorkspaceContext,
) -> None:
    """Enforce ``users.edit_profile_other`` on the caller's workspace.

    Wraps :func:`app.authz.require` + translates a caller-bug
    (unknown key / invalid scope) into a :class:`RuntimeError` so the
    router can surface it as a 500 instead of a 403. Matches the
    :func:`app.domain.time.shifts._require_capability` shape.
    """
    try:
        require(
            session,
            ctx,
            action_key="users.edit_profile_other",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'users.edit_profile_other': {exc!s}"
        ) from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_employee(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> EmployeeView:
    """Return the employee projection for ``user_id`` in the caller's workspace.

    Raises :class:`EmployeeNotFound` when the user is unknown to this
    workspace.
    """
    _assert_membership(session, ctx, user_id=user_id)
    user = _load_user(session, user_id=user_id)
    engagement = _load_active_engagement(session, ctx, user_id=user_id)
    return _row_to_view(user, engagement=engagement)


# ---------------------------------------------------------------------------
# Writes — profile update
# ---------------------------------------------------------------------------


def update_profile(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    body: EmployeeProfileUpdate,
    clock: Clock | None = None,
) -> EmployeeView:
    """Partial update of an employee's profile fields.

    Authorisation:

    * ``ctx.actor_id == user_id`` → self-edit. No capability check.
    * Otherwise → caller must hold ``users.edit_profile_other`` on the
      workspace. A missing capability raises
      :class:`~app.authz.PermissionDenied`; the router maps it to 403.

    Raises :class:`EmployeeNotFound` if the user is not a member of
    the caller's workspace. The membership check runs BEFORE the
    capability check so a cross-tenant probe still collapses to 404.

    One ``employee.profile_updated`` audit row per call, carrying a
    redacted before / after diff of the changed fields.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    _assert_membership(session, ctx, user_id=user_id)

    if ctx.actor_id != user_id:
        try:
            _require_edit_other(session, ctx)
        except PermissionDenied as exc:
            raise ProfileFieldForbidden(
                f"caller {ctx.actor_id!r} may not edit profile of {user_id!r}"
            ) from exc

    user = _load_user(session, user_id=user_id)

    # ``model_fields_set`` is the Pydantic-v2 truth of "which fields
    # did the caller actually send?" — we use it to distinguish
    # "explicitly set to None" (clear) from "omitted" (keep). For
    # ``display_name`` a ``None`` is rejected at the DTO layer (the
    # column is NOT NULL); the other two columns are nullable.
    sent = body.model_fields_set
    if not sent:
        # No-op update — still return the current view so the router
        # doesn't have to special-case an empty body. No audit row:
        # a zero-change write is not a forensic event.
        engagement = _load_active_engagement(session, ctx, user_id=user_id)
        return _row_to_view(user, engagement=engagement)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if "display_name" in sent:
        # ``display_name=None`` is rejected by
        # :meth:`EmployeeProfileUpdate._reject_display_name_null` at
        # the DTO boundary (422). The ``assert`` narrows the type for
        # mypy and guards against a caller that bypassed the DTO.
        assert body.display_name is not None, (
            "display_name null reached service layer — DTO guard bypassed"
        )
        if body.display_name != user.display_name:
            before["display_name"] = user.display_name
            after["display_name"] = body.display_name
            user.display_name = body.display_name

    if "locale" in sent and body.locale != user.locale:
        before["locale"] = user.locale
        after["locale"] = body.locale
        user.locale = body.locale

    if "timezone" in sent and body.timezone != user.timezone:
        before["timezone"] = user.timezone
        after["timezone"] = body.timezone
        user.timezone = body.timezone

    if not after:
        # Every sent field matched the current value — no actual change.
        engagement = _load_active_engagement(session, ctx, user_id=user_id)
        return _row_to_view(user, engagement=engagement)

    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="employee.profile_updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )

    engagement = _load_active_engagement(session, ctx, user_id=user_id)
    return _row_to_view(user, engagement=engagement)


# ---------------------------------------------------------------------------
# Writes — archive / reinstate
# ---------------------------------------------------------------------------


def archive_employee(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> EmployeeView:
    """Archive the user's engagement + every user_work_role in this workspace.

    §05 "Archive / reinstate" scope #2 ("Archive a work_engagement"):

    * Set ``WorkEngagement.archived_on = today`` on the active
      engagement (if any). The partial UNIQUE index guarantees at
      most one matches; archiving the same user twice is a no-op on
      the engagement side.
    * Soft-delete every active :class:`UserWorkRole` in this
      workspace by stamping ``deleted_at``.

    **Idempotent.** A repeated call with no live rows to touch is a
    no-op for the DB state, but still writes an audit entry so the
    forensic trail does not swallow the operator action.

    Returns the employee view so the router can echo the archived
    engagement timestamp back.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    today: date = now.date()

    _assert_membership(session, ctx, user_id=user_id)
    # Archive is a write on *other* users' workspace pipeline, so the
    # ``users.archive`` capability (not ``edit_profile_other``) gates
    # it. Matching §05 spec which lists archive among the capabilities
    # owners + managers hold by default.
    _require_archive(session, ctx)

    engagement = _load_active_engagement(session, ctx, user_id=user_id)
    engagement_was_active = engagement is not None
    if engagement is not None:
        engagement.archived_on = today
        engagement.updated_at = now

    active_roles = _load_user_work_roles(
        session, ctx, user_id=user_id, active_only=True
    )
    archived_role_ids: list[str] = [r.id for r in active_roles]
    if archived_role_ids:
        # Bulk UPDATE — ``deleted_at`` + ``ended_on`` stamp as one DML.
        # The partial-active predicate on the select matches exactly
        # the rows we update, so the statement is safe against races
        # within the caller's transaction.
        session.execute(
            update(UserWorkRole)
            .where(
                UserWorkRole.id.in_(archived_role_ids),
            )
            .values(deleted_at=now, ended_on=today)
            .execution_options(synchronize_session="fetch")
        )
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="employee.archived",
        diff={
            "user_id": user_id,
            "engagement_id": engagement.id if engagement is not None else None,
            "engagement_was_active": engagement_was_active,
            "archived_user_work_role_ids": archived_role_ids,
        },
        clock=resolved_clock,
    )

    user = _load_user(session, user_id=user_id)
    refreshed_engagement = _load_active_engagement(session, ctx, user_id=user_id)
    return _row_to_view(user, engagement=refreshed_engagement)


def reinstate_employee(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> EmployeeView:
    """Reverse archive for a user in the caller's workspace.

    §05 "Archive / reinstate" — reinstates the user's most recent
    :class:`WorkEngagement` (clearing ``archived_on``) AND every
    archived :class:`UserWorkRole` in this workspace (clearing
    ``deleted_at`` / ``ended_on``). **Does NOT** clear
    ``users.archived_at`` — see module docstring for the v1
    scope note.

    Idempotent. A repeated call on an already-active user writes an
    audit row with ``changed_rows = 0`` so the trail is linear.

    **v1 overreach (cd-pb8p):** The reinstate sweep clears
    ``deleted_at`` on *every* archived :class:`UserWorkRole` for the
    (user, workspace) pair — not only the rows the corresponding
    archive touched. Spec §05 describes per-row reinstatement; if an
    operator had manually ended a single role before the archive,
    this path brings it back. Accepted for the MVP scope: the
    archive/reinstate pair ships as a coarse toggle for off-boarding.
    Narrowing the sweep to the engagement's archive-time window is
    tracked as a follow-up.

    Returns the refreshed employee view.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _assert_membership(session, ctx, user_id=user_id)
    _require_archive(session, ctx)

    engagement = _load_any_engagement(session, ctx, user_id=user_id)
    engagement_was_archived = (
        engagement is not None and engagement.archived_on is not None
    )
    if engagement is not None and engagement.archived_on is not None:
        engagement.archived_on = None
        engagement.updated_at = now

    all_roles = _load_user_work_roles(session, ctx, user_id=user_id, active_only=False)
    reinstated_role_ids: list[str] = [
        r.id for r in all_roles if r.deleted_at is not None
    ]
    if reinstated_role_ids:
        session.execute(
            update(UserWorkRole)
            .where(UserWorkRole.id.in_(reinstated_role_ids))
            .values(deleted_at=None, ended_on=None)
            .execution_options(synchronize_session="fetch")
        )
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="employee.reinstated",
        diff={
            "user_id": user_id,
            "engagement_id": engagement.id if engagement is not None else None,
            "engagement_was_archived": engagement_was_archived,
            "reinstated_user_work_role_ids": reinstated_role_ids,
        },
        clock=resolved_clock,
    )

    user = _load_user(session, user_id=user_id)
    refreshed_engagement = _load_active_engagement(session, ctx, user_id=user_id)
    return _row_to_view(user, engagement=refreshed_engagement)


def _require_archive(
    session: Session,
    ctx: WorkspaceContext,
) -> None:
    """Enforce ``users.archive`` on the caller's workspace or raise.

    Same wrapper shape as :func:`_require_edit_other`.
    """
    try:
        require(
            session,
            ctx,
            action_key="users.archive",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'users.archive': {exc!s}"
        ) from exc


# ---------------------------------------------------------------------------
# Accept-time seed helper (called from membership._activate_invite)
# ---------------------------------------------------------------------------


def seed_pending_work_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    now: datetime,
    clock: Clock | None = None,
) -> WorkEngagement:
    """Insert a minimal pending :class:`WorkEngagement` at invite accept time.

    Spec §03 "Additional users" mandates that nothing workspace-scoped
    is materialised for the invitee until they complete their passkey
    challenge. :func:`app.domain.identity.membership._activate_invite`
    calls this helper *inside* the accept transaction so the
    engagement row lands alongside the ``role_grant`` +
    ``permission_group_member`` rows atomically.

    The engagement is created with the Phase 1 defaults:

    * ``engagement_kind = 'payroll'`` — the majority case for
      direct-employment workers (§22 "Engagement kinds"); the
      richer invite sub-payload that can override this is the work
      of cd-1hd0 / cd-4o61.
    * ``started_on = now.date()`` — the accept instant.
    * ``archived_on = NULL`` — the engagement is active from the
      moment the passkey challenge completes.
    * ``supplier_org_id = NULL`` — required iff
      ``engagement_kind = 'agency_supplied'`` (CHECK constraint).

    **Idempotency.** If an active engagement already exists for
    ``(user_id, workspace_id)``, the partial UNIQUE index would
    reject a duplicate. We look up first and return the existing
    row unchanged — safe for accept-replay scenarios where an
    invite's ``_activate_invite`` runs twice.

    Returns the engagement row (new or existing). The caller is
    responsible for ensuring a :class:`UserWorkspace` row exists for
    the ``(user_id, ctx.workspace_id)`` pair before invoking — the
    sole production caller (:func:`app.domain.identity.membership._activate_invite`)
    writes the junction row in the same transaction just upstream of
    this call.
    """
    existing = _load_active_engagement(session, ctx, user_id=user_id)
    if existing is not None:
        return existing

    resolved_clock = clock if clock is not None else SystemClock()
    engagement_id = new_ulid(clock=clock)
    row = WorkEngagement(
        id=engagement_id,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
        engagement_kind="payroll",
        supplier_org_id=None,
        pay_destination_id=None,
        reimbursement_destination_id=None,
        started_on=now.date(),
        archived_on=None,
        notes_md="",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="work_engagement",
        entity_id=engagement_id,
        action="work_engagement.seeded_on_accept",
        diff={
            "user_id": user_id,
            "engagement_kind": "payroll",
            "started_on": row.started_on.isoformat(),
        },
        clock=resolved_clock,
    )
    return row


# ---------------------------------------------------------------------------
# Utility — list iterable helper used by tests and potentially callers
# ---------------------------------------------------------------------------


def iter_active_engagements(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_ids: Iterable[str],
) -> dict[str, WorkEngagement]:
    """Return a mapping ``user_id -> active engagement`` for a user set.

    Helper for roster views that need to annotate a batch of users
    with their engagement state without issuing N queries. Workspace-
    scoped via both the ORM tenant filter and an explicit predicate.
    """
    ids = list(user_ids)
    if not ids:
        return {}
    stmt = select(WorkEngagement).where(
        WorkEngagement.user_id.in_(ids),
        WorkEngagement.workspace_id == ctx.workspace_id,
        WorkEngagement.archived_on.is_(None),
    )
    out: dict[str, WorkEngagement] = {}
    for row in session.scalars(stmt).all():
        out[row.user_id] = row
    return out
