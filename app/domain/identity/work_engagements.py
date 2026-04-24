"""Work-engagement CRUD domain service (§02 ``work_engagement``, §22).

The :class:`~app.adapters.db.workspace.models.WorkEngagement` row is
the per-(user, workspace) employment relationship that carries the
pay pipeline. A sibling module,
:mod:`app.services.employees.service`, already owns the
*user-centric* archive + reinstate flow exposed via ``POST
/users/{id}/archive`` and ``POST /users/{id}/reinstate`` — that path
archives the active engagement **plus** every active user_work_role
in one sweep.

This module is the **engagement-keyed** surface: list engagements,
read one, PATCH mutable fields, and the engagement-scoped archive /
reinstate that the spec §12 ``/work_engagements`` endpoints hit. The
archive / reinstate paths here target a specific engagement by id
(manager explicitly archives one row of a user's engagement history,
leaving the user's other engagements and user_work_role rows alone).
The engagement-centric path is a narrower tool than the user-centric
one; callers who want the sweep still go through
:mod:`app.services.employees.service`.

Public surface:

* **DTOs** — :class:`WorkEngagementUpdate`, :class:`WorkEngagementView`.
  No ``Create`` DTO: engagements are seeded on invite-accept
  (:func:`app.services.employees.service.seed_pending_work_engagement`)
  and upgraded over time; spec §12 does not expose a
  ``POST /work_engagements`` endpoint.
* **Service functions** — :func:`list_work_engagements` (cursor-
  paginated), :func:`get_work_engagement`,
  :func:`update_work_engagement`,
  :func:`archive_work_engagement`, :func:`reinstate_work_engagement`.
* **Errors** — :class:`WorkEngagementNotFound`,
  :class:`WorkEngagementInvariantViolated`.

**Authorisation.** The HTTP router gates mutations with
``work_roles.manage`` on workspace scope (§05 action catalog —
default-allow ``owners, managers``). Reads use ``scope.view``.

**Tenancy.** The ORM tenant filter narrows every SELECT on
``work_engagement``; each function re-asserts the predicate
explicitly.

**Transaction boundary.** The service never commits; the caller's
UoW owns transaction boundaries. Every mutation writes one
:mod:`app.audit` row in the same transaction.

**CHECK: supplier pairing.** §02 "work_engagement" binds
``supplier_org_id`` to ``engagement_kind = 'agency_supplied'`` (both
directions). :func:`update_work_engagement` re-asserts the pair at
the DTO boundary so the DB CHECK only fires as a last-resort net.

See ``docs/specs/02-domain-model.md`` §"work_engagement",
``docs/specs/05-employees-and-roles.md`` §"Work engagement",
``docs/specs/22-clients-and-vendors.md`` §"Engagement kinds".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import WorkEngagement
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "EngagementKind",
    "WorkEngagementInvariantViolated",
    "WorkEngagementNotFound",
    "WorkEngagementUpdate",
    "WorkEngagementView",
    "archive_work_engagement",
    "get_work_engagement",
    "list_work_engagements",
    "reinstate_work_engagement",
    "update_work_engagement",
]


EngagementKind = Literal["payroll", "contractor", "agency_supplied"]


_MAX_ID_LEN = 64
_MAX_NOTES_LEN = 20_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkEngagementNotFound(LookupError):
    """The engagement id is unknown / in another workspace.

    404-equivalent; collapses foreign-workspace + deleted + never
    existed (§01 "tenant surface is not enumerable").
    """


class WorkEngagementInvariantViolated(ValueError):
    """PATCH body violates a §02 "work_engagement" invariant.

    422-equivalent. Fires on:

    * ``engagement_kind = 'agency_supplied'`` with no
      ``supplier_org_id`` — or the converse (a non-agency engagement
      with a supplier id).
    * An attempt to change ``user_id`` / ``workspace_id`` / ``id`` /
      ``started_on`` through the update path (those fields are
      frozen; rebuild via a new row).
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class WorkEngagementUpdate(BaseModel):
    """Partial update body for :func:`update_work_engagement`.

    Explicit-sparse. Every field optional; Pydantic v2's
    ``model_fields_set`` distinguishes "omitted" from "explicitly set
    to None". The frozen columns (``id`` / ``user_id`` /
    ``workspace_id`` / ``started_on``) are deliberately absent from
    the shape — the spec treats them as identity keys, and mutation
    requires creating a new row + archiving the old one.
    """

    model_config = ConfigDict(extra="forbid")

    engagement_kind: EngagementKind | None = None
    supplier_org_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    pay_destination_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    reimbursement_destination_id: str | None = Field(
        default=None, max_length=_MAX_ID_LEN
    )
    notes_md: str | None = Field(default=None, max_length=_MAX_NOTES_LEN)

    @model_validator(mode="after")
    def _reject_notes_null(self) -> WorkEngagementUpdate:
        """Reject explicit ``notes_md=None`` — column is NOT NULL.

        Matches the :class:`WorkEngagement` ORM contract. Other
        columns in this DTO are nullable so their ``None`` is a
        legitimate "clear" signal. The supplier-pairing check is
        applied at the service boundary because it depends on the
        row's existing ``engagement_kind`` when only one side is
        sent.
        """
        sent = self.model_fields_set
        if "notes_md" in sent and self.notes_md is None:
            raise ValueError("notes_md cannot be cleared; it is NOT NULL")
        return self


@dataclass(frozen=True, slots=True)
class WorkEngagementView:
    """Immutable read projection of a ``work_engagement`` row."""

    id: str
    user_id: str
    workspace_id: str
    engagement_kind: str
    supplier_org_id: str | None
    pay_destination_id: str | None
    reimbursement_destination_id: str | None
    started_on: date
    archived_on: date | None
    notes_md: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: WorkEngagement) -> WorkEngagementView:
    """Project a SQLAlchemy row into :class:`WorkEngagementView`."""
    return WorkEngagementView(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        engagement_kind=row.engagement_kind,
        supplier_org_id=row.supplier_org_id,
        pay_destination_id=row.pay_destination_id,
        reimbursement_destination_id=row.reimbursement_destination_id,
        started_on=row.started_on,
        archived_on=row.archived_on,
        notes_md=row.notes_md,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    engagement_id: str,
) -> WorkEngagement:
    """Return the engagement row or raise :class:`WorkEngagementNotFound`.

    Does NOT filter on ``archived_on`` — the archive / reinstate
    endpoints need to see archived rows so they can be re-activated
    or no-op'd idempotently.
    """
    stmt = select(WorkEngagement).where(
        WorkEngagement.id == engagement_id,
        WorkEngagement.workspace_id == ctx.workspace_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise WorkEngagementNotFound(engagement_id)
    return row


def _validate_supplier_pairing(
    *, engagement_kind: str, supplier_org_id: str | None
) -> None:
    """Enforce the §02 "work_engagement" supplier/kind biconditional."""
    if engagement_kind == "agency_supplied" and supplier_org_id is None:
        raise WorkEngagementInvariantViolated(
            "engagement_kind='agency_supplied' requires supplier_org_id"
        )
    if engagement_kind != "agency_supplied" and supplier_org_id is not None:
        raise WorkEngagementInvariantViolated(
            f"engagement_kind={engagement_kind!r} must not carry supplier_org_id"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_work_engagements(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    after_id: str | None = None,
    user_id: str | None = None,
    include_archived: bool = True,
) -> Sequence[WorkEngagementView]:
    """Return up to ``limit + 1`` engagement views for the caller's workspace.

    ``user_id`` narrows to a specific worker. ``include_archived`` is
    True by default so the manager's roster view sees historical
    rows alongside the active one; the ``/work_engagements?active=true``
    query param on the HTTP layer flips it off.

    Rows ordered by ``id ASC`` (ULID → time-ordered) so the forward
    cursor is deterministic.
    """
    stmt = select(WorkEngagement).where(WorkEngagement.workspace_id == ctx.workspace_id)
    if user_id is not None:
        stmt = stmt.where(WorkEngagement.user_id == user_id)
    if not include_archived:
        stmt = stmt.where(WorkEngagement.archived_on.is_(None))
    if after_id is not None:
        stmt = stmt.where(WorkEngagement.id > after_id)
    stmt = stmt.order_by(WorkEngagement.id.asc()).limit(limit + 1)
    rows = session.scalars(stmt).all()
    return [_row_to_view(r) for r in rows]


def get_work_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    engagement_id: str,
) -> WorkEngagementView:
    """Return one engagement view or raise :class:`WorkEngagementNotFound`."""
    return _row_to_view(_load_row(session, ctx, engagement_id=engagement_id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def update_work_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    engagement_id: str,
    body: WorkEngagementUpdate,
    clock: Clock | None = None,
) -> WorkEngagementView:
    """Partial update of an engagement's mutable fields.

    Only fields in :attr:`body.model_fields_set` are applied. A
    zero-change write returns the view without an audit row. The
    supplier-pairing invariant is checked against the *resulting*
    state so a single PATCH that flips ``engagement_kind`` +
    ``supplier_org_id`` in the same payload is accepted.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    row = _load_row(session, ctx, engagement_id=engagement_id)

    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    # Compute the resulting state so the supplier-pairing check fires
    # against the merged view rather than the row's pre-patch value.
    new_kind = (
        body.engagement_kind if "engagement_kind" in sent else row.engagement_kind
    )
    new_supplier = (
        body.supplier_org_id if "supplier_org_id" in sent else row.supplier_org_id
    )
    assert new_kind is not None  # DTO forbids None on this column
    _validate_supplier_pairing(engagement_kind=new_kind, supplier_org_id=new_supplier)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if "engagement_kind" in sent and body.engagement_kind != row.engagement_kind:
        before["engagement_kind"] = row.engagement_kind
        after["engagement_kind"] = body.engagement_kind
        row.engagement_kind = body.engagement_kind  # type: ignore[assignment]

    if "supplier_org_id" in sent and body.supplier_org_id != row.supplier_org_id:
        before["supplier_org_id"] = row.supplier_org_id
        after["supplier_org_id"] = body.supplier_org_id
        row.supplier_org_id = body.supplier_org_id

    if (
        "pay_destination_id" in sent
        and body.pay_destination_id != row.pay_destination_id
    ):
        before["pay_destination_id"] = row.pay_destination_id
        after["pay_destination_id"] = body.pay_destination_id
        row.pay_destination_id = body.pay_destination_id

    if (
        "reimbursement_destination_id" in sent
        and body.reimbursement_destination_id != row.reimbursement_destination_id
    ):
        before["reimbursement_destination_id"] = row.reimbursement_destination_id
        after["reimbursement_destination_id"] = body.reimbursement_destination_id
        row.reimbursement_destination_id = body.reimbursement_destination_id

    if "notes_md" in sent and body.notes_md != row.notes_md:
        assert body.notes_md is not None  # DTO guards the None case
        before["notes_md"] = row.notes_md
        after["notes_md"] = body.notes_md
        row.notes_md = body.notes_md

    if not after:
        return _row_to_view(row)

    row.updated_at = now
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="work_engagement",
        entity_id=row.id,
        action="work_engagement.updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def archive_work_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    engagement_id: str,
    clock: Clock | None = None,
) -> WorkEngagementView:
    """Stamp ``archived_on`` on a single engagement — idempotent.

    Differs from :func:`app.services.employees.service.archive_employee`
    in scope: this path targets one engagement row by id and does
    NOT sweep the user's user_work_role rows. The user-centric
    archive in the employees service is the right tool for full
    off-boarding; this surface exists so a manager can archive a
    single engagement (e.g. end a payroll pipeline without touching
    the user's workspace membership).

    Idempotent: a call on an already-archived row is a no-op on DB
    state but still writes an audit row so the trail is linear.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    today: date = now.date()

    row = _load_row(session, ctx, engagement_id=engagement_id)

    was_active = row.archived_on is None
    if was_active:
        row.archived_on = today
        row.updated_at = now
        session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="work_engagement",
        entity_id=row.id,
        action="work_engagement.archived",
        diff={
            "user_id": row.user_id,
            "was_active": was_active,
            "archived_on": row.archived_on.isoformat()
            if row.archived_on is not None
            else None,
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)


def reinstate_work_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    engagement_id: str,
    clock: Clock | None = None,
) -> WorkEngagementView:
    """Clear ``archived_on`` on a single engagement — idempotent.

    Mirror of :func:`archive_work_engagement`. A call on an already-
    active row is a no-op on DB state but writes an audit row so the
    linear trail survives the replay.

    **Partial UNIQUE.** The ``(user_id, workspace_id) WHERE
    archived_on IS NULL`` partial unique index enforces "at most one
    active engagement per user per workspace". If the user already
    has a different active engagement in this workspace, we refuse
    the reinstate here with :class:`WorkEngagementInvariantViolated`
    (HTTP 422) — the UI / API caller must archive the other row
    first. The pre-flush SELECT narrows the race window against a
    parallel reinstate; the flush-time :class:`IntegrityError` is
    caught as defence-in-depth and collapsed into the same typed
    error so the HTTP surface is consistent across single-writer
    and race.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    row = _load_row(session, ctx, engagement_id=engagement_id)

    was_archived = row.archived_on is not None
    if was_archived:
        # Guard the partial UNIQUE before attempting the flush. The
        # predicate mirrors the index: another active engagement for
        # ``(user_id, workspace_id)`` that is NOT this row blocks the
        # reinstate.
        conflicting = session.scalar(
            select(WorkEngagement).where(
                WorkEngagement.user_id == row.user_id,
                WorkEngagement.workspace_id == ctx.workspace_id,
                WorkEngagement.archived_on.is_(None),
                WorkEngagement.id != row.id,
            )
        )
        if conflicting is not None:
            raise WorkEngagementInvariantViolated(
                f"user {row.user_id!r} already has an active engagement "
                f"in this workspace ({conflicting.id!r}); archive it before "
                "reinstating this one"
            )
        row.archived_on = None
        row.updated_at = now
        try:
            session.flush()
        except IntegrityError as exc:
            # Race collapse — a parallel reinstate landed first after
            # our pre-flight SELECT. Surface the same typed error so
            # the HTTP response is predictable.
            session.rollback()
            raise WorkEngagementInvariantViolated(
                f"user {row.user_id!r} already has an active engagement "
                "in this workspace; archive it before reinstating this one"
            ) from exc

    write_audit(
        session,
        ctx,
        entity_kind="work_engagement",
        entity_id=row.id,
        action="work_engagement.reinstated",
        diff={
            "user_id": row.user_id,
            "was_archived": was_archived,
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)
