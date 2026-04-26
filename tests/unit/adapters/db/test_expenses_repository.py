"""Unit tests for :class:`SqlAlchemyExpensesRepository` (cd-v3jp).

Exercises the SA-backed concretion of the cd-v3jp
:class:`~app.domain.expenses.ports.ExpensesRepository` Protocol over
an in-memory SQLite session:

* engagement reads (``get_engagement`` / ``get_engagement_user_ids``);
* claim CRUD (``get_claim`` / ``insert_claim`` / ``update_claim_fields``);
* state transitions (``mark_claim_submitted`` / ``mark_claim_approved`` /
  ``mark_claim_rejected`` / ``mark_claim_reimbursed`` /
  ``mark_claim_deleted``);
* claim listing (``list_claims_for_user`` / ``list_claims_for_workspace``
  / ``list_pending_claims`` / ``list_pending_reimbursement_claims``)
  including cursor walks, soft-delete exclusion, and the per-filter
  predicates on the manager queue (claimant / property / category);
* attachment CRUD (``list_attachments_for_claim`` / ``get_attachment``
  / ``insert_attachment`` / ``delete_attachment``);
* identity bulk read (``get_user_display_names``);
* LLM-usage write (``insert_llm_usage``);
* SQLite tzinfo round-trip on every datetime column;
* ``update_claim_fields`` rejects unknown keys; missing-claim mutations
  raise ``RuntimeError`` (caller-gate-breach surfaces loudly);
* soft-deleted claims are invisible to mutation paths
  (``_load_claim`` filters ``deleted_at IS NULL``);
* ``get_claim(for_update=True)`` round-trips on SQLite (the dialect
  drops the lock clause silently).

Mirrors the cd-2upg ``test_user_leave.py`` adapter-shape pattern.
Schema-shape coverage of the underlying tables lives in
``tests/unit/test_db_expenses.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.expenses.repositories import (
    SqlAlchemyExpensesRepository,
)
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.workspace.models import WorkEngagement, Workspace
from app.domain.expenses.ports import PendingClaimsCursor

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)
_WORKSPACE_ID = "01HWA00000000000000000WS01"
_OTHER_WORKSPACE_ID = "01HWA00000000000000000WS02"
_USER_ID = "01HWA00000000000000000USR1"
_OTHER_USER_ID = "01HWA00000000000000000USR2"
_ENGAGEMENT_ID = "01HWA00000000000000000ENG1"
_OTHER_ENGAGEMENT_ID = "01HWA00000000000000000ENG2"


# ---------------------------------------------------------------------------
# Engine / session fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Walk the adapter packages so cross-package FKs resolve."""
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
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def repo(session: Session) -> SqlAlchemyExpensesRepository:
    return SqlAlchemyExpensesRepository(session)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_workspace(session: Session, *, workspace_id: str = _WORKSPACE_ID) -> None:
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-4:]}",
            name=f"WS {workspace_id[-4:]}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()


def _seed_user(
    session: Session,
    *,
    user_id: str = _USER_ID,
    email: str = "alice@example.com",
    display_name: str = "Alice",
) -> None:
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=email.lower(),
            display_name=display_name,
            locale=None,
            timezone=None,
            created_at=_PINNED,
        )
    )
    session.flush()


def _seed_engagement(
    session: Session,
    *,
    engagement_id: str = _ENGAGEMENT_ID,
    workspace_id: str = _WORKSPACE_ID,
    user_id: str = _USER_ID,
) -> None:
    session.add(
        WorkEngagement(
            id=engagement_id,
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind="payroll",
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
    session.flush()


def _seed_claim(
    repo: SqlAlchemyExpensesRepository,
    *,
    claim_id: str,
    workspace_id: str = _WORKSPACE_ID,
    engagement_id: str = _ENGAGEMENT_ID,
    currency: str = "EUR",
    amount: int = 12_50,
    state: str = "draft",
    purchased_at: datetime = _PURCHASED,
    submitted_at: datetime | None = None,
    deleted_at: datetime | None = None,
    property_id: str | None = None,
) -> None:
    """Insert a claim row directly via the ORM (the repo's ``insert_claim`` only
    builds drafts)."""
    repo.session.add(
        ExpenseClaim(
            id=claim_id,
            workspace_id=workspace_id,
            work_engagement_id=engagement_id,
            vendor="Acme",
            purchased_at=purchased_at,
            currency=currency,
            total_amount_cents=amount,
            category="supplies",
            property_id=property_id,
            note_md="",
            state=state,
            submitted_at=submitted_at,
            decided_by=None,
            decided_at=None,
            decision_note_md=None,
            created_at=_PINNED,
            deleted_at=deleted_at,
        )
    )
    repo.session.flush()


def _bootstrap(session: Session) -> None:
    """Seed the standard workspace + user + engagement triple."""
    _seed_workspace(session)
    _seed_user(session)
    _seed_engagement(session)


# ---------------------------------------------------------------------------
# session passthrough
# ---------------------------------------------------------------------------


class TestSessionAccessor:
    def test_session_returns_underlying_session(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        assert repo.session is session


# ---------------------------------------------------------------------------
# Engagement reads
# ---------------------------------------------------------------------------


class TestGetEngagement:
    def test_returns_projection_for_seeded_engagement(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        row = repo.get_engagement(
            workspace_id=_WORKSPACE_ID, engagement_id=_ENGAGEMENT_ID
        )
        assert row is not None
        assert row.id == _ENGAGEMENT_ID
        assert row.workspace_id == _WORKSPACE_ID
        assert row.user_id == _USER_ID

    def test_returns_none_for_missing_engagement(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        assert (
            repo.get_engagement(workspace_id=_WORKSPACE_ID, engagement_id="missing")
            is None
        )

    def test_cross_workspace_engagement_is_invisible(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_workspace(session, workspace_id=_OTHER_WORKSPACE_ID)
        # Same id, but in another workspace — must not surface here.
        assert (
            repo.get_engagement(
                workspace_id=_OTHER_WORKSPACE_ID, engagement_id=_ENGAGEMENT_ID
            )
            is None
        )


class TestGetEngagementUserIds:
    def test_empty_input_returns_empty_dict(
        self, repo: SqlAlchemyExpensesRepository
    ) -> None:
        assert (
            repo.get_engagement_user_ids(workspace_id=_WORKSPACE_ID, engagement_ids=[])
            == {}
        )

    def test_resolves_multiple_engagements(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_user(session, user_id=_OTHER_USER_ID, email="b@e.c", display_name="B")
        _seed_engagement(
            session, engagement_id=_OTHER_ENGAGEMENT_ID, user_id=_OTHER_USER_ID
        )
        result = repo.get_engagement_user_ids(
            workspace_id=_WORKSPACE_ID,
            engagement_ids=[_ENGAGEMENT_ID, _OTHER_ENGAGEMENT_ID, "missing"],
        )
        assert result == {
            _ENGAGEMENT_ID: _USER_ID,
            _OTHER_ENGAGEMENT_ID: _OTHER_USER_ID,
        }


# ---------------------------------------------------------------------------
# Claim CRUD
# ---------------------------------------------------------------------------


class TestInsertClaim:
    def test_inserts_draft_with_nulled_decision_columns(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        row = repo.insert_claim(
            claim_id="01HWA00000000000000CLM1",
            workspace_id=_WORKSPACE_ID,
            work_engagement_id=_ENGAGEMENT_ID,
            vendor="Acme",
            purchased_at=_PURCHASED,
            currency="EUR",
            total_amount_cents=99_50,
            category="supplies",
            property_id=None,
            note_md="",
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000CLM1"
        assert row.state == "draft"
        assert row.submitted_at is None
        assert row.decided_by is None
        assert row.deleted_at is None
        # tzinfo normalised on the return path.
        assert row.created_at.tzinfo is UTC
        assert row.purchased_at.tzinfo is UTC


class TestGetClaim:
    def test_returns_projection_for_live_claim(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        row = repo.get_claim(workspace_id=_WORKSPACE_ID, claim_id="C1")
        assert row is not None
        assert row.id == "C1"
        assert row.state == "draft"

    def test_hides_soft_deleted_by_default(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", deleted_at=_PINNED)
        assert repo.get_claim(workspace_id=_WORKSPACE_ID, claim_id="C1") is None

    def test_include_deleted_surfaces_soft_deleted_row(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", deleted_at=_PINNED)
        row = repo.get_claim(
            workspace_id=_WORKSPACE_ID, claim_id="C1", include_deleted=True
        )
        assert row is not None
        assert row.deleted_at is not None
        assert row.deleted_at.tzinfo is UTC

    def test_cross_workspace_returns_none(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        _seed_workspace(session, workspace_id=_OTHER_WORKSPACE_ID)
        assert repo.get_claim(workspace_id=_OTHER_WORKSPACE_ID, claim_id="C1") is None

    def test_for_update_clause_does_not_crash_on_sqlite(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        # The SQLite dialect silently drops ``SELECT ... FOR UPDATE``;
        # this test pins the contract that the seam still returns the
        # row on a backend without lock support.
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        row = repo.get_claim(workspace_id=_WORKSPACE_ID, claim_id="C1", for_update=True)
        assert row is not None
        assert row.id == "C1"


class TestUpdateClaimFields:
    def test_rewrites_supplied_columns(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        row = repo.update_claim_fields(
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            fields={"vendor": "New", "total_amount_cents": 9999},
        )
        assert row.vendor == "New"
        assert row.total_amount_cents == 9999
        # Untouched.
        assert row.currency == "EUR"

    def test_empty_fields_is_noop_returns_current_view(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        row = repo.update_claim_fields(
            workspace_id=_WORKSPACE_ID, claim_id="C1", fields={}
        )
        assert row.vendor == "Acme"

    def test_rejects_unknown_field_name(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        with pytest.raises(KeyError):
            repo.update_claim_fields(
                workspace_id=_WORKSPACE_ID,
                claim_id="C1",
                fields={"bogus_column": "x"},
            )

    def test_accepts_llm_autofill_json_payload(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        payload = {"vendor": "X", "amount_cents": 42}
        row = repo.update_claim_fields(
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            fields={
                "llm_autofill_json": payload,
                "autofill_confidence_overall": Decimal("0.92"),
            },
        )
        assert row.llm_autofill_json == payload
        assert row.autofill_confidence_overall == Decimal("0.92")


class TestStateTransitions:
    def test_mark_submitted_stamps_state_and_submitted_at(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        row = repo.mark_claim_submitted(
            workspace_id=_WORKSPACE_ID, claim_id="C1", submitted_at=_PINNED
        )
        assert row.state == "submitted"
        assert row.submitted_at is not None
        assert row.submitted_at.tzinfo is UTC

    def test_mark_approved_stamps_decision_pair(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="submitted", submitted_at=_PINNED)
        row = repo.mark_claim_approved(
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            decided_by=_USER_ID,
            decided_at=_PINNED,
        )
        assert row.state == "approved"
        assert row.decided_by == _USER_ID
        assert row.decided_at is not None

    def test_mark_rejected_stamps_decision_note(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="submitted", submitted_at=_PINNED)
        row = repo.mark_claim_rejected(
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            decided_by=_USER_ID,
            decided_at=_PINNED,
            decision_note_md="not approved",
        )
        assert row.state == "rejected"
        assert row.decision_note_md == "not approved"

    def test_mark_reimbursed_stamps_triplet(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="approved")
        row = repo.mark_claim_reimbursed(
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            reimbursed_at=_PINNED,
            reimbursed_via="bank",
            reimbursed_by=_USER_ID,
        )
        assert row.state == "reimbursed"
        assert row.reimbursed_via == "bank"
        assert row.reimbursed_by == _USER_ID
        assert row.reimbursed_at is not None
        assert row.reimbursed_at.tzinfo is UTC

    def test_mark_deleted_stamps_deleted_at_only(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        row = repo.mark_claim_deleted(
            workspace_id=_WORKSPACE_ID, claim_id="C1", deleted_at=_PINNED
        )
        # The state column does NOT change — only ``deleted_at`` flips.
        assert row.state == "draft"
        assert row.deleted_at is not None

    def test_mark_submitted_on_missing_claim_raises_runtime_error(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        # Mid-UoW: caller's gate via :meth:`get_claim` succeeded, but a
        # programming error skipped it — the seam fails loud rather
        # than silently no-op.
        _bootstrap(session)
        with pytest.raises(RuntimeError, match="not found"):
            repo.mark_claim_submitted(
                workspace_id=_WORKSPACE_ID,
                claim_id="missing",
                submitted_at=_PINNED,
            )

    def test_update_claim_fields_on_missing_claim_raises_runtime_error(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        with pytest.raises(RuntimeError, match="not found"):
            repo.update_claim_fields(
                workspace_id=_WORKSPACE_ID,
                claim_id="missing",
                fields={"vendor": "X"},
            )

    def test_load_claim_skips_soft_deleted_for_mutation(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        # Defence-in-depth: a caller that bypasses the
        # :meth:`get_claim` gate must not be able to silently mutate a
        # soft-deleted claim. ``_load_claim`` filters
        # ``deleted_at IS NULL`` so the mutation raises instead.
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", deleted_at=_PINNED)
        with pytest.raises(RuntimeError, match="not found"):
            repo.mark_claim_submitted(
                workspace_id=_WORKSPACE_ID,
                claim_id="C1",
                submitted_at=_PINNED,
            )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListings:
    def test_list_for_user_filters_by_user_via_engagement(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_user(session, user_id=_OTHER_USER_ID, email="b@e.c", display_name="B")
        _seed_engagement(
            session, engagement_id=_OTHER_ENGAGEMENT_ID, user_id=_OTHER_USER_ID
        )
        _seed_claim(repo, claim_id="C1")  # Alice
        _seed_claim(repo, claim_id="C2", engagement_id=_OTHER_ENGAGEMENT_ID)  # Bob

        rows = repo.list_claims_for_user(
            workspace_id=_WORKSPACE_ID,
            user_id=_USER_ID,
            state=None,
            limit=10,
            cursor_id=None,
        )
        assert {r.id for r in rows} == {"C1"}

    def test_list_for_user_filters_by_state(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="draft")
        _seed_claim(repo, claim_id="C2", state="submitted", submitted_at=_PINNED)
        rows = repo.list_claims_for_user(
            workspace_id=_WORKSPACE_ID,
            user_id=_USER_ID,
            state="submitted",
            limit=10,
            cursor_id=None,
        )
        assert {r.id for r in rows} == {"C2"}

    def test_list_for_workspace_returns_every_user_claim(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_user(session, user_id=_OTHER_USER_ID, email="b@e.c", display_name="B")
        _seed_engagement(
            session, engagement_id=_OTHER_ENGAGEMENT_ID, user_id=_OTHER_USER_ID
        )
        _seed_claim(repo, claim_id="C1")
        _seed_claim(repo, claim_id="C2", engagement_id=_OTHER_ENGAGEMENT_ID)
        rows = repo.list_claims_for_workspace(
            workspace_id=_WORKSPACE_ID, state=None, limit=10, cursor_id=None
        )
        assert {r.id for r in rows} == {"C1", "C2"}

    def test_list_for_workspace_excludes_soft_deleted(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        _seed_claim(repo, claim_id="C2", deleted_at=_PINNED)
        rows = repo.list_claims_for_workspace(
            workspace_id=_WORKSPACE_ID, state=None, limit=10, cursor_id=None
        )
        assert {r.id for r in rows} == {"C1"}

    def test_list_for_user_excludes_soft_deleted(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        _seed_claim(repo, claim_id="C2", deleted_at=_PINNED)
        rows = repo.list_claims_for_user(
            workspace_id=_WORKSPACE_ID,
            user_id=_USER_ID,
            state=None,
            limit=10,
            cursor_id=None,
        )
        assert {r.id for r in rows} == {"C1"}

    def test_list_for_user_walks_cursor(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        # IDs descend lexicographically — the cursor walks ``id < cursor_id``.
        _seed_claim(repo, claim_id="C1")
        _seed_claim(repo, claim_id="C2")
        _seed_claim(repo, claim_id="C3")
        rows = repo.list_claims_for_user(
            workspace_id=_WORKSPACE_ID,
            user_id=_USER_ID,
            state=None,
            limit=10,
            cursor_id="C3",
        )
        assert [r.id for r in rows] == ["C2", "C1"]

    def test_list_for_workspace_walks_cursor(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        _seed_claim(repo, claim_id="C2")
        _seed_claim(repo, claim_id="C3")
        rows = repo.list_claims_for_workspace(
            workspace_id=_WORKSPACE_ID,
            state=None,
            limit=10,
            cursor_id="C2",
        )
        assert [r.id for r in rows] == ["C1"]

    def test_list_pending_claims_filters_by_submitted_state(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="draft")
        _seed_claim(repo, claim_id="C2", state="submitted", submitted_at=_PINNED)
        _seed_claim(
            repo,
            claim_id="C3",
            state="submitted",
            submitted_at=_PINNED + timedelta(seconds=1),
        )
        rows = repo.list_pending_claims(
            workspace_id=_WORKSPACE_ID,
            claimant_user_id=None,
            property_id=None,
            category=None,
            limit=10,
            cursor=None,
        )
        # Order: submitted_at DESC.
        assert [r.id for r in rows] == ["C3", "C2"]

    def test_list_pending_claims_walks_cursor(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="submitted", submitted_at=_PINNED)
        c2_ts = _PINNED + timedelta(seconds=1)
        _seed_claim(repo, claim_id="C2", state="submitted", submitted_at=c2_ts)
        # Aware UTC cursor mirrors production: the upstream cursor
        # decoder (:func:`app.domain.expenses.approval._decode_pending_cursor`)
        # rejects naive timestamps.
        rows = repo.list_pending_claims(
            workspace_id=_WORKSPACE_ID,
            claimant_user_id=None,
            property_id=None,
            category=None,
            limit=10,
            cursor=PendingClaimsCursor(submitted_at=c2_ts, claim_id="C2"),
        )
        assert [r.id for r in rows] == ["C1"]

    def test_list_pending_claims_filters_by_claimant_user(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_user(session, user_id=_OTHER_USER_ID, email="b@e.c", display_name="B")
        _seed_engagement(
            session, engagement_id=_OTHER_ENGAGEMENT_ID, user_id=_OTHER_USER_ID
        )
        _seed_claim(repo, claim_id="C1", state="submitted", submitted_at=_PINNED)
        _seed_claim(
            repo,
            claim_id="C2",
            state="submitted",
            submitted_at=_PINNED,
            engagement_id=_OTHER_ENGAGEMENT_ID,
        )
        rows = repo.list_pending_claims(
            workspace_id=_WORKSPACE_ID,
            claimant_user_id=_OTHER_USER_ID,
            property_id=None,
            category=None,
            limit=10,
            cursor=None,
        )
        assert {r.id for r in rows} == {"C2"}

    def test_list_pending_claims_filters_by_property_id(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(
            repo,
            claim_id="C1",
            state="submitted",
            submitted_at=_PINNED,
            property_id=None,
        )
        _seed_claim(
            repo,
            claim_id="C2",
            state="submitted",
            submitted_at=_PINNED,
            property_id="01HWA00000000000000000PRP1",
        )
        rows = repo.list_pending_claims(
            workspace_id=_WORKSPACE_ID,
            claimant_user_id=None,
            property_id="01HWA00000000000000000PRP1",
            category=None,
            limit=10,
            cursor=None,
        )
        assert {r.id for r in rows} == {"C2"}

    def test_list_pending_claims_filters_by_category(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="submitted", submitted_at=_PINNED)
        # ``_seed_claim`` hard-codes ``category='supplies'``; insert a
        # second claim directly with a different category.
        repo.session.add(
            ExpenseClaim(
                id="C2",
                workspace_id=_WORKSPACE_ID,
                work_engagement_id=_ENGAGEMENT_ID,
                vendor="Acme",
                purchased_at=_PURCHASED,
                currency="EUR",
                total_amount_cents=1000,
                category="transport",
                property_id=None,
                note_md="",
                state="submitted",
                submitted_at=_PINNED,
                created_at=_PINNED,
                deleted_at=None,
            )
        )
        repo.session.flush()
        rows = repo.list_pending_claims(
            workspace_id=_WORKSPACE_ID,
            claimant_user_id=None,
            property_id=None,
            category="transport",
            limit=10,
            cursor=None,
        )
        assert {r.id for r in rows} == {"C2"}

    def test_list_pending_reimbursement_only_returns_approved_rows(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1", state="approved")
        _seed_claim(repo, claim_id="C2", state="reimbursed")
        _seed_claim(repo, claim_id="C3", state="submitted", submitted_at=_PINNED)
        rows = repo.list_pending_reimbursement_claims(
            workspace_id=_WORKSPACE_ID, user_id=None
        )
        assert {r.id for r in rows} == {"C1"}

    def test_list_pending_reimbursement_filters_by_user(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_user(session, user_id=_OTHER_USER_ID, email="b@e.c", display_name="B")
        _seed_engagement(
            session, engagement_id=_OTHER_ENGAGEMENT_ID, user_id=_OTHER_USER_ID
        )
        _seed_claim(repo, claim_id="C1", state="approved")  # Alice
        _seed_claim(
            repo,
            claim_id="C2",
            state="approved",
            engagement_id=_OTHER_ENGAGEMENT_ID,
        )  # Bob
        rows = repo.list_pending_reimbursement_claims(
            workspace_id=_WORKSPACE_ID, user_id=_OTHER_USER_ID
        )
        assert {r.id for r in rows} == {"C2"}


# ---------------------------------------------------------------------------
# Attachment CRUD
# ---------------------------------------------------------------------------


class TestAttachments:
    def test_insert_attachment_round_trips(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        att = repo.insert_attachment(
            attachment_id="A1",
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            blob_hash="a" * 64,
            kind="receipt",
            pages=None,
            created_at=_PINNED,
        )
        assert att.id == "A1"
        assert att.kind == "receipt"
        # Round-trip sees the row.
        rows = repo.list_attachments_for_claim(
            workspace_id=_WORKSPACE_ID, claim_id="C1"
        )
        assert [r.id for r in rows] == ["A1"]

    def test_get_attachment_returns_none_for_missing(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        assert (
            repo.get_attachment(
                workspace_id=_WORKSPACE_ID, claim_id="C1", attachment_id="missing"
            )
            is None
        )

    def test_delete_attachment_removes_row(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        repo.insert_attachment(
            attachment_id="A1",
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            blob_hash="a" * 64,
            kind="receipt",
            pages=None,
            created_at=_PINNED,
        )
        repo.delete_attachment(
            workspace_id=_WORKSPACE_ID, claim_id="C1", attachment_id="A1"
        )
        assert (
            repo.get_attachment(
                workspace_id=_WORKSPACE_ID, claim_id="C1", attachment_id="A1"
            )
            is None
        )

    def test_delete_missing_attachment_raises_runtime_error(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        with pytest.raises(RuntimeError, match="not found"):
            repo.delete_attachment(
                workspace_id=_WORKSPACE_ID, claim_id="C1", attachment_id="missing"
            )

    def test_attachments_ordered_by_created_then_id(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        _seed_claim(repo, claim_id="C1")
        repo.insert_attachment(
            attachment_id="A2",
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            blob_hash="b" * 64,
            kind="receipt",
            pages=None,
            created_at=_PINNED + timedelta(seconds=1),
        )
        repo.insert_attachment(
            attachment_id="A1",
            workspace_id=_WORKSPACE_ID,
            claim_id="C1",
            blob_hash="a" * 64,
            kind="receipt",
            pages=None,
            created_at=_PINNED,
        )
        rows = repo.list_attachments_for_claim(
            workspace_id=_WORKSPACE_ID, claim_id="C1"
        )
        assert [r.id for r in rows] == ["A1", "A2"]


# ---------------------------------------------------------------------------
# Identity bulk read
# ---------------------------------------------------------------------------


class TestGetUserDisplayNames:
    def test_empty_input_returns_empty_dict(
        self, repo: SqlAlchemyExpensesRepository
    ) -> None:
        assert repo.get_user_display_names(user_ids=[]) == {}

    def test_resolves_only_present_ids(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _seed_user(session)
        _seed_user(session, user_id=_OTHER_USER_ID, email="b@e.c", display_name="B")
        result = repo.get_user_display_names(
            user_ids=[_USER_ID, _OTHER_USER_ID, "missing"]
        )
        assert result == {_USER_ID: "Alice", _OTHER_USER_ID: "B"}


# ---------------------------------------------------------------------------
# LLM-usage write
# ---------------------------------------------------------------------------


class TestInsertLlmUsage:
    def test_lands_row_in_session(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        repo.insert_llm_usage(
            usage_id="U1",
            workspace_id=_WORKSPACE_ID,
            capability="expenses.autofill",
            model_id="claude-3-5-sonnet",
            tokens_in=100,
            tokens_out=50,
            cost_cents=0,
            latency_ms=1234,
            status="ok",
            correlation_id="CORR1",
            actor_user_id=_USER_ID,
            created_at=_PINNED,
        )
        session.flush()
        row = session.get(LlmUsage, "U1")
        assert row is not None
        assert row.capability == "expenses.autofill"
        assert row.tokens_in == 100
        assert row.tokens_out == 50
        assert row.status == "ok"
        assert row.correlation_id == "CORR1"
        assert row.actor_user_id == _USER_ID

    def test_supports_error_status(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        repo.insert_llm_usage(
            usage_id="U2",
            workspace_id=_WORKSPACE_ID,
            capability="expenses.autofill",
            model_id="m",
            tokens_in=0,
            tokens_out=0,
            cost_cents=0,
            latency_ms=0,
            status="error",
            correlation_id="CORR2",
            actor_user_id=_USER_ID,
            created_at=_PINNED,
        )
        session.flush()
        row = session.get(LlmUsage, "U2")
        assert row is not None
        assert row.status == "error"

    def test_supports_timeout_status(
        self, session: Session, repo: SqlAlchemyExpensesRepository
    ) -> None:
        _bootstrap(session)
        repo.insert_llm_usage(
            usage_id="U3",
            workspace_id=_WORKSPACE_ID,
            capability="expenses.autofill",
            model_id="m",
            tokens_in=0,
            tokens_out=0,
            cost_cents=0,
            latency_ms=0,
            status="timeout",
            correlation_id="CORR3",
            actor_user_id=_USER_ID,
            created_at=_PINNED,
        )
        session.flush()
        row = session.get(LlmUsage, "U3")
        assert row is not None
        assert row.status == "timeout"
