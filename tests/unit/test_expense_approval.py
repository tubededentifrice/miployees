"""Unit tests for :mod:`app.domain.expenses.approval` (cd-9guk).

Layered on top of the worker-side fixtures from
``tests/unit/test_expense_claims.py`` (in-memory SQLite, no migrations,
no tenant filter — pure ORM round-trip + pure-Python DTO validators
+ the authz seam). Covers every acceptance criterion called out in
``cd-9guk``:

* Approve happy path (state, ``decided_by`` / ``decided_at``,
  audit row, event published).
* Approve with inline edits (before/after diff in the audit row,
  fields rewritten on the row, ``had_edits=True`` on the event).
* Approve on a non-submitted claim → ``ClaimNotApprovable``.
* Approve without ``expenses.approve`` → ``ApprovalPermissionDenied``.
* Reject happy path (state, ``decision_note_md``, audit, event).
* Reject with empty ``reason_md`` → DTO ``ValidationError``.
* Reject on a non-submitted claim → ``ClaimNotApprovable``.
* Reimburse happy path (state, ``reimbursed_at`` / ``reimbursed_via``
  / ``reimbursed_by`` set, event published).
* Reimburse on a non-approved claim → ``ClaimNotReimbursable``.
* Reimburse without ``expenses.reimburse`` → ``ReimbursePermissionDenied``.
* Reimburse with future ``paid_at`` → DTO / service rejection.
* ``list_pending`` filtering, pagination, cross-workspace isolation.
* State-machine guards (no skipping; submitted → reimbursed direct
  raises; draft → approved raises).
* Concurrency: a second approver hitting an already-approved claim
  surfaces ``ClaimNotApprovable`` after the first commit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.expenses.repositories import (
    SqlAlchemyCapabilityChecker,
    SqlAlchemyExpensesRepository,
)
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import WorkEngagement, Workspace
from app.domain.expenses import (
    ApprovalEdits,
    ApprovalPermissionDenied,
    ClaimNotApprovable,
    ClaimNotFound,
    ClaimNotReimbursable,
    ExpenseClaimCreate,
    ExpenseClaimView,
    ReimburseBody,
    ReimbursePermissionDenied,
    RejectBody,
    approve_claim,
    list_pending,
    mark_reimbursed,
    reject_claim,
)
from app.domain.expenses import claims as _claims_module
from app.events import (
    ExpenseApproved,
    ExpenseReimbursed,
    ExpenseRejected,
    bus,
)
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)


# ---------------------------------------------------------------------------
# Seam compat shims (cd-0e8i)
# ---------------------------------------------------------------------------
#
# The cd-0e8i refactor flipped :mod:`app.domain.expenses.claims`'s public
# API to ``(repo, checker, ctx, *, ...)``. Approval is still on the old
# session-based API (cd-zoj4 follow-up); these thin wrappers re-create
# the old call shape for the test fixtures so the approval-side
# coverage doesn't have to thread the seam pair through every setup
# helper. The wrappers build the SA pair fresh each call — cheap, and
# matches the per-request shape the production routes use.


def _make_seam_pair(
    session: Session, ctx: WorkspaceContext
) -> tuple[SqlAlchemyExpensesRepository, SqlAlchemyCapabilityChecker]:
    return (
        SqlAlchemyExpensesRepository(session),
        SqlAlchemyCapabilityChecker(session, ctx),
    )


def create_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: ExpenseClaimCreate,
    clock: FrozenClock | None = None,
) -> ExpenseClaimView:
    repo, checker = _make_seam_pair(session, ctx)
    return _claims_module.create_claim(repo, checker, ctx, body=body, clock=clock)


def submit_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    clock: FrozenClock | None = None,
) -> ExpenseClaimView:
    repo, checker = _make_seam_pair(session, ctx)
    return _claims_module.submit_claim(
        repo, checker, ctx, claim_id=claim_id, clock=clock
    )


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/unit/test_expense_claims.py)
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(autouse=True)
def reset_bus() -> Iterator[None]:
    """Drop every subscription between tests so captures don't bleed."""
    yield
    bus._reset_for_tests()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _grant(s: Session, *, workspace_id: str, user_id: str, grant_role: str) -> None:
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()


def _bootstrap_engagement(
    s: Session,
    *,
    workspace_id: str,
    user_id: str,
    kind: str = "payroll",
) -> str:
    eng_id = new_ulid()
    s.add(
        WorkEngagement(
            id=eng_id,
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind=kind,
            supplier_org_id=None,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=_PINNED.date(),
            archived_on=None,
            notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    s.flush()
    return eng_id


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    slug: str = "ws",
    grant_role: ActorGrantRole = "worker",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _create_body(
    *,
    work_engagement_id: str,
    vendor: str = "Acme Hardware",
    purchased_at: datetime = _PURCHASED,
    currency: str = "EUR",
    total_amount_cents: int = 12_50,
    category: str = "supplies",
    property_id: str | None = None,
    note_md: str = "",
) -> ExpenseClaimCreate:
    return ExpenseClaimCreate.model_validate(
        {
            "work_engagement_id": work_engagement_id,
            "vendor": vendor,
            "purchased_at": purchased_at,
            "currency": currency,
            "total_amount_cents": total_amount_cents,
            "category": category,
            "property_id": property_id,
            "note_md": note_md,
        }
    )


@pytest.fixture
def manager_and_worker(
    session: Session, clock: FrozenClock
) -> tuple[WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock]:
    """Workspace with one worker + one manager + one engagement.

    Returns ``(worker_ctx, manager_ctx, worker_id, manager_id, eng_id, clock)``.
    """
    ws_id = _bootstrap_workspace(session, slug="approve-env")
    worker_id = _bootstrap_user(session, email="w@a.com", display_name="W")
    manager_id = _bootstrap_user(session, email="m@a.com", display_name="M")
    _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
    _grant(session, workspace_id=ws_id, user_id=manager_id, grant_role="manager")
    eng_id = _bootstrap_engagement(session, workspace_id=ws_id, user_id=worker_id)
    session.commit()
    worker_ctx = _ctx(
        workspace_id=ws_id, actor_id=worker_id, grant_role="worker", slug="approve-env"
    )
    manager_ctx = _ctx(
        workspace_id=ws_id,
        actor_id=manager_id,
        grant_role="manager",
        slug="approve-env",
    )
    return worker_ctx, manager_ctx, worker_id, manager_id, eng_id, clock


def _create_and_submit(
    session: Session,
    worker_ctx: WorkspaceContext,
    eng_id: str,
    clock: FrozenClock,
    *,
    vendor: str = "Acme Hardware",
    total_amount_cents: int = 12_50,
    category: str = "supplies",
    property_id: str | None = None,
    currency: str = "EUR",
) -> str:
    """Create a claim as the worker and submit it. Returns the claim id."""
    created = create_claim(
        session,
        worker_ctx,
        body=_create_body(
            work_engagement_id=eng_id,
            vendor=vendor,
            total_amount_cents=total_amount_cents,
            category=category,
            property_id=property_id,
            currency=currency,
        ),
        clock=clock,
    )
    submit_claim(session, worker_ctx, claim_id=created.id, clock=clock)
    return created.id


def _audit_rows(session: Session, *, workspace_id: str) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    return list(session.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorTypes:
    def test_not_approvable_is_value_error(self) -> None:
        assert issubclass(ClaimNotApprovable, ValueError)

    def test_not_reimbursable_is_value_error(self) -> None:
        assert issubclass(ClaimNotReimbursable, ValueError)

    def test_approval_permission_denied_is_permission_error(self) -> None:
        assert issubclass(ApprovalPermissionDenied, PermissionError)

    def test_reimburse_permission_denied_is_permission_error(self) -> None:
        assert issubclass(ReimbursePermissionDenied, PermissionError)


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


class TestApprovalEditsDto:
    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalEdits(bogus="x")  # type: ignore[call-arg]

    def test_rejects_zero_amount(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalEdits(total_amount_cents=0)

    def test_rejects_negative_amount(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalEdits(total_amount_cents=-1)

    def test_rejects_naive_purchased_at(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalEdits.model_validate(
                {"purchased_at": datetime(2026, 4, 19, 12, 0, 0)}
            )

    def test_no_engagement_field(self) -> None:
        """The approver cannot reassign the engagement — the DTO has
        no key for it. The ``extra='forbid'`` config rejects an
        attempt to slip it through."""
        with pytest.raises(ValidationError):
            ApprovalEdits.model_validate({"work_engagement_id": "eng_x"})


class TestRejectBodyDto:
    def test_requires_reason(self) -> None:
        with pytest.raises(ValidationError):
            RejectBody.model_validate({})

    def test_rejects_empty_reason(self) -> None:
        """Pydantic-level 422 — the ``min_length=1`` guard fires
        before the service ever sees an empty rejection note."""
        with pytest.raises(ValidationError):
            RejectBody(reason_md="")


class TestReimburseBodyDto:
    def test_requires_via(self) -> None:
        with pytest.raises(ValidationError):
            ReimburseBody.model_validate({})

    def test_rejects_unknown_via(self) -> None:
        with pytest.raises(ValidationError):
            ReimburseBody.model_validate({"via": "crypto"})

    def test_rejects_naive_paid_at(self) -> None:
        with pytest.raises(ValidationError):
            ReimburseBody.model_validate(
                {"via": "cash", "paid_at": datetime(2026, 4, 19, 12, 0, 0)}
            )

    def test_paid_at_optional(self) -> None:
        body = ReimburseBody(via="bank")
        assert body.paid_at is None


# ---------------------------------------------------------------------------
# approve_claim
# ---------------------------------------------------------------------------


class TestApproveClaim:
    def test_approve_happy_path(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, mgr_id, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        view = approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        assert view.state == "approved"
        assert view.decided_by == mgr_id
        assert view.decided_at == _PINNED

    def test_approve_publishes_event(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, worker_id, mgr_id, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        captured: list[ExpenseApproved] = []

        @bus.subscribe(ExpenseApproved)
        def _on_approved(event: ExpenseApproved) -> None:
            captured.append(event)

        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        assert len(captured) == 1
        evt = captured[0]
        assert evt.claim_id == claim_id
        assert evt.work_engagement_id == eng_id
        assert evt.submitter_user_id == worker_id
        assert evt.decided_by_user_id == mgr_id
        assert evt.had_edits is False

    def test_approve_writes_audit_row(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        rows = _audit_rows(session, workspace_id=manager_ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == [
            "expense.claim.created",
            "expense.claim.submitted",
            "expense.claim.approved",
        ]

    def test_approve_with_edits_applies_diff(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(
            session, worker_ctx, eng_id, clock, vendor="Old Vendor"
        )
        captured: list[ExpenseApproved] = []

        @bus.subscribe(ExpenseApproved)
        def _on_approved(event: ExpenseApproved) -> None:
            captured.append(event)

        edits = ApprovalEdits(vendor="Corrected Vendor", total_amount_cents=2_000)
        view = approve_claim(
            session, manager_ctx, claim_id=claim_id, edits=edits, clock=clock
        )
        assert view.vendor == "Corrected Vendor"
        assert view.total_amount_cents == 2_000

        rows = _audit_rows(session, workspace_id=manager_ctx.workspace_id)
        approved_audit = [r for r in rows if r.action == "expense.claim.approved"]
        assert len(approved_audit) == 1
        diff = approved_audit[0].diff
        assert isinstance(diff, dict)
        before = diff["before"]
        after = diff["after"]
        assert isinstance(before, dict)
        assert isinstance(after, dict)
        assert before["vendor"] == "Old Vendor"
        assert after["vendor"] == "Corrected Vendor"
        assert before["total_amount_cents"] == 12_50
        assert after["total_amount_cents"] == 2_000

        assert len(captured) == 1
        assert captured[0].had_edits is True

    def test_approve_with_empty_edits_is_no_op(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """An ``ApprovalEdits()`` with every field omitted leaves the
        row untouched; the event ``had_edits`` flag is ``False``."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        captured: list[ExpenseApproved] = []

        @bus.subscribe(ExpenseApproved)
        def _on(event: ExpenseApproved) -> None:
            captured.append(event)

        approve_claim(
            session,
            manager_ctx,
            claim_id=claim_id,
            edits=ApprovalEdits(),
            clock=clock,
        )
        assert captured[0].had_edits is False

    def test_approve_non_submitted_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        # Draft claim — never submitted.
        created = create_claim(
            session,
            worker_ctx,
            body=_create_body(work_engagement_id=eng_id),
            clock=clock,
        )
        with pytest.raises(ClaimNotApprovable):
            approve_claim(session, manager_ctx, claim_id=created.id, clock=clock)

    def test_approve_already_approved_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        # State is now ``approved`` — a second approve is a 409.
        with pytest.raises(ClaimNotApprovable):
            approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)

    def test_approve_without_capability_rejected(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """A worker grant has no ``expenses.approve`` default — approval
        fails before the state-machine check fires."""
        worker_ctx, _manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        with pytest.raises(ApprovalPermissionDenied):
            approve_claim(session, worker_ctx, claim_id=claim_id, clock=clock)

    def test_approve_unknown_claim_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        _wctx, manager_ctx, _wid, _mid, _eng_id, clock = manager_and_worker
        with pytest.raises(ClaimNotFound):
            approve_claim(
                session, manager_ctx, claim_id="01HW0000000000000000000000", clock=clock
            )


# ---------------------------------------------------------------------------
# reject_claim
# ---------------------------------------------------------------------------


class TestRejectClaim:
    def test_reject_happy_path(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, mgr_id, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        view = reject_claim(
            session,
            manager_ctx,
            claim_id=claim_id,
            reason_md="Receipt unreadable.",
            clock=clock,
        )
        assert view.state == "rejected"
        assert view.decided_by == mgr_id
        assert view.decided_at == _PINNED
        assert view.decision_note_md == "Receipt unreadable."

    def test_reject_publishes_event(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, worker_id, mgr_id, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        captured: list[ExpenseRejected] = []

        @bus.subscribe(ExpenseRejected)
        def _on(event: ExpenseRejected) -> None:
            captured.append(event)

        reject_claim(
            session,
            manager_ctx,
            claim_id=claim_id,
            reason_md="Receipt unreadable.",
            clock=clock,
        )
        assert len(captured) == 1
        evt = captured[0]
        assert evt.claim_id == claim_id
        assert evt.work_engagement_id == eng_id
        assert evt.submitter_user_id == worker_id
        assert evt.decided_by_user_id == mgr_id
        # Reason MUST NOT be on the wire — see ``ExpenseRejected``
        # docstring. The event class only declares the four ID fields,
        # so attribute access on ``reason_md`` would AttributeError;
        # the ``model_dump()`` JSON is the canonical surface.
        assert "reason_md" not in evt.model_dump()

    def test_reject_writes_audit_row(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        reject_claim(
            session,
            manager_ctx,
            claim_id=claim_id,
            reason_md="Out of policy.",
            clock=clock,
        )
        rows = _audit_rows(session, workspace_id=manager_ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert "expense.claim.rejected" in actions

    def test_reject_non_submitted_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        created = create_claim(
            session,
            worker_ctx,
            body=_create_body(work_engagement_id=eng_id),
            clock=clock,
        )
        # Draft, not submitted.
        with pytest.raises(ClaimNotApprovable):
            reject_claim(
                session,
                manager_ctx,
                claim_id=created.id,
                reason_md="No.",
                clock=clock,
            )

    def test_reject_without_capability_rejected(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, _manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        with pytest.raises(ApprovalPermissionDenied):
            reject_claim(
                session,
                worker_ctx,
                claim_id=claim_id,
                reason_md="My own.",
                clock=clock,
            )

    def test_reject_empty_reason_at_service_layer(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """The DTO's ``min_length=1`` already rejects empty reasons,
        but a Python caller bypassing the DTO must still observe the
        rule. The service raises a vanilla ``ValueError`` (router
        maps to 422)."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        with pytest.raises(ValueError, match="non-empty"):
            reject_claim(
                session, manager_ctx, claim_id=claim_id, reason_md="", clock=clock
            )

    def test_reject_whitespace_reason_at_service_layer(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        with pytest.raises(ValueError, match="non-empty"):
            reject_claim(
                session,
                manager_ctx,
                claim_id=claim_id,
                reason_md="   \n  ",
                clock=clock,
            )


# ---------------------------------------------------------------------------
# mark_reimbursed
# ---------------------------------------------------------------------------


class TestMarkReimbursed:
    def test_reimburse_happy_path(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, mgr_id, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)

        view = mark_reimbursed(
            session,
            manager_ctx,
            claim_id=claim_id,
            body=ReimburseBody(via="bank"),
            clock=clock,
        )
        assert view.state == "reimbursed"

        # Read the underlying row to inspect the new columns.
        row = session.get(ExpenseClaim, claim_id)
        assert row is not None
        assert row.reimbursed_via == "bank"
        assert row.reimbursed_by == mgr_id
        assert row.reimbursed_at is not None

    def test_reimburse_with_explicit_paid_at(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)

        paid_at = _PINNED - timedelta(hours=2)
        mark_reimbursed(
            session,
            manager_ctx,
            claim_id=claim_id,
            body=ReimburseBody(via="cash", paid_at=paid_at),
            clock=clock,
        )
        row = session.get(ExpenseClaim, claim_id)
        assert row is not None
        # Reimbursed_at is the supplied paid_at (back-stamped is fine).
        # SQLite's DateTime(timezone=True) strips tzinfo on read; the
        # naive replace mirrors the wall-clock stored in the column.
        assert row.reimbursed_at is not None
        stored = row.reimbursed_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored == paid_at

    def test_reimburse_publishes_event(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, worker_id, mgr_id, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)

        captured: list[ExpenseReimbursed] = []

        @bus.subscribe(ExpenseReimbursed)
        def _on(event: ExpenseReimbursed) -> None:
            captured.append(event)

        mark_reimbursed(
            session,
            manager_ctx,
            claim_id=claim_id,
            body=ReimburseBody(via="card"),
            clock=clock,
        )
        assert len(captured) == 1
        evt = captured[0]
        assert evt.claim_id == claim_id
        assert evt.work_engagement_id == eng_id
        assert evt.submitter_user_id == worker_id
        assert evt.reimbursed_via == "card"
        assert evt.reimbursed_by_user_id == mgr_id

    def test_reimburse_writes_audit_row(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        mark_reimbursed(
            session,
            manager_ctx,
            claim_id=claim_id,
            body=ReimburseBody(via="bank"),
            clock=clock,
        )
        rows = _audit_rows(session, workspace_id=manager_ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert "expense.claim.reimbursed" in actions

    def test_reimburse_non_approved_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        # Submitted, not approved.
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        with pytest.raises(ClaimNotReimbursable):
            mark_reimbursed(
                session,
                manager_ctx,
                claim_id=claim_id,
                body=ReimburseBody(via="bank"),
                clock=clock,
            )

    def test_reimburse_already_reimbursed_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        mark_reimbursed(
            session,
            manager_ctx,
            claim_id=claim_id,
            body=ReimburseBody(via="bank"),
            clock=clock,
        )
        with pytest.raises(ClaimNotReimbursable):
            mark_reimbursed(
                session,
                manager_ctx,
                claim_id=claim_id,
                body=ReimburseBody(via="bank"),
                clock=clock,
            )

    def test_reimburse_without_capability_rejected(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        with pytest.raises(ReimbursePermissionDenied):
            mark_reimbursed(
                session,
                worker_ctx,
                claim_id=claim_id,
                body=ReimburseBody(via="bank"),
                clock=clock,
            )

    def test_reimburse_future_paid_at_rejected(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """A ``paid_at`` more than the skew window in the future is
        a back-dating attempt — the service raises 422."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        future = _PINNED + timedelta(hours=1)
        with pytest.raises(ValueError, match="future"):
            mark_reimbursed(
                session,
                manager_ctx,
                claim_id=claim_id,
                body=ReimburseBody(via="bank", paid_at=future),
                clock=clock,
            )

    def test_reimburse_paid_at_within_skew_accepted(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """A small clock-skew window (~30s ahead) is tolerated so a
        SPA on a slightly fast laptop doesn't surface a confusing
        422 for the "I just paid" submission."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        ahead = _PINNED + timedelta(seconds=30)
        mark_reimbursed(
            session,
            manager_ctx,
            claim_id=claim_id,
            body=ReimburseBody(via="bank", paid_at=ahead),
            clock=clock,
        )
        row = session.get(ExpenseClaim, claim_id)
        assert row is not None
        assert row.reimbursed_at is not None
        stored = row.reimbursed_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored == ahead


# ---------------------------------------------------------------------------
# State-machine guards (no skipping)
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_submitted_to_reimbursed_directly_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """Cannot skip ``approved`` — submitted → reimbursed is a 409."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        with pytest.raises(ClaimNotReimbursable):
            mark_reimbursed(
                session,
                manager_ctx,
                claim_id=claim_id,
                body=ReimburseBody(via="bank"),
                clock=clock,
            )

    def test_draft_to_approved_directly_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """Cannot skip ``submitted`` — draft → approved is a 409."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        created = create_claim(
            session,
            worker_ctx,
            body=_create_body(work_engagement_id=eng_id),
            clock=clock,
        )
        with pytest.raises(ClaimNotApprovable):
            approve_claim(session, manager_ctx, claim_id=created.id, clock=clock)

    def test_rejected_to_approved_blocked(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """Once rejected, the claim is terminal. A re-approve attempt
        is a 409 — the worker must file a fresh claim."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        reject_claim(
            session,
            manager_ctx,
            claim_id=claim_id,
            reason_md="No.",
            clock=clock,
        )
        with pytest.raises(ClaimNotApprovable):
            approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)


# ---------------------------------------------------------------------------
# Post-commit serialisation
# ---------------------------------------------------------------------------


class TestPostCommitSerialisation:
    """In-memory SQLite serialises writes with a whole-database lock,
    so we cannot reproduce a true ``SELECT FOR UPDATE`` race here. The
    row-lock guard in :func:`app.domain.expenses.claims._load_row`
    (``for_update=True``) is unit-tested for ``submit_claim`` against
    the same engine and re-used by the approval service; this class
    pins the post-commit contract: once a manager's UoW closes, every
    subsequent attempt observes the new ``state`` and surfaces
    :class:`ClaimNotApprovable` rather than re-flipping or
    double-publishing the event.
    """

    def test_second_approve_after_first_commit_raises(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        """After manager A commits, manager B's reload sees
        ``state='approved'`` and the state-machine guard fires."""
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=claim_id, clock=clock)
        session.commit()

        # Second approver in the same workspace — separate user, but
        # same authz capability.
        ws_id = manager_ctx.workspace_id
        second_mgr_id = _bootstrap_user(session, email="m2@a.com", display_name="M2")
        _grant(session, workspace_id=ws_id, user_id=second_mgr_id, grant_role="manager")
        session.commit()
        second_ctx = _ctx(
            workspace_id=ws_id,
            actor_id=second_mgr_id,
            grant_role="manager",
            slug="approve-env",
        )
        with pytest.raises(ClaimNotApprovable):
            approve_claim(session, second_ctx, claim_id=claim_id, clock=clock)


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


class TestCrossWorkspaceIsolation:
    """A manager in workspace A cannot mutate a claim from workspace B.

    The ORM tenant filter + the explicit ``workspace_id`` predicate in
    :func:`app.domain.expenses.claims._load_row` together hide rows
    from other tenants — the cross-tenant attempt surfaces as
    :class:`ClaimNotFound`, not as a 403, because the resource simply
    does not exist from the caller's perspective.
    """

    def _two_workspaces(
        self, session: Session, clock: FrozenClock
    ) -> tuple[WorkspaceContext, WorkspaceContext, str]:
        """Bootstrap two workspaces; submit one claim in workspace A.

        Returns ``(manager_a_ctx, manager_b_ctx, claim_in_a_id)``.
        """
        # Workspace A — worker submits a claim, manager A is the
        # would-be victim if isolation broke.
        ws_a = _bootstrap_workspace(session, slug="ws-a")
        worker_a = _bootstrap_user(session, email="wa@x.com", display_name="WA")
        manager_a = _bootstrap_user(session, email="ma@x.com", display_name="MA")
        _grant(session, workspace_id=ws_a, user_id=worker_a, grant_role="worker")
        _grant(session, workspace_id=ws_a, user_id=manager_a, grant_role="manager")
        eng_a = _bootstrap_engagement(session, workspace_id=ws_a, user_id=worker_a)

        # Workspace B — manager B holds ``expenses.approve`` /
        # ``expenses.reimburse`` (default for managers) and tries to
        # reach into workspace A.
        ws_b = _bootstrap_workspace(session, slug="ws-b")
        manager_b = _bootstrap_user(session, email="mb@x.com", display_name="MB")
        _grant(session, workspace_id=ws_b, user_id=manager_b, grant_role="manager")
        session.commit()

        worker_a_ctx = _ctx(
            workspace_id=ws_a,
            actor_id=worker_a,
            grant_role="worker",
            slug="ws-a",
        )
        manager_a_ctx = _ctx(
            workspace_id=ws_a,
            actor_id=manager_a,
            grant_role="manager",
            slug="ws-a",
        )
        manager_b_ctx = _ctx(
            workspace_id=ws_b,
            actor_id=manager_b,
            grant_role="manager",
            slug="ws-b",
        )
        claim_id = _create_and_submit(session, worker_a_ctx, eng_a, clock)
        return manager_a_ctx, manager_b_ctx, claim_id

    def test_approve_cross_workspace_raises_not_found(
        self, session: Session, clock: FrozenClock
    ) -> None:
        _ctx_a, manager_b_ctx, claim_id = self._two_workspaces(session, clock)
        with pytest.raises(ClaimNotFound):
            approve_claim(session, manager_b_ctx, claim_id=claim_id, clock=clock)

    def test_reject_cross_workspace_raises_not_found(
        self, session: Session, clock: FrozenClock
    ) -> None:
        _ctx_a, manager_b_ctx, claim_id = self._two_workspaces(session, clock)
        with pytest.raises(ClaimNotFound):
            reject_claim(
                session,
                manager_b_ctx,
                claim_id=claim_id,
                reason_md="Reaching across tenants.",
                clock=clock,
            )

    def test_reimburse_cross_workspace_raises_not_found(
        self, session: Session, clock: FrozenClock
    ) -> None:
        # Approve first in workspace A so the claim is reimbursable
        # from A's manager's perspective; the cross-tenant attempt
        # should still surface as not-found, not as the state-machine
        # 409.
        manager_a_ctx, manager_b_ctx, claim_id = self._two_workspaces(session, clock)
        approve_claim(session, manager_a_ctx, claim_id=claim_id, clock=clock)
        with pytest.raises(ClaimNotFound):
            mark_reimbursed(
                session,
                manager_b_ctx,
                claim_id=claim_id,
                body=ReimburseBody(via="bank"),
                clock=clock,
            )


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


class TestListPending:
    def test_list_pending_returns_only_submitted(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        # One submitted (kept).
        kept_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        # One draft (excluded).
        create_claim(
            session,
            worker_ctx,
            body=_create_body(work_engagement_id=eng_id),
            clock=clock,
        )
        # One submitted-then-approved (excluded).
        approved_id = _create_and_submit(session, worker_ctx, eng_id, clock)
        approve_claim(session, manager_ctx, claim_id=approved_id, clock=clock)

        listed, cursor = list_pending(session, manager_ctx)
        ids = {v.id for v in listed}
        assert ids == {kept_id}
        assert cursor is None

    def test_list_pending_requires_capability(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, _manager_ctx, _wid, _mid, _eng_id, _clock = manager_and_worker
        with pytest.raises(ApprovalPermissionDenied):
            list_pending(session, worker_ctx)

    def test_list_pending_filters_by_claimant(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, worker_id, _mid, eng_id, clock = manager_and_worker
        claim_id = _create_and_submit(session, worker_ctx, eng_id, clock)

        # Add a second worker in the same workspace and submit a claim.
        ws_id = manager_ctx.workspace_id
        worker2_id = _bootstrap_user(session, email="w2@a.com", display_name="W2")
        _grant(session, workspace_id=ws_id, user_id=worker2_id, grant_role="worker")
        eng2 = _bootstrap_engagement(session, workspace_id=ws_id, user_id=worker2_id)
        session.commit()
        worker2_ctx = _ctx(
            workspace_id=ws_id,
            actor_id=worker2_id,
            grant_role="worker",
            slug="approve-env",
        )
        other_claim_id = _create_and_submit(session, worker2_ctx, eng2, clock)

        # Filter to worker 1 only.
        listed, _ = list_pending(session, manager_ctx, claimant_user_id=worker_id)
        ids = {v.id for v in listed}
        assert ids == {claim_id}
        assert other_claim_id not in ids

    def test_list_pending_filters_by_property(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        prop_id = "01PROPABCDEFGHIJKLMNOPQRST"
        pinned = _create_and_submit(
            session, worker_ctx, eng_id, clock, property_id=prop_id
        )
        unpinned = _create_and_submit(session, worker_ctx, eng_id, clock)

        listed, _ = list_pending(session, manager_ctx, property_id=prop_id)
        ids = {v.id for v in listed}
        assert ids == {pinned}
        assert unpinned not in ids

    def test_list_pending_filters_by_category(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        fuel_id = _create_and_submit(
            session, worker_ctx, eng_id, clock, category="fuel"
        )
        food_id = _create_and_submit(
            session, worker_ctx, eng_id, clock, category="food"
        )
        listed, _ = list_pending(session, manager_ctx, category="fuel")
        ids = {v.id for v in listed}
        assert ids == {fuel_id}
        assert food_id not in ids

    def test_list_pending_pagination_round_trip(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        # Submit five claims, advancing the clock each time so
        # ``submitted_at`` orders deterministically.
        ids: list[str] = []
        for i in range(5):
            clock.set(_PINNED + timedelta(minutes=i))
            ids.append(_create_and_submit(session, worker_ctx, eng_id, clock))

        # Page 1 — limit 2, gets the two newest.
        page1, cursor1 = list_pending(session, manager_ctx, limit=2)
        assert len(page1) == 2
        assert cursor1 is not None
        # Newest first: ids[4], ids[3].
        assert page1[0].id == ids[4]
        assert page1[1].id == ids[3]

        page2, cursor2 = list_pending(session, manager_ctx, limit=2, cursor=cursor1)
        assert len(page2) == 2
        assert cursor2 is not None
        assert page2[0].id == ids[2]
        assert page2[1].id == ids[1]

        page3, cursor3 = list_pending(session, manager_ctx, limit=2, cursor=cursor2)
        assert len(page3) == 1
        assert page3[0].id == ids[0]
        assert cursor3 is None

    def test_list_pending_excludes_cross_workspace(
        self,
        session: Session,
        manager_and_worker: tuple[
            WorkspaceContext, WorkspaceContext, str, str, str, FrozenClock
        ],
    ) -> None:
        worker_ctx, manager_ctx, _wid, _mid, eng_id, clock = manager_and_worker
        kept = _create_and_submit(session, worker_ctx, eng_id, clock)

        # A second workspace with its own submitted claim — the
        # manager of workspace A cannot see it.
        ws_b = _bootstrap_workspace(session, slug="other-ws")
        worker_b = _bootstrap_user(session, email="wb@x.com", display_name="WB")
        _grant(session, workspace_id=ws_b, user_id=worker_b, grant_role="worker")
        eng_b = _bootstrap_engagement(session, workspace_id=ws_b, user_id=worker_b)
        session.commit()
        worker_b_ctx = _ctx(
            workspace_id=ws_b,
            actor_id=worker_b,
            grant_role="worker",
            slug="other-ws",
        )
        _create_and_submit(session, worker_b_ctx, eng_b, clock)

        listed, _ = list_pending(session, manager_ctx)
        ids = {v.id for v in listed}
        assert ids == {kept}
