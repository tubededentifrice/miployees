"""Expenses context — repository + capability seams (cd-v3jp).

Defines the seams :mod:`app.domain.expenses.claims`,
:mod:`app.domain.expenses.approval`, and
:mod:`app.domain.expenses.autofill` use to read and write
:mod:`app.adapters.db.expenses.models` (ExpenseClaim,
ExpenseAttachment), :mod:`app.adapters.db.workspace.models`
(WorkEngagement), :mod:`app.adapters.db.identity.models` (User —
display-name lookup for the per-user pending-reimbursement
breakdown), :mod:`app.adapters.db.llm.models` (LlmUsage — autofill
post-flight ledger), and to enforce action-catalog capabilities
(:func:`app.authz.require`) — without importing those modules
directly.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``).

Two seams live here:

* :class:`ExpensesRepository` — read + write seam for every claim
  / attachment row the three modules touch, plus the workspace
  / identity / LLM-usage row reads they need (engagement membership-
  defence reads, per-user display-name lookup, post-flight LLM-
  usage write). Returns immutable
  :class:`ExpenseClaimRow` / :class:`ExpenseAttachmentRow` /
  :class:`WorkEngagementRow` projections so the domain never sees
  an ORM row.

* :class:`CapabilityChecker` — workspace-scoped authz probe for the
  ``expenses.*`` action keys. Wraps :func:`app.authz.require` at
  the adapter layer so the domain service does not transitively pull
  :mod:`app.adapters.db.authz.models` via :mod:`app.authz.membership`
  / :mod:`app.authz.owners` (the cd-7qxh stopgap rationale).

**Why a single :class:`ExpensesRepository` (rather than per-table).**
The three modules tightly interleave reads/writes across
ExpenseClaim + ExpenseAttachment + WorkEngagement: ``approve_claim``
loads a claim, reads its engagement (for the submitter id), and
writes the audit row in the same UoW. Splitting into per-table
repos would force every public function to take three repo handles;
a single repo exposes the necessary surface in one place. The
identity-context split (``UserAvailabilityOverrideRepository`` /
``UserLeaveRepository``) is a different shape — the two there do
not share read paths.

**Mutating semantics.** Claim + attachment writes flush after the
mutation so the caller's audit-writer FK reference (and any peer
read in the same UoW) sees the new row. The :meth:`ExpensesRepository.insert_llm_usage`
write does NOT flush — the LLM-usage ledger row is never an audit
target and the caller's outer UoW commit covers it. The repo never
commits — the caller's UoW owns the transaction boundary (§01
"Key runtime invariants" #3).

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against these protocols would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session

__all__ = [
    "CapabilityChecker",
    "ExpenseAttachmentRow",
    "ExpenseClaimRow",
    "ExpensesRepository",
    "LlmUsageStatus",
    "PendingClaimsCursor",
    "SeamPermissionDenied",
    "WorkEngagementRow",
]


# ---------------------------------------------------------------------------
# Seam exceptions
# ---------------------------------------------------------------------------


class SeamPermissionDenied(Exception):
    """Raised by :meth:`CapabilityChecker.require` for a denied capability.

    A seam-level analogue of :class:`app.authz.PermissionDenied` so the
    domain service can ``except`` on this without importing
    :mod:`app.authz` (the transitive walk via :mod:`app.authz.membership`
    / :mod:`app.authz.owners` is what the cd-7qxh stopgap was tagged to
    bypass). The SA-backed checker in
    :mod:`app.adapters.db.expenses.repositories` translates the
    underlying authz exception into this seam-level one before raising.

    Domain services re-raise this as their own context-specific
    ``PermissionDenied`` shape (``ClaimPermissionDenied`` /
    ``ApprovalPermissionDenied`` / ``ReimbursePermissionDenied``) so
    the router's error map stays narrow — one domain exception type per
    403 envelope.
    """


# ---------------------------------------------------------------------------
# Row + value-object shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpenseAttachmentRow:
    """Immutable projection of an ``expense_attachment`` row.

    Mirrors the column shape of
    :class:`app.adapters.db.expenses.models.ExpenseAttachment`. Declared
    here so the SA adapter projects ORM rows into a domain-owned shape
    without forcing the domain service to import the ORM class.
    """

    id: str
    workspace_id: str
    claim_id: str
    blob_hash: str
    kind: str
    pages: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExpenseClaimRow:
    """Immutable projection of an ``expense_claim`` row.

    Carries every column the three modules read. Mirrors the column
    shape of :class:`app.adapters.db.expenses.models.ExpenseClaim`.
    The ``llm_autofill_json`` payload is held as a generic
    :class:`Mapping` (or ``None``) so the domain layer can branch on
    "no autofill yet" via ``IS NULL`` semantics without typing the
    payload schema on the seam.
    """

    id: str
    workspace_id: str
    work_engagement_id: str
    vendor: str
    purchased_at: datetime
    currency: str
    total_amount_cents: int
    category: str
    property_id: str | None
    note_md: str
    state: str
    submitted_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note_md: str | None
    reimbursed_at: datetime | None
    reimbursed_via: str | None
    reimbursed_by: str | None
    llm_autofill_json: Mapping[str, Any] | None
    autofill_confidence_overall: Decimal | None
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkEngagementRow:
    """Immutable projection of a ``work_engagement`` row.

    The expenses domain only needs the ``user_id`` (membership-defence
    on create + per-user filtering on list / pending-reimbursement)
    and the ``id`` (echo-back). Other engagement columns
    (``engagement_kind``, pay destinations, etc.) are not consulted by
    the expenses flow today; if a future caller needs them the seam
    grows then.
    """

    id: str
    workspace_id: str
    user_id: str


# ``status`` enum on the LlmUsage row — keeps the seam contract narrow.
LlmUsageStatus = Literal["ok", "error", "timeout"]


@dataclass(frozen=True, slots=True)
class PendingClaimsCursor:
    """Decoded cursor for the manager pending-queue read path.

    The queue orders by ``submitted_at DESC, id DESC``; the cursor
    pins the position of the last-returned claim. Wrapping the pair
    in a value object keeps the seam method signature narrow (one
    parameter, not two) and ensures callers cannot accidentally
    mismatch the column ordering.
    """

    submitted_at: datetime
    claim_id: str


# ---------------------------------------------------------------------------
# CapabilityChecker
# ---------------------------------------------------------------------------


class CapabilityChecker(Protocol):
    """Workspace-scoped action-catalog probe used by the expenses services.

    Wraps the canonical :func:`app.authz.require` enforcement so
    callers don't transitively pull :mod:`app.adapters.db.authz.models`
    via the authz module's membership / owners walk (the cd-7qxh
    stopgap rationale). The SA-backed concretion lives in
    :mod:`app.adapters.db.expenses.repositories`; tests substitute
    fakes.

    Pinned at construction time to a single ``(session, workspace_id,
    actor)`` triple — the underlying :func:`require` call always uses
    ``scope_kind='workspace'`` and ``scope_id=ctx.workspace_id``
    because every action key the expenses services check is workspace-
    scoped.

    A misconfigured action catalog (unknown key, invalid scope) is a
    server-side bug, not a denial — the SA concretion lets those
    errors propagate as :class:`RuntimeError` rather than
    :class:`SeamPermissionDenied` so the router surfaces 500, not 403.
    """

    def require(self, action_key: str) -> None:
        """Enforce the named capability or raise :class:`SeamPermissionDenied`.

        Callers re-raise the seam exception as their own context-
        specific 403 type so the router's error map stays narrow.
        """
        ...


# ---------------------------------------------------------------------------
# ExpensesRepository
# ---------------------------------------------------------------------------


class ExpensesRepository(Protocol):
    """Read + write seam for the expenses-context rows the three modules touch.

    Hides every direct ORM read from the import surface of
    :mod:`app.domain.expenses.claims`,
    :mod:`app.domain.expenses.approval`, and
    :mod:`app.domain.expenses.autofill` so the cd-7rfu / cd-9guk /
    cd-95zb / cd-7qxh ignore_imports entries can drop. The SA-backed
    concretion in :mod:`app.adapters.db.expenses.repositories` walks
    five ORM classes:

    * :class:`~app.adapters.db.expenses.models.ExpenseClaim` — claim
      CRUD + state mutations.
    * :class:`~app.adapters.db.expenses.models.ExpenseAttachment` —
      attachment CRUD.
    * :class:`~app.adapters.db.workspace.models.WorkEngagement` —
      membership-defence reads (engagement -> user_id).
    * :class:`~app.adapters.db.identity.models.User` — bulk display-
      name lookup for the per-user pending-reimbursement breakdown
      (cd-mh4p).
    * :class:`~app.adapters.db.llm.models.LlmUsage` — post-flight
      autofill usage row writes (cd-95zb).

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    The repo never commits — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3). Claim + attachment
    writes flush so the caller's next read (and the audit writer's
    FK reference to ``entity_id``) sees the new row.
    :meth:`insert_llm_usage` does NOT flush — that row is never an
    audit target and the caller's outer UoW commit covers it.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its own
        Protocol port.
        """
        ...

    # -- Engagement reads ------------------------------------------------

    def get_engagement(
        self, *, workspace_id: str, engagement_id: str
    ) -> WorkEngagementRow | None:
        """Return the engagement scoped to ``workspace_id`` or ``None``.

        Used for the membership-defence "the bound engagement belongs
        to the caller" check on every claim write path.
        """
        ...

    def get_engagement_user_ids(
        self, *, workspace_id: str, engagement_ids: Sequence[str]
    ) -> dict[str, str]:
        """Bulk-resolve ``engagement_id -> user_id`` for the supplied ids.

        Returns an empty dict when ``engagement_ids`` is empty so the
        caller can pass an empty sequence without conditional plumbing.
        Used by :func:`pending_reimbursement` to roll up claims into
        per-user groups in a single query rather than N+1.
        """
        ...

    # -- Claim CRUD ------------------------------------------------------

    def get_claim(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> ExpenseClaimRow | None:
        """Return the claim row or ``None``.

        Soft-deleted rows (``deleted_at IS NOT NULL``) are hidden
        unless ``include_deleted=True``. ``for_update`` toggles a
        row-level ``SELECT ... FOR UPDATE`` so every state mutation
        serialises against any concurrent mutation of the same claim;
        the SQLite engine drops the clause silently (its whole-database
        write lock already serialises).
        """
        ...

    def list_claims_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        """Return up to ``limit + 1`` rows for the per-user listing.

        Loads ``limit + 1`` rows so the caller can compute
        ``has_more`` without a second query. ``cursor_id`` walks
        ``id < cursor_id`` (descending order); ``None`` starts from
        the most-recent. ``state`` narrows by lifecycle state when
        non-``None``. Soft-deleted rows excluded.
        """
        ...

    def list_claims_for_workspace(
        self,
        *,
        workspace_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        """Return up to ``limit + 1`` rows for the workspace-wide listing.

        Same shape as :meth:`list_claims_for_user` minus the user
        filter. Used by the manager-queue read path that does not
        narrow by claimant.
        """
        ...

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
        """Return up to ``limit + 1`` submitted claims for the manager queue.

        Filters: ``claimant_user_id`` rides through the ``WorkEngagement``
        join; ``property_id`` is a soft-ref equality; ``category`` is
        an enum equality. Order: ``submitted_at DESC, id DESC``. The
        cursor decodes via :class:`PendingClaimsCursor` and walks
        forward (older rows under the DESC sort). Soft-deleted rows
        and non-submitted rows are excluded by the where clause.
        """
        ...

    def list_pending_reimbursement_claims(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
    ) -> list[ExpenseClaimRow]:
        """Return every approved-but-not-reimbursed claim in scope.

        ``user_id`` narrows via the engagement join when non-``None``;
        otherwise returns the full workspace pool. Order: ``id ASC``
        for deterministic same-currency rollup.
        """
        ...

    # -- Attachment CRUD -------------------------------------------------

    def list_attachments_for_claim(
        self, *, workspace_id: str, claim_id: str
    ) -> list[ExpenseAttachmentRow]:
        """Return every attachment for ``claim_id`` in upload order."""
        ...

    def get_attachment(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        attachment_id: str,
    ) -> ExpenseAttachmentRow | None:
        """Return the attachment scoped to ``workspace_id`` + ``claim_id``."""
        ...

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
        """Insert one attachment row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        ``entity_id`` FK reference) sees the new row.
        """
        ...

    def delete_attachment(
        self, *, workspace_id: str, claim_id: str, attachment_id: str
    ) -> None:
        """Hard-delete the attachment row (soft-delete is a claim-level concern)."""
        ...

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
        """Insert a fresh claim in ``state='draft'`` and return its row.

        Optional decision / submission columns are left NULL — the
        state machine populates them on subsequent transitions.
        Flushes so the audit writer's FK reference sees the new row.
        """
        ...

    def update_claim_fields(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        fields: Mapping[str, Any],
    ) -> ExpenseClaimRow:
        """Rewrite the supplied scalar columns on the claim and flush.

        Caller is responsible for validating + canonicalising every
        value. Only the keys present in ``fields`` are touched; absent
        keys leave the row's column intact. Returns the refreshed
        projection.

        Used by the worker-side update path AND the approver inline-
        edit path AND the autofill scalar rewrite. The set of
        accepted column names is the union of those three writers'
        needs (vendor / purchased_at / currency / total_amount_cents
        / category / property_id / note_md / work_engagement_id /
        llm_autofill_json / autofill_confidence_overall).
        """
        ...

    def mark_claim_submitted(
        self, *, workspace_id: str, claim_id: str, submitted_at: datetime
    ) -> ExpenseClaimRow:
        """Stamp ``state='submitted'`` + ``submitted_at`` and flush."""
        ...

    def mark_claim_approved(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
    ) -> ExpenseClaimRow:
        """Stamp ``state='approved'`` + ``decided_by`` + ``decided_at``."""
        ...

    def mark_claim_rejected(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
        decision_note_md: str,
    ) -> ExpenseClaimRow:
        """Stamp ``state='rejected'`` + decision triplet."""
        ...

    def mark_claim_reimbursed(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        reimbursed_at: datetime,
        reimbursed_via: str,
        reimbursed_by: str,
    ) -> ExpenseClaimRow:
        """Stamp ``state='reimbursed'`` + reimbursement triplet."""
        ...

    def mark_claim_deleted(
        self, *, workspace_id: str, claim_id: str, deleted_at: datetime
    ) -> ExpenseClaimRow:
        """Stamp ``deleted_at`` (soft-delete; row stays for audit replay)."""
        ...

    # -- Identity reads --------------------------------------------------

    def get_user_display_names(self, *, user_ids: Sequence[str]) -> dict[str, str]:
        """Bulk-resolve ``user_id -> display_name`` for the supplied ids.

        Returns an empty dict when ``user_ids`` is empty so the caller
        can pass an empty sequence without conditional plumbing. A
        user row that has been hard-deleted (rare) is missing from
        the dict; the caller substitutes a synthetic ``"unknown"``
        label rather than missing-key — the manager UI must always
        render a name on every breakdown row.
        """
        ...

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
        """Insert one ``llm_usage`` row in the same UoW.

        Does NOT flush — the row is never referenced by an audit-writer
        FK lookup, and the caller's outer UoW commit covers it. (The
        claim + attachment writes flush; this one is the only exception
        — keep an eye on it if a new caller relies on a mid-UoW read.)

        The ledger row carries the post-flight token counts (or a
        zeroed payload for pre-body failures — timeout / rate-limit /
        non-2xx provider error). Bypasses :mod:`app.domain.llm.budget`
        — see :func:`app.domain.expenses.autofill._record_llm_usage`
        for the rationale.
        """
        ...
