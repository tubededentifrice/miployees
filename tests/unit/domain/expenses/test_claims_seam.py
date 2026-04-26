"""Fake-driven seam tests for :mod:`app.domain.expenses.claims` (cd-0e8i).

Drives the claim service against in-memory fakes for the
:class:`~app.domain.expenses.ports.ExpensesRepository` and
:class:`~app.domain.expenses.ports.CapabilityChecker` Protocols so the
domain contract is exercised without an SQLAlchemy round-trip.
Validates:

* validation paths (:class:`CurrencyInvalid`,
  :class:`PurchaseDateInFuture`),
* permission paths (:class:`ClaimPermissionDenied` from the fake
  capability checker),
* not-found / state-machine transitions
  (:class:`ClaimNotFound`, :class:`ClaimNotEditable`,
  :class:`ClaimStateTransitionInvalid`),
* the seam contract — claim writes flush via ``insert_claim`` /
  ``mark_claim_*`` / ``update_claim_fields``, attachment writes via
  ``insert_attachment`` / ``delete_attachment``.

The SA-backed concretion (DB roundtrip + tenant filter) is covered by
``tests/unit/adapters/db/test_expenses_repository.py``; the integration
suite at ``tests/integration/api/test_expenses_routes.py`` covers the
HTTP boundary. This file owns the fake-driven seam contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.expenses import (
    ClaimNotEditable,
    ClaimNotFound,
    ClaimPermissionDenied,
    ClaimStateTransitionInvalid,
    CurrencyInvalid,
    ExpenseClaimCreate,
    ExpenseClaimUpdate,
    PurchaseDateInFuture,
    cancel_claim,
    create_claim,
    detach_receipt,
    get_claim,
    list_for_user,
    list_for_workspace,
    pending_reimbursement,
    submit_claim,
    update_claim,
)
from app.domain.expenses.ports import (
    ExpenseAttachmentRow,
    ExpenseClaimRow,
    PendingClaimsCursor,
    SeamPermissionDenied,
    WorkEngagementRow,
)
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)
_WS_ID = "01HWA00000000000000000WS01"
_ACTOR_ID = "01HWA00000000000000000USR1"
_OTHER_USER = "01HWA00000000000000000USR2"
_ENG_ID = "01HWA00000000000000000ENG1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAuditSession:
    """Audit-side ``session`` stub.

    The seam tests don't assert on audit-row contents (the SA suite
    covers the full audit round-trip); we only need ``add`` /
    ``flush`` to be no-ops so :func:`app.audit.write_audit` doesn't
    blow up on its first call.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        pass


@dataclass
class _FakeRepo:
    """In-memory :class:`ExpensesRepository` for the seam tests.

    Models the surface :mod:`app.domain.expenses.claims` consumes —
    enough to drive the public API end-to-end without an SQLAlchemy
    session. Mirrors the SA concretion's flush semantics: writes are
    visible to subsequent reads in the same fake.
    """

    engagements: dict[str, WorkEngagementRow] = field(default_factory=dict)
    claims: dict[str, ExpenseClaimRow] = field(default_factory=dict)
    attachments: dict[str, ExpenseAttachmentRow] = field(default_factory=dict)
    user_names: dict[str, str] = field(default_factory=dict)
    audit_session: _FakeAuditSession = field(default_factory=_FakeAuditSession)
    llm_usage_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def session(self) -> Any:
        # ``Any`` keeps mypy quiet — the protocol declares ``Session``
        # but our audit writer only needs ``add`` / ``flush``.
        return self.audit_session

    # -- engagements ----------------------------------------------------
    def get_engagement(
        self, *, workspace_id: str, engagement_id: str
    ) -> WorkEngagementRow | None:
        eng = self.engagements.get(engagement_id)
        if eng is None or eng.workspace_id != workspace_id:
            return None
        return eng

    def get_engagement_user_ids(
        self, *, workspace_id: str, engagement_ids: Sequence[str]
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for eid in engagement_ids:
            eng = self.engagements.get(eid)
            if eng is not None and eng.workspace_id == workspace_id:
                out[eid] = eng.user_id
        return out

    # -- claim CRUD -----------------------------------------------------
    def get_claim(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> ExpenseClaimRow | None:
        row = self.claims.get(claim_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        if not include_deleted and row.deleted_at is not None:
            return None
        return row

    def list_claims_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        rows = [
            r
            for r in self.claims.values()
            if r.workspace_id == workspace_id
            and r.deleted_at is None
            and self._claim_user_id(r) == user_id
            and (state is None or r.state == state)
            and (cursor_id is None or r.id < cursor_id)
        ]
        rows.sort(key=lambda r: r.id, reverse=True)
        return rows[: limit + 1]

    def list_claims_for_workspace(
        self,
        *,
        workspace_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        rows = [
            r
            for r in self.claims.values()
            if r.workspace_id == workspace_id
            and r.deleted_at is None
            and (state is None or r.state == state)
            and (cursor_id is None or r.id < cursor_id)
        ]
        rows.sort(key=lambda r: r.id, reverse=True)
        return rows[: limit + 1]

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
        rows = [
            r
            for r in self.claims.values()
            if r.workspace_id == workspace_id
            and r.state == "submitted"
            and r.deleted_at is None
            and (claimant_user_id is None or self._claim_user_id(r) == claimant_user_id)
            and (property_id is None or r.property_id == property_id)
            and (category is None or r.category == category)
        ]
        rows.sort(
            key=lambda r: (r.submitted_at or _PINNED, r.id),
            reverse=True,
        )
        return rows[: limit + 1]

    def list_pending_reimbursement_claims(
        self, *, workspace_id: str, user_id: str | None
    ) -> list[ExpenseClaimRow]:
        rows = [
            r
            for r in self.claims.values()
            if r.workspace_id == workspace_id
            and r.state == "approved"
            and r.deleted_at is None
            and (user_id is None or self._claim_user_id(r) == user_id)
        ]
        rows.sort(key=lambda r: r.id)
        return rows

    # -- attachments ----------------------------------------------------
    def list_attachments_for_claim(
        self, *, workspace_id: str, claim_id: str
    ) -> list[ExpenseAttachmentRow]:
        rows = [
            a
            for a in self.attachments.values()
            if a.workspace_id == workspace_id and a.claim_id == claim_id
        ]
        rows.sort(key=lambda a: (a.created_at, a.id))
        return rows

    def get_attachment(
        self, *, workspace_id: str, claim_id: str, attachment_id: str
    ) -> ExpenseAttachmentRow | None:
        row = self.attachments.get(attachment_id)
        if row is None or row.workspace_id != workspace_id or row.claim_id != claim_id:
            return None
        return row

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
        row = ExpenseAttachmentRow(
            id=attachment_id,
            workspace_id=workspace_id,
            claim_id=claim_id,
            blob_hash=blob_hash,
            kind=kind,
            pages=pages,
            created_at=created_at,
        )
        self.attachments[attachment_id] = row
        return row

    def delete_attachment(
        self, *, workspace_id: str, claim_id: str, attachment_id: str
    ) -> None:
        del self.attachments[attachment_id]

    # -- claim writes ---------------------------------------------------
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
        row = ExpenseClaimRow(
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
            reimbursed_at=None,
            reimbursed_via=None,
            reimbursed_by=None,
            llm_autofill_json=None,
            autofill_confidence_overall=None,
            created_at=created_at,
            deleted_at=None,
        )
        self.claims[claim_id] = row
        return row

    def update_claim_fields(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        fields: Mapping[str, Any],
    ) -> ExpenseClaimRow:
        row = self.claims[claim_id]
        if not fields:
            return row
        # ``replace`` returns a fresh frozen dataclass with the
        # supplied keys overridden; matches the SA concretion's
        # "fresh projection" semantics.
        new_row = replace(row, **fields)
        self.claims[claim_id] = new_row
        return new_row

    def mark_claim_submitted(
        self, *, workspace_id: str, claim_id: str, submitted_at: datetime
    ) -> ExpenseClaimRow:
        row = self.claims[claim_id]
        new_row = replace(row, state="submitted", submitted_at=submitted_at)
        self.claims[claim_id] = new_row
        return new_row

    def mark_claim_approved(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
    ) -> ExpenseClaimRow:
        row = self.claims[claim_id]
        new_row = replace(
            row,
            state="approved",
            decided_by=decided_by,
            decided_at=decided_at,
        )
        self.claims[claim_id] = new_row
        return new_row

    def mark_claim_rejected(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
        decision_note_md: str,
    ) -> ExpenseClaimRow:
        row = self.claims[claim_id]
        new_row = replace(
            row,
            state="rejected",
            decided_by=decided_by,
            decided_at=decided_at,
            decision_note_md=decision_note_md,
        )
        self.claims[claim_id] = new_row
        return new_row

    def mark_claim_reimbursed(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        reimbursed_at: datetime,
        reimbursed_via: str,
        reimbursed_by: str,
    ) -> ExpenseClaimRow:
        row = self.claims[claim_id]
        new_row = replace(
            row,
            state="reimbursed",
            reimbursed_at=reimbursed_at,
            reimbursed_via=reimbursed_via,
            reimbursed_by=reimbursed_by,
        )
        self.claims[claim_id] = new_row
        return new_row

    def mark_claim_deleted(
        self, *, workspace_id: str, claim_id: str, deleted_at: datetime
    ) -> ExpenseClaimRow:
        row = self.claims[claim_id]
        new_row = replace(row, deleted_at=deleted_at)
        self.claims[claim_id] = new_row
        return new_row

    # -- identity / llm-usage -------------------------------------------
    def get_user_display_names(self, *, user_ids: Sequence[str]) -> dict[str, str]:
        return {uid: self.user_names[uid] for uid in user_ids if uid in self.user_names}

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
        status: str,
        correlation_id: str,
        actor_user_id: str,
        created_at: datetime,
    ) -> None:
        self.llm_usage_rows.append(
            {
                "id": usage_id,
                "workspace_id": workspace_id,
                "capability": capability,
                "model_id": model_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_cents": cost_cents,
                "latency_ms": latency_ms,
                "status": status,
                "correlation_id": correlation_id,
                "actor_user_id": actor_user_id,
                "created_at": created_at,
            }
        )

    # -- helpers --------------------------------------------------------
    def _claim_user_id(self, row: ExpenseClaimRow) -> str | None:
        eng = self.engagements.get(row.work_engagement_id)
        return eng.user_id if eng is not None else None


@dataclass
class _FakeChecker:
    """In-memory :class:`CapabilityChecker`.

    Pre-program ``allowed_keys`` with the action keys the caller is
    permitted; everything else raises :class:`SeamPermissionDenied`.
    Records every checked key on :attr:`required_keys` so a test can
    assert on what was probed.
    """

    allowed_keys: set[str] = field(default_factory=set)
    required_keys: list[str] = field(default_factory=list)

    def require(self, action_key: str) -> None:
        self.required_keys.append(action_key)
        if action_key not in self.allowed_keys:
            raise SeamPermissionDenied(f"caller lacks {action_key!r}")


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    workspace_id: str = _WS_ID,
    actor_id: str = _ACTOR_ID,
    grant_role: ActorGrantRole = "worker",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _engagement(
    *, eng_id: str = _ENG_ID, user_id: str = _ACTOR_ID, ws_id: str = _WS_ID
) -> WorkEngagementRow:
    return WorkEngagementRow(id=eng_id, workspace_id=ws_id, user_id=user_id)


def _create_body(
    *, work_engagement_id: str = _ENG_ID, currency: str = "EUR"
) -> ExpenseClaimCreate:
    return ExpenseClaimCreate(
        work_engagement_id=work_engagement_id,
        vendor="Acme",
        purchased_at=_PURCHASED,
        currency=currency,
        total_amount_cents=1000,
        category="supplies",
    )


def _seamed_repo() -> _FakeRepo:
    """Return a repo with a single self-engagement + worker context."""
    repo = _FakeRepo()
    repo.engagements[_ENG_ID] = _engagement()
    return repo


# ---------------------------------------------------------------------------
# create_claim
# ---------------------------------------------------------------------------


class TestCreateClaim:
    def test_happy_path(self) -> None:
        repo = _seamed_repo()
        checker = _FakeChecker()
        view = create_claim(
            repo,
            checker,
            _ctx(),
            body=_create_body(),
            clock=FrozenClock(_PINNED),
        )
        assert view.state == "draft"
        assert view.currency == "EUR"
        # The seam stamped the row under the same UoW.
        assert view.id in repo.claims
        # No capability check fires on self-create.
        assert checker.required_keys == []

    def test_currency_is_uppercased(self) -> None:
        repo = _seamed_repo()
        view = create_claim(
            repo,
            _FakeChecker(),
            _ctx(),
            body=_create_body(currency="usd"),
            clock=FrozenClock(_PINNED),
        )
        assert view.currency == "USD"

    def test_unknown_currency_raises(self) -> None:
        repo = _seamed_repo()
        with pytest.raises(CurrencyInvalid):
            create_claim(
                repo,
                _FakeChecker(),
                _ctx(),
                body=_create_body(currency="ZZZ"),
                clock=FrozenClock(_PINNED),
            )
        # No claim was inserted on the validation path.
        assert repo.claims == {}

    def test_future_purchased_at_rejected(self) -> None:
        repo = _seamed_repo()
        body = ExpenseClaimCreate(
            work_engagement_id=_ENG_ID,
            vendor="V",
            purchased_at=_PINNED + timedelta(hours=1),
            currency="EUR",
            total_amount_cents=100,
            category="supplies",
        )
        with pytest.raises(PurchaseDateInFuture):
            create_claim(
                repo, _FakeChecker(), _ctx(), body=body, clock=FrozenClock(_PINNED)
            )
        assert repo.claims == {}

    def test_engagement_in_other_workspace_rejected(self) -> None:
        repo = _FakeRepo()
        # Engagement lives in a different workspace.
        repo.engagements[_ENG_ID] = WorkEngagementRow(
            id=_ENG_ID, workspace_id="other-ws", user_id=_ACTOR_ID
        )
        with pytest.raises(ClaimPermissionDenied):
            create_claim(
                repo,
                _FakeChecker(),
                _ctx(),
                body=_create_body(),
                clock=FrozenClock(_PINNED),
            )

    def test_engagement_owned_by_other_user_rejected(self) -> None:
        repo = _FakeRepo()
        repo.engagements[_ENG_ID] = _engagement(user_id=_OTHER_USER)
        with pytest.raises(ClaimPermissionDenied):
            create_claim(
                repo,
                _FakeChecker(),
                _ctx(),
                body=_create_body(),
                clock=FrozenClock(_PINNED),
            )


# ---------------------------------------------------------------------------
# update_claim
# ---------------------------------------------------------------------------


class TestUpdateClaim:
    def test_partial_update_changes_supplied_fields(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        edited = update_claim(
            repo,
            _FakeChecker(),
            _ctx(),
            claim_id=created.id,
            body=ExpenseClaimUpdate(vendor="New", total_amount_cents=999),
            clock=clock,
        )
        assert edited.vendor == "New"
        assert edited.total_amount_cents == 999
        assert edited.currency == "EUR"

    def test_update_on_submitted_rejected(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        # Worker carries the ``expenses.submit`` capability.
        checker = _FakeChecker(allowed_keys={"expenses.submit"})
        created = create_claim(repo, checker, _ctx(), body=_create_body(), clock=clock)
        submit_claim(repo, checker, _ctx(), claim_id=created.id, clock=clock)
        with pytest.raises(ClaimNotEditable):
            update_claim(
                repo,
                _FakeChecker(),
                _ctx(),
                claim_id=created.id,
                body=ExpenseClaimUpdate(vendor="Whoops"),
                clock=clock,
            )

    def test_no_op_update_returns_unchanged(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        view = update_claim(
            repo,
            _FakeChecker(),
            _ctx(),
            claim_id=created.id,
            body=ExpenseClaimUpdate(),
            clock=clock,
        )
        assert view.id == created.id
        assert view.vendor == created.vendor

    def test_invalid_currency_on_update(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        with pytest.raises(CurrencyInvalid):
            update_claim(
                repo,
                _FakeChecker(),
                _ctx(),
                claim_id=created.id,
                body=ExpenseClaimUpdate(currency="ZZZ"),
                clock=clock,
            )


# ---------------------------------------------------------------------------
# get_claim / list_for_user / list_for_workspace
# ---------------------------------------------------------------------------


class TestReads:
    def test_get_claim_self_no_capability_check(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        checker = _FakeChecker()
        view = get_claim(repo, checker, _ctx(), claim_id=created.id)
        assert view.id == created.id
        assert checker.required_keys == []

    def test_get_claim_other_user_requires_approve(self) -> None:
        repo = _FakeRepo()
        repo.engagements[_ENG_ID] = _engagement(user_id=_OTHER_USER)
        clock = FrozenClock(_PINNED)
        # The OTHER user creates the claim against their own engagement.
        owner_ctx = _ctx(actor_id=_OTHER_USER)
        created = create_claim(
            repo, _FakeChecker(), owner_ctx, body=_create_body(), clock=clock
        )
        # The current actor (different user) tries to read the claim.
        checker = _FakeChecker(allowed_keys={"expenses.approve"})
        view = get_claim(repo, checker, _ctx(), claim_id=created.id)
        assert view.id == created.id
        assert checker.required_keys == ["expenses.approve"]

    def test_get_claim_other_user_403_without_capability(self) -> None:
        repo = _FakeRepo()
        repo.engagements[_ENG_ID] = _engagement(user_id=_OTHER_USER)
        clock = FrozenClock(_PINNED)
        owner_ctx = _ctx(actor_id=_OTHER_USER)
        created = create_claim(
            repo, _FakeChecker(), owner_ctx, body=_create_body(), clock=clock
        )
        with pytest.raises(ClaimPermissionDenied):
            get_claim(repo, _FakeChecker(), _ctx(), claim_id=created.id)

    def test_get_claim_404_cross_workspace(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        other_ctx = _ctx(workspace_id="other-ws")
        with pytest.raises(ClaimNotFound):
            get_claim(repo, _FakeChecker(), other_ctx, claim_id=created.id)

    def test_list_for_workspace_requires_capability(self) -> None:
        repo = _seamed_repo()
        # No allowed keys → 403.
        with pytest.raises(ClaimPermissionDenied):
            list_for_workspace(repo, _FakeChecker(), _ctx())

    def test_list_for_workspace_with_manager_cap(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        create_claim(repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock)
        rows, cursor = list_for_workspace(
            repo, _FakeChecker(allowed_keys={"expenses.approve"}), _ctx()
        )
        assert len(rows) == 1
        assert cursor is None

    def test_list_for_user_self_default(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        create_claim(repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock)
        rows, _ = list_for_user(repo, _FakeChecker(), _ctx())
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# submit_claim
# ---------------------------------------------------------------------------


class TestSubmitClaim:
    def test_draft_to_submitted(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        checker = _FakeChecker(allowed_keys={"expenses.submit"})
        created = create_claim(repo, checker, _ctx(), body=_create_body(), clock=clock)
        view = submit_claim(repo, checker, _ctx(), claim_id=created.id, clock=clock)
        assert view.state == "submitted"
        assert view.submitted_at == _PINNED

    def test_submit_without_capability_403(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        with pytest.raises(ClaimPermissionDenied):
            submit_claim(repo, _FakeChecker(), _ctx(), claim_id=created.id, clock=clock)

    def test_double_submit_rejected(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        checker = _FakeChecker(allowed_keys={"expenses.submit"})
        created = create_claim(repo, checker, _ctx(), body=_create_body(), clock=clock)
        submit_claim(repo, checker, _ctx(), claim_id=created.id, clock=clock)
        with pytest.raises(ClaimStateTransitionInvalid):
            submit_claim(repo, checker, _ctx(), claim_id=created.id, clock=clock)


# ---------------------------------------------------------------------------
# cancel_claim
# ---------------------------------------------------------------------------


class TestCancelClaim:
    def test_cancel_draft_soft_deletes(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        view = cancel_claim(
            repo, _FakeChecker(), _ctx(), claim_id=created.id, clock=clock
        )
        assert view.deleted_at == _PINNED
        # Soft-deleted claim is invisible.
        with pytest.raises(ClaimNotFound):
            get_claim(repo, _FakeChecker(), _ctx(), claim_id=created.id)

    def test_cancel_submitted_marks_rejected(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        checker = _FakeChecker(allowed_keys={"expenses.submit"})
        created = create_claim(repo, checker, _ctx(), body=_create_body(), clock=clock)
        submit_claim(repo, checker, _ctx(), claim_id=created.id, clock=clock)
        view = cancel_claim(
            repo, _FakeChecker(), _ctx(), claim_id=created.id, clock=clock
        )
        assert view.state == "rejected"
        assert view.decision_note_md == "cancelled by requester"


# ---------------------------------------------------------------------------
# detach_receipt
# ---------------------------------------------------------------------------


class TestDetachReceipt:
    def test_detach_unknown_attachment_404(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        created = create_claim(
            repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock
        )
        with pytest.raises(ClaimNotFound):
            detach_receipt(
                repo,
                _FakeChecker(),
                _ctx(),
                claim_id=created.id,
                attachment_id="missing",
                clock=clock,
            )


# ---------------------------------------------------------------------------
# pending_reimbursement
# ---------------------------------------------------------------------------


class TestPendingReimbursement:
    def test_self_only_no_capability(self) -> None:
        repo = _seamed_repo()
        clock = FrozenClock(_PINNED)
        # Approved claim for self.
        body = _create_body()
        created = create_claim(repo, _FakeChecker(), _ctx(), body=body, clock=clock)
        repo.mark_claim_approved(
            workspace_id=_WS_ID,
            claim_id=created.id,
            decided_by="manager-id",
            decided_at=_PINNED,
        )
        checker = _FakeChecker()
        view = pending_reimbursement(repo, checker, _ctx(), user_id=_ACTOR_ID)
        assert view.user_id == _ACTOR_ID
        assert len(view.claims) == 1
        # No capability check on self.
        assert checker.required_keys == []

    def test_other_user_requires_capability(self) -> None:
        repo = _seamed_repo()
        with pytest.raises(ClaimPermissionDenied):
            pending_reimbursement(repo, _FakeChecker(), _ctx(), user_id=_OTHER_USER)

    def test_workspace_aggregate_with_breakdown(self) -> None:
        repo = _FakeRepo()
        repo.engagements[_ENG_ID] = _engagement(user_id=_ACTOR_ID)
        other_eng = "01HWA00000000000000000ENG2"
        repo.engagements[other_eng] = _engagement(eng_id=other_eng, user_id=_OTHER_USER)
        repo.user_names = {_ACTOR_ID: "Alice", _OTHER_USER: "Bob"}
        clock = FrozenClock(_PINNED)
        # Approved claim for self.
        a = create_claim(repo, _FakeChecker(), _ctx(), body=_create_body(), clock=clock)
        repo.mark_claim_approved(
            workspace_id=_WS_ID,
            claim_id=a.id,
            decided_by="m",
            decided_at=_PINNED,
        )
        # Approved claim for other user.
        b = create_claim(
            repo,
            _FakeChecker(),
            _ctx(actor_id=_OTHER_USER),
            body=_create_body(work_engagement_id=other_eng),
            clock=clock,
        )
        repo.mark_claim_approved(
            workspace_id=_WS_ID,
            claim_id=b.id,
            decided_by="m",
            decided_at=_PINNED,
        )
        view = pending_reimbursement(
            repo,
            _FakeChecker(allowed_keys={"expenses.approve"}),
            _ctx(),
            user_id=None,
        )
        assert view.user_id is None
        assert view.by_user is not None
        names = {b.user_id for b in view.by_user}
        assert names == {_ACTOR_ID, _OTHER_USER}


# ---------------------------------------------------------------------------
# Enum-parity guard
# ---------------------------------------------------------------------------


class TestEnumParity:
    """Lifted from the assertion at the bottom of
    :mod:`app.domain.expenses.claims` (cd-0e8i): the domain layer can
    no longer import the adapter's private value tuples after the seam
    refactor, so the parity check now runs as a test. Imports both
    sides — adapter + domain — which is allowed at the test layer per
    ``docs/specs/01-architecture.md`` §"Boundary rules"."""

    def test_state_values_match(self) -> None:
        from app.adapters.db.expenses.models import _STATE_VALUES
        from app.domain.expenses.claims import _STATE_VALUES_LOCAL

        assert set(_STATE_VALUES) == set(_STATE_VALUES_LOCAL)

    def test_category_values_match(self) -> None:
        from app.adapters.db.expenses.models import _CATEGORY_VALUES
        from app.domain.expenses.claims import _CATEGORY_VALUES_LOCAL

        assert set(_CATEGORY_VALUES) == set(_CATEGORY_VALUES_LOCAL)

    def test_attachment_kind_values_match(self) -> None:
        from app.adapters.db.expenses.models import _ATTACHMENT_KIND_VALUES
        from app.domain.expenses.claims import _ATTACHMENT_KIND_VALUES_LOCAL

        assert set(_ATTACHMENT_KIND_VALUES) == set(_ATTACHMENT_KIND_VALUES_LOCAL)
