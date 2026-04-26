"""SA-backed repositories implementing the expenses-context Protocol seams.

The concrete classes here adapt SQLAlchemy ``Session`` work to the
Protocol surfaces declared on
:mod:`app.domain.expenses.ports`:

* :class:`SqlAlchemyExpensesRepository` — wraps every read/write the
  three domain modules (claims / approval / autofill) need against
  :mod:`app.adapters.db.expenses.models` plus the cross-package reads
  for engagement membership-defence
  (:mod:`app.adapters.db.workspace.models`), per-user display-name
  resolution (:mod:`app.adapters.db.identity.models`), and post-flight
  LLM-usage rows (:mod:`app.adapters.db.llm.models`).

* :class:`SqlAlchemyCapabilityChecker` — wraps :func:`app.authz.require`
  for a fixed ``(session, ctx)`` pair so the domain modules don't
  transitively pull :mod:`app.adapters.db.authz.models`.

Reaches into multiple adapter packages directly. Adapter-to-adapter
imports are allowed by the import-linter — only ``app.domain →
app.adapters`` is forbidden.

The repos carry an open ``Session`` and never commit — the caller's
UoW owns the transaction boundary (§01 "Key runtime invariants" #3).
Claim + attachment mutating methods flush so the caller's next read
(and the audit writer's FK reference to ``entity_id``) sees the new
row. :meth:`SqlAlchemyExpensesRepository.insert_llm_usage` does NOT
flush — that row is never an audit target and the caller's outer UoW
commit covers it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.expenses.models import ExpenseAttachment, ExpenseClaim
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.workspace.models import WorkEngagement
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.domain.expenses.ports import (
    CapabilityChecker,
    ExpenseAttachmentRow,
    ExpenseClaimRow,
    ExpensesRepository,
    LlmUsageStatus,
    PendingClaimsCursor,
    SeamPermissionDenied,
    WorkEngagementRow,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "SqlAlchemyCapabilityChecker",
    "SqlAlchemyExpensesRepository",
]


# ---------------------------------------------------------------------------
# Row projections
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Tag a naive ``datetime`` as UTC (SQLite roundtrip strips tzinfo).

    The cross-backend invariant ("time is UTC at rest") lets us tag a
    naive value as UTC without guessing. The PG dialect retains tzinfo
    so the ``replace`` is a no-op there; this normalises the SQLite
    path to match.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _ensure_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)


def _to_engagement_row(row: WorkEngagement) -> WorkEngagementRow:
    return WorkEngagementRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
    )


def _to_attachment_row(row: ExpenseAttachment) -> ExpenseAttachmentRow:
    return ExpenseAttachmentRow(
        id=row.id,
        workspace_id=row.workspace_id,
        claim_id=row.claim_id,
        blob_hash=row.blob_hash,
        kind=row.kind,
        pages=row.pages,
        created_at=_ensure_utc(row.created_at),
    )


def _to_claim_row(row: ExpenseClaim) -> ExpenseClaimRow:
    """Project an ORM ``ExpenseClaim`` into the seam-level row.

    Field-by-field copy with ``_ensure_utc`` normalisation on every
    timestamp column so the domain compares against aware UTC values
    without re-implementing the SQLite tzinfo dance per call site.
    The ``llm_autofill_json`` payload is held as ``Any`` on the ORM
    side (SQLAlchemy JSON column convention); the seam narrows it to
    ``Mapping[str, Any] | None`` — domain consumers only read it as a
    dict, not as an arbitrary JSON value.
    """
    payload: Mapping[str, Any] | None
    if row.llm_autofill_json is None:
        payload = None
    elif isinstance(row.llm_autofill_json, Mapping):
        payload = row.llm_autofill_json
    else:
        # Defence-in-depth — a stale row that landed a non-dict JSON
        # value (legacy fixture, hand-edited data) would fail loud
        # rather than silently round-trip a list / string into the
        # domain's autofill branch.
        raise RuntimeError(
            f"expense_claim.llm_autofill_json on {row.id!r} is not a JSON object: "
            f"{type(row.llm_autofill_json).__name__}"
        )
    return ExpenseClaimRow(
        id=row.id,
        workspace_id=row.workspace_id,
        work_engagement_id=row.work_engagement_id,
        vendor=row.vendor,
        purchased_at=_ensure_utc(row.purchased_at),
        currency=row.currency,
        total_amount_cents=row.total_amount_cents,
        category=row.category,
        property_id=row.property_id,
        note_md=row.note_md,
        state=row.state,
        submitted_at=_ensure_utc_optional(row.submitted_at),
        decided_by=row.decided_by,
        decided_at=_ensure_utc_optional(row.decided_at),
        decision_note_md=row.decision_note_md,
        reimbursed_at=_ensure_utc_optional(row.reimbursed_at),
        reimbursed_via=row.reimbursed_via,
        reimbursed_by=row.reimbursed_by,
        llm_autofill_json=payload,
        autofill_confidence_overall=row.autofill_confidence_overall,
        created_at=_ensure_utc(row.created_at),
        deleted_at=_ensure_utc_optional(row.deleted_at),
    )


# ---------------------------------------------------------------------------
# ExpensesRepository concretion
# ---------------------------------------------------------------------------


# Set of column names :meth:`update_claim_fields` is allowed to write.
# Every other key in the supplied mapping raises :class:`KeyError` so a
# typo / drift surfaces loudly instead of silently dropping the column.
_ALLOWED_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        "vendor",
        "purchased_at",
        "currency",
        "total_amount_cents",
        "category",
        "property_id",
        "note_md",
        "work_engagement_id",
        "llm_autofill_json",
        "autofill_confidence_overall",
    }
)


class SqlAlchemyExpensesRepository(ExpensesRepository):
    """SA-backed concretion of :class:`ExpensesRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits —
    the caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3). Claim + attachment mutating methods flush so the
    caller's next read (and the audit writer's FK reference to
    ``entity_id``) sees the new row. :meth:`insert_llm_usage` does NOT
    flush — that row is never an audit target and the caller's outer
    UoW commit covers it.

    Every read pins the caller's ``workspace_id`` explicitly — defence-
    in-depth on top of the ORM tenant filter so a misconfigured filter
    fails loud, not silently.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Engagement reads ------------------------------------------------

    def get_engagement(
        self, *, workspace_id: str, engagement_id: str
    ) -> WorkEngagementRow | None:
        stmt = select(WorkEngagement).where(
            WorkEngagement.id == engagement_id,
            WorkEngagement.workspace_id == workspace_id,
        )
        row = self._session.scalars(stmt).one_or_none()
        if row is None:
            return None
        return _to_engagement_row(row)

    def get_engagement_user_ids(
        self, *, workspace_id: str, engagement_ids: Sequence[str]
    ) -> dict[str, str]:
        if not engagement_ids:
            return {}
        stmt = select(WorkEngagement.id, WorkEngagement.user_id).where(
            WorkEngagement.workspace_id == workspace_id,
            WorkEngagement.id.in_(engagement_ids),
        )
        return {eid: uid for eid, uid in self._session.execute(stmt).all()}

    # -- Claim CRUD ------------------------------------------------------

    def get_claim(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> ExpenseClaimRow | None:
        stmt = select(ExpenseClaim).where(
            ExpenseClaim.id == claim_id,
            ExpenseClaim.workspace_id == workspace_id,
        )
        if not include_deleted:
            stmt = stmt.where(ExpenseClaim.deleted_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        if row is None:
            return None
        return _to_claim_row(row)

    def list_claims_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        stmt = (
            select(ExpenseClaim)
            .join(
                WorkEngagement,
                (WorkEngagement.id == ExpenseClaim.work_engagement_id)
                & (WorkEngagement.workspace_id == ExpenseClaim.workspace_id),
            )
            .where(
                ExpenseClaim.workspace_id == workspace_id,
                ExpenseClaim.deleted_at.is_(None),
                WorkEngagement.user_id == user_id,
            )
        )
        if state is not None:
            stmt = stmt.where(ExpenseClaim.state == state)
        if cursor_id is not None:
            stmt = stmt.where(ExpenseClaim.id < cursor_id)
        stmt = stmt.order_by(ExpenseClaim.id.desc()).limit(limit + 1)
        rows = list(self._session.scalars(stmt).all())
        return [_to_claim_row(r) for r in rows]

    def list_claims_for_workspace(
        self,
        *,
        workspace_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        stmt = select(ExpenseClaim).where(
            ExpenseClaim.workspace_id == workspace_id,
            ExpenseClaim.deleted_at.is_(None),
        )
        if state is not None:
            stmt = stmt.where(ExpenseClaim.state == state)
        if cursor_id is not None:
            stmt = stmt.where(ExpenseClaim.id < cursor_id)
        stmt = stmt.order_by(ExpenseClaim.id.desc()).limit(limit + 1)
        rows = list(self._session.scalars(stmt).all())
        return [_to_claim_row(r) for r in rows]

    def list_pending_claims(
        self,
        *,
        workspace_id: str,
        claimant_user_id: str | None,
        property_id: str | None,
        category: str | None,
        limit: int,
        cursor: PendingClaimsCursor | None,
    ) -> list[ExpenseClaimRow]:
        stmt = select(ExpenseClaim).where(
            ExpenseClaim.workspace_id == workspace_id,
            ExpenseClaim.state == "submitted",
            ExpenseClaim.deleted_at.is_(None),
        )
        if claimant_user_id is not None:
            stmt = stmt.join(
                WorkEngagement,
                (WorkEngagement.id == ExpenseClaim.work_engagement_id)
                & (WorkEngagement.workspace_id == ExpenseClaim.workspace_id),
            ).where(WorkEngagement.user_id == claimant_user_id)
        if property_id is not None:
            stmt = stmt.where(ExpenseClaim.property_id == property_id)
        if category is not None:
            stmt = stmt.where(ExpenseClaim.category == category)
        if cursor is not None:
            stmt = stmt.where(
                (ExpenseClaim.submitted_at < cursor.submitted_at)
                | (
                    (ExpenseClaim.submitted_at == cursor.submitted_at)
                    & (ExpenseClaim.id < cursor.claim_id)
                )
            )
        stmt = stmt.order_by(
            ExpenseClaim.submitted_at.desc(),
            ExpenseClaim.id.desc(),
        ).limit(limit + 1)
        rows = list(self._session.scalars(stmt).all())
        return [_to_claim_row(r) for r in rows]

    def list_pending_reimbursement_claims(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
    ) -> list[ExpenseClaimRow]:
        stmt = select(ExpenseClaim).where(
            ExpenseClaim.workspace_id == workspace_id,
            ExpenseClaim.state == "approved",
            ExpenseClaim.deleted_at.is_(None),
        )
        if user_id is not None:
            stmt = stmt.join(
                WorkEngagement,
                (WorkEngagement.id == ExpenseClaim.work_engagement_id)
                & (WorkEngagement.workspace_id == ExpenseClaim.workspace_id),
            ).where(WorkEngagement.user_id == user_id)
        stmt = stmt.order_by(ExpenseClaim.id.asc())
        rows = list(self._session.scalars(stmt).all())
        return [_to_claim_row(r) for r in rows]

    # -- Attachment CRUD -------------------------------------------------

    def list_attachments_for_claim(
        self, *, workspace_id: str, claim_id: str
    ) -> list[ExpenseAttachmentRow]:
        stmt = (
            select(ExpenseAttachment)
            .where(
                ExpenseAttachment.workspace_id == workspace_id,
                ExpenseAttachment.claim_id == claim_id,
            )
            .order_by(
                ExpenseAttachment.created_at.asc(),
                ExpenseAttachment.id.asc(),
            )
        )
        rows = list(self._session.scalars(stmt).all())
        return [_to_attachment_row(r) for r in rows]

    def get_attachment(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        attachment_id: str,
    ) -> ExpenseAttachmentRow | None:
        stmt = select(ExpenseAttachment).where(
            ExpenseAttachment.id == attachment_id,
            ExpenseAttachment.claim_id == claim_id,
            ExpenseAttachment.workspace_id == workspace_id,
        )
        row = self._session.scalars(stmt).one_or_none()
        if row is None:
            return None
        return _to_attachment_row(row)

    def insert_attachment(
        self,
        *,
        attachment_id: str,
        workspace_id: str,
        claim_id: str,
        blob_hash: str,
        kind: str,
        pages: int | None,
        created_at: datetime,
    ) -> ExpenseAttachmentRow:
        attachment = ExpenseAttachment(
            id=attachment_id,
            workspace_id=workspace_id,
            claim_id=claim_id,
            blob_hash=blob_hash,
            kind=kind,
            pages=pages,
            created_at=created_at,
        )
        self._session.add(attachment)
        self._session.flush()
        return _to_attachment_row(attachment)

    def delete_attachment(
        self, *, workspace_id: str, claim_id: str, attachment_id: str
    ) -> None:
        stmt = select(ExpenseAttachment).where(
            ExpenseAttachment.id == attachment_id,
            ExpenseAttachment.claim_id == claim_id,
            ExpenseAttachment.workspace_id == workspace_id,
        )
        row = self._session.scalars(stmt).one_or_none()
        if row is None:
            # Caller already gated via ``get_attachment``; reaching
            # here means the row vanished mid-UoW. Surface as a
            # programming error so the caller's vocabulary stays
            # explicit (a missing attachment is a 404, not a silent
            # no-op).
            raise RuntimeError(
                f"delete_attachment: attachment {attachment_id!r} not found"
            )
        self._session.delete(row)
        self._session.flush()

    # -- Claim writes ----------------------------------------------------

    def insert_claim(
        self,
        *,
        claim_id: str,
        workspace_id: str,
        work_engagement_id: str,
        vendor: str,
        purchased_at: datetime,
        currency: str,
        total_amount_cents: int,
        category: str,
        property_id: str | None,
        note_md: str,
        created_at: datetime,
    ) -> ExpenseClaimRow:
        row = ExpenseClaim(
            id=claim_id,
            workspace_id=workspace_id,
            work_engagement_id=work_engagement_id,
            vendor=vendor,
            purchased_at=purchased_at,
            currency=currency,
            total_amount_cents=total_amount_cents,
            category=category,
            property_id=property_id,
            note_md=note_md,
            state="draft",
            submitted_at=None,
            decided_by=None,
            decided_at=None,
            decision_note_md=None,
            created_at=created_at,
            deleted_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_claim_row(row)

    def update_claim_fields(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        fields: Mapping[str, Any],
    ) -> ExpenseClaimRow:
        if not fields:
            # No-op write. Re-load the row so the caller still sees
            # the canonical projection (and the row exists).
            row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
            return _to_claim_row(row)

        unknown = set(fields) - _ALLOWED_UPDATE_FIELDS
        if unknown:
            raise KeyError(
                f"update_claim_fields: unknown field name(s) {sorted(unknown)!r}"
            )

        row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
        for key, value in fields.items():
            setattr(row, key, value)
        self._session.flush()
        return _to_claim_row(row)

    def mark_claim_submitted(
        self, *, workspace_id: str, claim_id: str, submitted_at: datetime
    ) -> ExpenseClaimRow:
        row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
        row.state = "submitted"
        row.submitted_at = submitted_at
        self._session.flush()
        return _to_claim_row(row)

    def mark_claim_approved(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
    ) -> ExpenseClaimRow:
        row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
        row.state = "approved"
        row.decided_by = decided_by
        row.decided_at = decided_at
        self._session.flush()
        return _to_claim_row(row)

    def mark_claim_rejected(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
        decision_note_md: str,
    ) -> ExpenseClaimRow:
        row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
        row.state = "rejected"
        row.decided_by = decided_by
        row.decided_at = decided_at
        row.decision_note_md = decision_note_md
        self._session.flush()
        return _to_claim_row(row)

    def mark_claim_reimbursed(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        reimbursed_at: datetime,
        reimbursed_via: str,
        reimbursed_by: str,
    ) -> ExpenseClaimRow:
        row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
        row.state = "reimbursed"
        row.reimbursed_at = reimbursed_at
        row.reimbursed_via = reimbursed_via
        row.reimbursed_by = reimbursed_by
        self._session.flush()
        return _to_claim_row(row)

    def mark_claim_deleted(
        self, *, workspace_id: str, claim_id: str, deleted_at: datetime
    ) -> ExpenseClaimRow:
        row = self._load_claim(workspace_id=workspace_id, claim_id=claim_id)
        row.deleted_at = deleted_at
        self._session.flush()
        return _to_claim_row(row)

    # -- Internal helpers ------------------------------------------------

    def _load_claim(self, *, workspace_id: str, claim_id: str) -> ExpenseClaim:
        """Load a live (non-soft-deleted) claim row for in-place mutation.

        The repo's mutating methods all need the live ORM instance to
        attach the field swap; centralising the load here keeps the
        ``RuntimeError`` ladder uniform across them. Callers gate on
        :meth:`get_claim` first so a missing row mid-UoW signals a
        programming error rather than a normal 404.

        Filters ``deleted_at IS NULL`` so a soft-deleted claim cannot
        be silently mutated by a caller that skipped the gate — mirrors
        the defence-in-depth in :func:`app.domain.expenses.claims._load_claim`
        and :func:`app.domain.expenses.autofill._load_claim`. The
        :meth:`mark_claim_deleted` path still works because it loads the
        live row, then stamps ``deleted_at`` in the same UoW; re-flipping
        an already-deleted claim is not a supported operation.
        """
        stmt = select(ExpenseClaim).where(
            ExpenseClaim.id == claim_id,
            ExpenseClaim.workspace_id == workspace_id,
            ExpenseClaim.deleted_at.is_(None),
        )
        row = self._session.scalars(stmt).one_or_none()
        if row is None:
            raise RuntimeError(
                f"_load_claim: claim {claim_id!r} in workspace "
                f"{workspace_id!r} not found"
            )
        return row

    # -- Identity reads --------------------------------------------------

    def get_user_display_names(self, *, user_ids: Sequence[str]) -> dict[str, str]:
        if not user_ids:
            return {}
        stmt = select(User.id, User.display_name).where(User.id.in_(user_ids))
        return {uid: name for uid, name in self._session.execute(stmt).all()}

    # -- LLM-usage writes ------------------------------------------------

    def insert_llm_usage(
        self,
        *,
        usage_id: str,
        workspace_id: str,
        capability: str,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
        cost_cents: int,
        latency_ms: int,
        status: LlmUsageStatus,
        correlation_id: str,
        actor_user_id: str,
        created_at: datetime,
    ) -> None:
        row = LlmUsageRow(
            id=usage_id,
            workspace_id=workspace_id,
            capability=capability,
            model_id=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_cents=cost_cents,
            latency_ms=latency_ms,
            status=status,
            correlation_id=correlation_id,
            attempt=0,
            assignment_id=None,
            fallback_attempts=0,
            finish_reason=None,
            actor_user_id=actor_user_id,
            token_id=None,
            agent_label=None,
            created_at=created_at,
        )
        self._session.add(row)


# ---------------------------------------------------------------------------
# CapabilityChecker concretion
# ---------------------------------------------------------------------------


class SqlAlchemyCapabilityChecker(CapabilityChecker):
    """SA-backed concretion of :class:`CapabilityChecker`.

    Wraps :func:`app.authz.require` for a fixed ``(session, ctx)``
    pair so callers don't have to thread the workspace scope through
    every ``require()`` call. The transitive walk via
    :mod:`app.authz.membership` / :mod:`app.authz.owners` reaches
    :mod:`app.adapters.db.authz.models` here at the adapter layer
    where the import is allowed — keeping it out of the domain
    services which would otherwise pick up the dependency through a
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
