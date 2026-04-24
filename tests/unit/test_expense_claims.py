"""Unit tests for :mod:`app.domain.expenses.claims` (cd-7rfu).

Exercises the service surface against an in-memory SQLite engine
built via ``Base.metadata.create_all()`` — no alembic, no tenant
filter, just the ORM round-trip + the pure-Python DTO validators and
authz seam.

Covers the acceptance criteria called out in the task brief:

* Create — happy path, cross-tenant 403, cross-user 403, currency
  validation, soft-deleted reload-as-404 invariant.
* Update — partial PATCH, only-while-draft guard, audit per mutation.
* Attach — happy path, 10-cap, mime allow-list, size cap, missing
  blob in storage, draft-only guard, detach-only-while-draft.
* Submit — draft -> submitted, ``submitted_at`` set, event published,
  capability gate, second-submit rejected.
* Cancel — draft (soft-delete + invisible), submitted (rejected
  with cancellation note), approved (rejected by state machine).
* Cross-workspace isolation — a claim in another workspace is
  invisible to get / list.
* Audit — every write produces one row in the same UoW.
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
from app.adapters.db.expenses.models import ExpenseAttachment, ExpenseClaim
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import WorkEngagement, Workspace
from app.domain.expenses import (
    BlobMimeNotAllowed,
    BlobMissing,
    BlobTooLarge,
    ClaimNotEditable,
    ClaimNotFound,
    ClaimPermissionDenied,
    ClaimStateTransitionInvalid,
    CurrencyInvalid,
    ExpenseCategory,
    ExpenseClaimCreate,
    ExpenseClaimUpdate,
    PurchaseDateInFuture,
    TooManyAttachments,
    attach_receipt,
    cancel_claim,
    create_claim,
    detach_receipt,
    get_claim,
    list_for_user,
    list_for_workspace,
    submit_claim,
    update_claim,
)
from app.events import ExpenseSubmitted, bus
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)


# ---------------------------------------------------------------------------
# Fixtures
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
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers (mirror the shape used in test_leave_service.py)
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


@pytest.fixture
def worker_env(
    session: Session, clock: FrozenClock
) -> tuple[WorkspaceContext, str, str, FrozenClock]:
    """Worker w/ workspace + work_engagement. Returns (ctx, user_id, eng_id, clock)."""
    ws_id = _bootstrap_workspace(session, slug="worker-env")
    user_id = _bootstrap_user(session, email="w@example.com", display_name="W")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="worker")
    eng_id = _bootstrap_engagement(session, workspace_id=ws_id, user_id=user_id)
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="worker")
    return ctx, user_id, eng_id, clock


@pytest.fixture
def manager_env(
    session: Session, clock: FrozenClock
) -> tuple[WorkspaceContext, str, str, FrozenClock]:
    """Manager w/ workspace + work_engagement."""
    ws_id = _bootstrap_workspace(session, slug="manager-env")
    user_id = _bootstrap_user(session, email="m@example.com", display_name="M")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="manager")
    eng_id = _bootstrap_engagement(session, workspace_id=ws_id, user_id=user_id)
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="manager")
    return ctx, user_id, eng_id, clock


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _put_blob(storage: InMemoryStorage, *, payload: bytes = b"x") -> str:
    """Push a blob into the in-memory store and return its hash.

    The hash is just a sequence-keyed string — InMemoryStorage doesn't
    re-hash payloads, it stores under the hash the caller asserts.
    The 64-char length matches the production SHA-256 hex.
    """
    import hashlib

    h = hashlib.sha256(payload + new_ulid().encode()).hexdigest()
    import io

    storage.put(h, io.BytesIO(payload), content_type="image/jpeg")
    return h


def _create_body(
    *,
    work_engagement_id: str,
    vendor: str = "Acme Hardware",
    purchased_at: datetime = _PURCHASED,
    currency: str = "EUR",
    total_amount_cents: int = 12_50,
    category: ExpenseCategory = "supplies",
    property_id: str | None = None,
    note_md: str = "",
) -> ExpenseClaimCreate:
    return ExpenseClaimCreate(
        work_engagement_id=work_engagement_id,
        vendor=vendor,
        purchased_at=purchased_at,
        currency=currency,
        total_amount_cents=total_amount_cents,
        category=category,
        property_id=property_id,
        note_md=note_md,
    )


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorTypes:
    def test_not_found_is_lookup_error(self) -> None:
        assert issubclass(ClaimNotFound, LookupError)

    def test_not_editable_is_value_error(self) -> None:
        assert issubclass(ClaimNotEditable, ValueError)

    def test_state_transition_is_value_error(self) -> None:
        assert issubclass(ClaimStateTransitionInvalid, ValueError)

    def test_currency_invalid_is_value_error(self) -> None:
        assert issubclass(CurrencyInvalid, ValueError)

    def test_blob_missing_is_lookup_error(self) -> None:
        assert issubclass(BlobMissing, LookupError)

    def test_blob_mime_is_value_error(self) -> None:
        assert issubclass(BlobMimeNotAllowed, ValueError)

    def test_blob_too_large_is_value_error(self) -> None:
        assert issubclass(BlobTooLarge, ValueError)

    def test_too_many_attachments_is_value_error(self) -> None:
        assert issubclass(TooManyAttachments, ValueError)

    def test_permission_denied_is_permission_error(self) -> None:
        assert issubclass(ClaimPermissionDenied, PermissionError)


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


class TestExpenseClaimCreateDto:
    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimCreate(
                work_engagement_id="eng_1",
                vendor="V",
                purchased_at=_PURCHASED,
                currency="EUR",
                total_amount_cents=100,
                category="supplies",
                bogus="yes",  # type: ignore[call-arg]
            )

    def test_rejects_negative_amount(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimCreate(
                work_engagement_id="eng_1",
                vendor="V",
                purchased_at=_PURCHASED,
                currency="EUR",
                total_amount_cents=-1,
                category="supplies",
            )

    def test_rejects_zero_amount(self) -> None:
        """A zero-cents claim is nonsensical — the worker is asking
        for nothing. The DTO rejects on the boundary."""
        with pytest.raises(ValidationError):
            ExpenseClaimCreate(
                work_engagement_id="eng_1",
                vendor="V",
                purchased_at=_PURCHASED,
                currency="EUR",
                total_amount_cents=0,
                category="supplies",
            )

    def test_rejects_naive_purchased_at(self) -> None:
        """A naive timestamp would silently shift the receipt date
        across timezones — reject at the boundary."""
        with pytest.raises(ValidationError):
            ExpenseClaimCreate.model_validate(
                {
                    "work_engagement_id": "eng_1",
                    "vendor": "V",
                    "purchased_at": datetime(2026, 4, 19, 12, 0, 0),
                    "currency": "EUR",
                    "total_amount_cents": 100,
                    "category": "supplies",
                }
            )

    def test_rejects_short_currency(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimCreate(
                work_engagement_id="eng_1",
                vendor="V",
                purchased_at=_PURCHASED,
                currency="EU",
                total_amount_cents=100,
                category="supplies",
            )

    def test_rejects_bad_category_literal(self) -> None:
        # Build via ``model_validate`` so mypy doesn't complain about the
        # bad literal — the rejection happens at runtime in the
        # validator, which is what we're exercising.
        with pytest.raises(ValidationError):
            ExpenseClaimCreate.model_validate(
                {
                    "work_engagement_id": "eng_1",
                    "vendor": "V",
                    "purchased_at": _PURCHASED,
                    "currency": "EUR",
                    "total_amount_cents": 100,
                    "category": "luxury",
                }
            )

    def test_note_md_defaults_to_empty(self) -> None:
        body = ExpenseClaimCreate(
            work_engagement_id="eng_1",
            vendor="V",
            purchased_at=_PURCHASED,
            currency="EUR",
            total_amount_cents=100,
            category="supplies",
        )
        assert body.note_md == ""


class TestExpenseClaimUpdateDto:
    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimUpdate(
                bogus="x",  # type: ignore[call-arg]
            )

    def test_all_fields_optional(self) -> None:
        body = ExpenseClaimUpdate()
        assert body.model_dump(exclude_unset=True) == {}

    def test_rejects_zero_amount_on_update(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimUpdate(total_amount_cents=0)

    def test_rejects_negative_amount_on_update(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimUpdate(total_amount_cents=-1)

    def test_rejects_naive_purchased_at_on_update(self) -> None:
        with pytest.raises(ValidationError):
            ExpenseClaimUpdate.model_validate(
                {"purchased_at": datetime(2026, 4, 19, 12, 0, 0)}
            )


# ---------------------------------------------------------------------------
# create_claim
# ---------------------------------------------------------------------------


class TestCreateClaim:
    def test_worker_creates_own_claim(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        assert view.work_engagement_id == eng_id
        assert view.state == "draft"
        assert view.currency == "EUR"
        assert view.total_amount_cents == 12_50
        assert view.submitted_at is None
        assert view.deleted_at is None
        assert view.attachments == ()

    def test_currency_is_uppercased(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, currency="eur"),
            clock=clock,
        )
        assert view.currency == "EUR"

    def test_unknown_currency_raises(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        with pytest.raises(CurrencyInvalid):
            create_claim(
                session,
                ctx,
                body=_create_body(work_engagement_id=eng_id, currency="ZZZ"),
                clock=clock,
            )

    def test_cross_user_engagement_rejected(
        self,
        session: Session,
    ) -> None:
        """Worker A cannot create a claim against worker B's engagement."""
        ws_id = _bootstrap_workspace(session, slug="cross-user")
        a_id = _bootstrap_user(session, email="a@x.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@x.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        eng_b = _bootstrap_engagement(session, workspace_id=ws_id, user_id=b_id)
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        with pytest.raises(ClaimPermissionDenied):
            create_claim(
                session,
                ctx_a,
                body=_create_body(work_engagement_id=eng_b),
                clock=FrozenClock(_PINNED),
            )

    def test_cross_workspace_engagement_rejected(
        self,
        session: Session,
    ) -> None:
        """An engagement in another workspace is invisible to the caller."""
        ws_a = _bootstrap_workspace(session, slug="iso-a")
        ws_b = _bootstrap_workspace(session, slug="iso-b")
        user_a = _bootstrap_user(session, email="a@i.com", display_name="A")
        user_b = _bootstrap_user(session, email="b@i.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        # Engagement lives in workspace B.
        eng_b = _bootstrap_engagement(session, workspace_id=ws_b, user_id=user_b)
        session.commit()
        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")

        with pytest.raises(ClaimPermissionDenied):
            create_claim(
                session,
                ctx_a,
                body=_create_body(work_engagement_id=eng_b),
                clock=FrozenClock(_PINNED),
            )

    def test_create_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == ["expense.claim.created"]
        assert rows[0].entity_id == view.id
        assert "after" in rows[0].diff

    def test_future_purchased_at_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """A receipt cannot exist before the purchase happens."""
        ctx, _user_id, eng_id, clock = worker_env
        future = _PINNED + timedelta(hours=1)
        with pytest.raises(PurchaseDateInFuture):
            create_claim(
                session,
                ctx,
                body=_create_body(work_engagement_id=eng_id, purchased_at=future),
                clock=clock,
            )

    def test_purchased_at_within_skew_accepted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """A small clock-skew artefact (~30s ahead) is tolerated so a
        SPA on a slightly fast laptop doesn't surface a confusing 422
        for the "I just paid" submission."""
        ctx, _user_id, eng_id, clock = worker_env
        ahead = _PINNED + timedelta(seconds=30)
        view = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, purchased_at=ahead),
            clock=clock,
        )
        assert view.purchased_at == ahead

    def test_currency_inr_accepted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """Real-world coverage: India is a major household-manager
        market — INR must round-trip."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, currency="INR"),
            clock=clock,
        )
        assert view.currency == "INR"

    def test_currency_aed_accepted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """GCC coverage: AED is the default for villa rentals in the
        UAE; missing it would be a real-world MISSING."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, currency="AED"),
            clock=clock,
        )
        assert view.currency == "AED"

    def test_currency_brl_accepted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """LATAM coverage: BRL is the largest LATAM economy."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, currency="BRL"),
            clock=clock,
        )
        assert view.currency == "BRL"

    def test_currency_kwd_accepted_three_decimal(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """Three-decimal-minor-unit currency: KWD divides by 1000.
        The allow-list must include it so the §02 "Money" contract
        ('integer cents respect minor units') stays honoured."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, currency="KWD"),
            clock=clock,
        )
        assert view.currency == "KWD"


# ---------------------------------------------------------------------------
# update_claim
# ---------------------------------------------------------------------------


class TestUpdateClaim:
    def test_partial_update_changes_only_supplied_fields(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, vendor="Old", note_md="hi"),
            clock=clock,
        )
        edited = update_claim(
            session,
            ctx,
            claim_id=created.id,
            body=ExpenseClaimUpdate(vendor="New", total_amount_cents=999),
            clock=clock,
        )
        assert edited.vendor == "New"
        assert edited.total_amount_cents == 999
        # Untouched fields preserved.
        assert edited.currency == created.currency
        assert edited.note_md == "hi"
        assert edited.category == created.category

    def test_update_rejected_on_submitted_claim(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        with pytest.raises(ClaimNotEditable):
            update_claim(
                session,
                ctx,
                claim_id=created.id,
                body=ExpenseClaimUpdate(vendor="Whoops"),
                clock=clock,
            )

    def test_update_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        update_claim(
            session,
            ctx,
            claim_id=created.id,
            body=ExpenseClaimUpdate(vendor="V2"),
            clock=clock,
        )
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == ["expense.claim.created", "expense.claim.updated"]

    def test_no_op_update_skips_audit(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        update_claim(
            session,
            ctx,
            claim_id=created.id,
            body=ExpenseClaimUpdate(),  # empty PATCH
            clock=clock,
        )
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == ["expense.claim.created"]

    def test_update_invalid_currency(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        with pytest.raises(CurrencyInvalid):
            update_claim(
                session,
                ctx,
                claim_id=created.id,
                body=ExpenseClaimUpdate(currency="ZZZ"),
                clock=clock,
            )

    def test_update_future_purchased_at_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        """Editing a draft can't push ``purchased_at`` into the
        future either — same guard as ``create_claim``."""
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        future = _PINNED + timedelta(hours=1)
        with pytest.raises(PurchaseDateInFuture):
            update_claim(
                session,
                ctx,
                claim_id=created.id,
                body=ExpenseClaimUpdate(purchased_at=future),
                clock=clock,
            )

    def test_peer_worker_cannot_update(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="peer-upd")
        a_id = _bootstrap_user(session, email="a@u.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@u.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        eng_a = _bootstrap_engagement(session, workspace_id=ws_id, user_id=a_id)
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)
        created = create_claim(
            session, ctx_a, body=_create_body(work_engagement_id=eng_a), clock=clock
        )
        with pytest.raises(ClaimPermissionDenied):
            update_claim(
                session,
                ctx_b,
                claim_id=created.id,
                body=ExpenseClaimUpdate(vendor="X"),
                clock=clock,
            )


# ---------------------------------------------------------------------------
# attach_receipt / detach_receipt
# ---------------------------------------------------------------------------


class TestAttachReceipt:
    def test_attach_happy_path(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        view = attach_receipt(
            session,
            ctx,
            claim_id=created.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=1024,
            storage=storage,
            clock=clock,
        )
        assert view.blob_hash == h
        assert view.kind == "receipt"
        # Round-trip via the claim view: attachments tuple now has one row.
        reloaded = get_claim(session, ctx, claim_id=created.id)
        assert len(reloaded.attachments) == 1
        assert reloaded.attachments[0].id == view.id

    def test_cap_at_ten_attachments(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        for _ in range(10):
            h = _put_blob(storage)
            attach_receipt(
                session,
                ctx,
                claim_id=created.id,
                blob_hash=h,
                content_type="image/jpeg",
                size_bytes=1024,
                storage=storage,
                clock=clock,
            )
        h11 = _put_blob(storage)
        with pytest.raises(TooManyAttachments):
            attach_receipt(
                session,
                ctx,
                claim_id=created.id,
                blob_hash=h11,
                content_type="image/jpeg",
                size_bytes=1024,
                storage=storage,
                clock=clock,
            )

    def test_bad_mime_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        with pytest.raises(BlobMimeNotAllowed):
            attach_receipt(
                session,
                ctx,
                claim_id=created.id,
                blob_hash=h,
                content_type="image/svg+xml",
                size_bytes=1024,
                storage=storage,
                clock=clock,
            )

    def test_oversized_blob_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        with pytest.raises(BlobTooLarge):
            attach_receipt(
                session,
                ctx,
                claim_id=created.id,
                blob_hash=h,
                content_type="image/jpeg",
                size_bytes=11 * 1024 * 1024,
                storage=storage,
                clock=clock,
            )

    def test_blob_missing_in_storage(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        with pytest.raises(BlobMissing):
            attach_receipt(
                session,
                ctx,
                claim_id=created.id,
                blob_hash="0" * 64,
                content_type="image/jpeg",
                size_bytes=1024,
                storage=storage,
                clock=clock,
            )

    def test_attach_on_submitted_claim_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        h = _put_blob(storage)
        with pytest.raises(ClaimNotEditable):
            attach_receipt(
                session,
                ctx,
                claim_id=created.id,
                blob_hash=h,
                content_type="image/jpeg",
                size_bytes=1024,
                storage=storage,
                clock=clock,
            )

    def test_attach_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        attach_receipt(
            session,
            ctx,
            claim_id=created.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=1024,
            storage=storage,
            clock=clock,
        )
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == [
            "expense.claim.created",
            "expense.claim.receipt_attached",
        ]


class TestDetachReceipt:
    def test_detach_happy_path(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        att = attach_receipt(
            session,
            ctx,
            claim_id=created.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=1024,
            storage=storage,
            clock=clock,
        )
        detach_receipt(
            session, ctx, claim_id=created.id, attachment_id=att.id, clock=clock
        )
        # Row is gone from storage.
        remaining = session.scalars(
            select(ExpenseAttachment).where(
                ExpenseAttachment.workspace_id == ctx.workspace_id
            )
        ).all()
        assert remaining == []

    def test_detach_on_submitted_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        att = attach_receipt(
            session,
            ctx,
            claim_id=created.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=1024,
            storage=storage,
            clock=clock,
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        with pytest.raises(ClaimNotEditable):
            detach_receipt(
                session,
                ctx,
                claim_id=created.id,
                attachment_id=att.id,
                clock=clock,
            )

    def test_detach_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        att = attach_receipt(
            session,
            ctx,
            claim_id=created.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=1024,
            storage=storage,
            clock=clock,
        )
        detach_receipt(
            session, ctx, claim_id=created.id, attachment_id=att.id, clock=clock
        )
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions[-1] == "expense.claim.receipt_detached"


# ---------------------------------------------------------------------------
# submit_claim
# ---------------------------------------------------------------------------


class TestSubmitClaim:
    def test_draft_to_submitted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        view = submit_claim(session, ctx, claim_id=created.id, clock=clock)
        assert view.state == "submitted"
        assert view.submitted_at == _PINNED

    def test_submit_publishes_event(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session,
            ctx,
            body=_create_body(work_engagement_id=eng_id, currency="usd"),
            clock=clock,
        )
        captured: list[ExpenseSubmitted] = []

        @bus.subscribe(ExpenseSubmitted)
        def _on_submit(event: ExpenseSubmitted) -> None:
            captured.append(event)

        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        assert len(captured) == 1
        evt = captured[0]
        assert evt.claim_id == created.id
        assert evt.work_engagement_id == eng_id
        assert evt.submitter_user_id == ctx.actor_id
        assert evt.currency == "USD"
        assert evt.total_amount_cents == 12_50

    def test_double_submit_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        with pytest.raises(ClaimStateTransitionInvalid):
            submit_claim(session, ctx, claim_id=created.id, clock=clock)

    def test_submit_without_capability_rejected(
        self,
        session: Session,
    ) -> None:
        """A guest grant has no ``expenses.submit`` default — submission fails."""
        ws_id = _bootstrap_workspace(session, slug="no-cap")
        user_id = _bootstrap_user(session, email="g@x.com", display_name="G")
        # Guest grant — ``expenses.submit`` default_allow does NOT include
        # ``all_clients`` / ``all_guests``, so the resolver denies.
        _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="guest")
        eng_id = _bootstrap_engagement(session, workspace_id=ws_id, user_id=user_id)
        session.commit()

        ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="guest")
        clock = FrozenClock(_PINNED)
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        with pytest.raises(ClaimPermissionDenied):
            submit_claim(session, ctx, claim_id=created.id, clock=clock)

    def test_submit_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == ["expense.claim.created", "expense.claim.submitted"]


# ---------------------------------------------------------------------------
# cancel_claim
# ---------------------------------------------------------------------------


class TestCancelClaim:
    def test_cancel_draft_soft_deletes(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        view = cancel_claim(session, ctx, claim_id=created.id, clock=clock)
        assert view.deleted_at == _PINNED
        # Draft state preserved on the row — ``deleted_at`` is the
        # cancellation marker.
        assert view.state == "draft"

        # Subsequent reads collapse to NotFound.
        with pytest.raises(ClaimNotFound):
            get_claim(session, ctx, claim_id=created.id)

        listed, _cursor = list_for_user(session, ctx)
        assert listed == []

    def test_cancel_submitted_transitions_to_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        view = cancel_claim(session, ctx, claim_id=created.id, clock=clock)
        assert view.state == "rejected"
        assert view.decided_by == user_id
        assert view.decision_note_md == "cancelled by requester"

    def test_cancel_submitted_appends_reason(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        submit_claim(session, ctx, claim_id=created.id, clock=clock)
        view = cancel_claim(
            session,
            ctx,
            claim_id=created.id,
            reason_md="needed to fix the amount",
            clock=clock,
        )
        assert view.decision_note_md == (
            "cancelled by requester: needed to fix the amount"
        )

    def test_cancel_approved_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        # Flip state directly — cd-9guk owns the approval transition.
        row = session.get(ExpenseClaim, created.id)
        assert row is not None
        row.state = "approved"
        session.flush()
        with pytest.raises(ClaimStateTransitionInvalid):
            cancel_claim(session, ctx, claim_id=created.id, clock=clock)

    def test_cancel_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        created = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        cancel_claim(session, ctx, claim_id=created.id, clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "expense_claim"]
        assert actions == ["expense.claim.created", "expense.claim.cancelled"]


# ---------------------------------------------------------------------------
# Listing + tenant isolation
# ---------------------------------------------------------------------------


class TestListing:
    def test_list_for_user_default_self(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        listed, cursor = list_for_user(session, ctx)
        assert len(listed) == 1
        assert cursor is None

    def test_list_for_user_excludes_soft_deleted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        a = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        b = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        cancel_claim(session, ctx, claim_id=a.id, clock=clock)
        listed, _ = list_for_user(session, ctx)
        assert [v.id for v in listed] == [b.id]

    def test_list_for_user_other_requires_capability(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="list-x")
        a_id = _bootstrap_user(session, email="a@l.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@l.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        with pytest.raises(ClaimPermissionDenied):
            list_for_user(session, ctx_a, user_id=b_id)

    def test_list_for_workspace_requires_manager(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
    ) -> None:
        ctx, *_ = worker_env
        with pytest.raises(ClaimPermissionDenied):
            list_for_workspace(session, ctx)

    def test_list_for_workspace_manager_sees_queue(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="queue-m")
        worker_id = _bootstrap_user(session, email="w@q.com", display_name="W")
        mgr_id = _bootstrap_user(session, email="m@q.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        eng = _bootstrap_engagement(session, workspace_id=ws_id, user_id=worker_id)
        session.commit()

        clock = FrozenClock(_PINNED)
        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")

        create_claim(
            session,
            ctx_worker,
            body=_create_body(work_engagement_id=eng),
            clock=clock,
        )
        listed, _ = list_for_workspace(session, ctx_mgr)
        assert len(listed) == 1
        assert listed[0].work_engagement_id == eng


class TestTenantIsolation:
    def test_cross_workspace_get_is_404(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="iso-a2")
        ws_b = _bootstrap_workspace(session, slug="iso-b2")
        user_a = _bootstrap_user(session, email="a@i2.com", display_name="A")
        user_b = _bootstrap_user(session, email="b@i2.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        eng_a = _bootstrap_engagement(session, workspace_id=ws_a, user_id=user_a)
        session.commit()

        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        clock = FrozenClock(_PINNED)
        created = create_claim(
            session, ctx_a, body=_create_body(work_engagement_id=eng_a), clock=clock
        )
        # Workspace B cannot see workspace A's claim.
        with pytest.raises(ClaimNotFound):
            get_claim(session, ctx_b, claim_id=created.id)

    def test_list_is_workspace_scoped(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="list-a2")
        ws_b = _bootstrap_workspace(session, slug="list-b2")
        user_a = _bootstrap_user(session, email="a@l2.com", display_name="A")
        user_b = _bootstrap_user(session, email="b@l2.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        eng_a = _bootstrap_engagement(session, workspace_id=ws_a, user_id=user_a)
        session.commit()

        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        clock = FrozenClock(_PINNED)
        create_claim(
            session, ctx_a, body=_create_body(work_engagement_id=eng_a), clock=clock
        )

        listed_a, _ = list_for_user(session, ctx_a)
        listed_b, _ = list_for_user(session, ctx_b)
        assert len(listed_a) == 1
        assert listed_b == []


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _audit_rows(session: Session, *, workspace_id: str) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    return list(session.scalars(stmt).all())
