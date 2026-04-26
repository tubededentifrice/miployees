"""Integration test for the receipt-OCR / autofill job (cd-95zb).

Exercises the wiring between
:func:`app.domain.expenses.claims.attach_receipt`, the synchronous
runner from :mod:`app.worker.tasks.receipt_ocr`, and the persist
path in :mod:`app.domain.expenses.autofill` end-to-end against the
real :class:`~tests._fakes.storage.InMemoryStorage` and the migrated
SQLite engine.

Coverage:

* First attachment on a draft claim with a high-confidence stub
  payload → fields autofilled in the same UoW.
* Second attachment on the same claim → ``llm_autofill_json`` already
  populated, so the runner does NOT overwrite the worker-typed
  fields even on another high-confidence run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.expenses.repositories import (
    SqlAlchemyCapabilityChecker,
    SqlAlchemyExpensesRepository,
)
from app.adapters.db.workspace.models import WorkEngagement
from app.adapters.llm.ports import (
    ChatMessage,
    LLMCapabilityMissing,
    LLMResponse,
    LLMUsage,
)
from app.config import Settings
from app.domain.expenses import (
    ExpenseAttachmentView,
    ExpenseClaimView,
    ReceiptKind,
)
from app.domain.expenses import claims as _claims_module
from app.domain.expenses.claims import ExpenseClaimCreate
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.receipt_ocr import run_receipt_ocr
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)
_OCR_MODEL = "test/gemma-vision"


# ---------------------------------------------------------------------------
# Seam compat shims (cd-0e8i)
# ---------------------------------------------------------------------------
#
# The cd-0e8i refactor flipped :mod:`app.domain.expenses.claims`'s
# public API to ``(repo, checker, ctx, *, ...)``. The integration
# coverage here doesn't yet feed seams through; these wrappers rebuild
# the SA pair on each call so the legacy session-based shape keeps
# working until cd-sxmz wires the worker job onto the seam.


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


def attach_receipt(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    blob_hash: str,
    content_type: str,
    size_bytes: int,
    storage: InMemoryStorage,
    kind: ReceiptKind = "receipt",
    pages: int | None = None,
    clock: FrozenClock | None = None,
    extraction_runner: Callable[..., Any] | None = None,
) -> ExpenseAttachmentView:
    repo, checker = _make_seam_pair(session, ctx)
    return _claims_module.attach_receipt(
        repo,
        checker,
        ctx,
        claim_id=claim_id,
        blob_hash=blob_hash,
        content_type=content_type,
        size_bytes=size_bytes,
        storage=storage,
        kind=kind,
        pages=pages,
        clock=clock,
        extraction_runner=extraction_runner,
    )


# ---------------------------------------------------------------------------
# Stub LLM (matches the unit-test stub shape — kept local to avoid a
# cross-tier import the unit tests own).
# ---------------------------------------------------------------------------


class StubLLMClient:
    def __init__(self, payloads: list[dict[str, Any]] | dict[str, Any]) -> None:
        if isinstance(payloads, dict):
            payloads = [payloads]
        self._payloads = payloads
        self._cursor = 0

    def _next_payload(self) -> dict[str, Any]:
        # Stick on the last payload once exhausted — useful when the
        # second attach should return the same canned shape.
        idx = min(self._cursor, len(self._payloads) - 1)
        self._cursor += 1
        return self._payloads[idx]

    def complete(  # pragma: no cover
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        raise NotImplementedError

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(self._next_payload()),
            usage=LLMUsage(
                prompt_tokens=23,
                completion_tokens=11,
                total_tokens=34,
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        return "Vendor: Stub\nTotal: 27.50 EUR\n2026-04-17"

    def stream_chat(  # pragma: no cover
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        raise LLMCapabilityMissing("stream_chat")


def _high_confidence_payload(
    *, vendor: str = "Bistro 42", amount: str = "27.50"
) -> dict[str, Any]:
    return {
        "vendor": vendor,
        "amount": amount,
        "currency": "EUR",
        "purchased_at": "2026-04-17T12:30:00+00:00",
        "category": "food",
        "confidence": {
            "vendor": 0.95,
            "amount": 0.95,
            "currency": 0.95,
            "purchased_at": 0.95,
            "category": 0.95,
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        llm_ocr_model=_OCR_MODEL,
    )


def _engagement(session: Session, *, workspace_id: str, user_id: str) -> str:
    eng_id = new_ulid()
    with tenant_agnostic():
        session.add(
            WorkEngagement(
                id=eng_id,
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
    return eng_id


def _grant_worker(session: Session, *, workspace_id: str, user_id: str) -> None:
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        session.flush()


@pytest.fixture
def seeded(db_session: Session) -> dict[str, Any]:
    """Seed a workspace + worker user + engagement; return ids + ctx."""
    tag = new_ulid()[-8:].lower()
    user = bootstrap_user(
        db_session, email=f"w-{tag}@example.com", display_name="Worker"
    )
    ws = bootstrap_workspace(
        db_session, slug=f"ocr-{tag}", name="OCR WS", owner_user_id=user.id
    )
    _grant_worker(db_session, workspace_id=ws.id, user_id=user.id)
    eng_id = _engagement(db_session, workspace_id=ws.id, user_id=user.id)
    db_session.commit()

    ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=True,
    )
    return {
        "workspace_id": ws.id,
        "user_id": user.id,
        "engagement_id": eng_id,
        "ctx": ctx,
    }


def _put_blob(storage: InMemoryStorage, *, payload: bytes = b"image-bytes") -> str:
    import hashlib
    import io

    h = hashlib.sha256(payload + new_ulid().encode()).hexdigest()
    storage.put(h, io.BytesIO(payload), content_type="image/jpeg")
    return h


def _create_body(*, work_engagement_id: str) -> ExpenseClaimCreate:
    return ExpenseClaimCreate(
        work_engagement_id=work_engagement_id,
        vendor="Original Vendor",
        purchased_at=_PURCHASED,
        currency="EUR",
        total_amount_cents=10_00,
        category="other",
        property_id=None,
        note_md="",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAttachReceiptWithRunner:
    def test_first_attachment_autofills(
        self,
        db_session: Session,
        storage: InMemoryStorage,
        settings: Settings,
        seeded: dict[str, Any],
    ) -> None:
        ctx: WorkspaceContext = seeded["ctx"]
        clock = FrozenClock(_PINNED)
        view = create_claim(
            db_session,
            ctx,
            body=_create_body(work_engagement_id=seeded["engagement_id"]),
            clock=clock,
        )

        llm = StubLLMClient(payloads=_high_confidence_payload())

        def runner(
            _session: Session,
            _ctx: WorkspaceContext,
            *,
            claim_id: str,
            attachment_id: str,
        ) -> None:
            run_receipt_ocr(
                _session,
                _ctx,
                claim_id=claim_id,
                attachment_id=attachment_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

        h = _put_blob(storage)
        attach_receipt(
            db_session,
            ctx,
            claim_id=view.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
            extraction_runner=runner,
        )

        # Re-read claim row → fields rewritten by the runner.
        with tenant_agnostic():
            claim = db_session.scalars(
                select(ExpenseClaim).where(ExpenseClaim.id == view.id)
            ).one()
        assert claim.vendor == "Bistro 42"
        assert claim.currency == "EUR"
        assert claim.total_amount_cents == 27_50
        assert claim.category == "food"
        assert claim.llm_autofill_json is not None

    def test_second_attachment_does_not_overwrite_user_edits(
        self,
        db_session: Session,
        storage: InMemoryStorage,
        settings: Settings,
        seeded: dict[str, Any],
    ) -> None:
        ctx: WorkspaceContext = seeded["ctx"]
        clock = FrozenClock(_PINNED)
        view = create_claim(
            db_session,
            ctx,
            body=_create_body(work_engagement_id=seeded["engagement_id"]),
            clock=clock,
        )

        # First attachment: high-confidence → autofills.
        first = _high_confidence_payload(vendor="Bistro 42", amount="27.50")
        # Second attachment: still high-confidence but DIFFERENT values
        # — the test asserts the second run does NOT overwrite the
        # already-typed user fields.
        second = _high_confidence_payload(vendor="Other Vendor", amount="55.00")
        llm = StubLLMClient(payloads=[first, second])

        def runner(
            _session: Session,
            _ctx: WorkspaceContext,
            *,
            claim_id: str,
            attachment_id: str,
        ) -> None:
            run_receipt_ocr(
                _session,
                _ctx,
                claim_id=claim_id,
                attachment_id=attachment_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

        h1 = _put_blob(storage)
        attach_receipt(
            db_session,
            ctx,
            claim_id=view.id,
            blob_hash=h1,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
            extraction_runner=runner,
        )
        # Worker types over the autofilled vendor.
        with tenant_agnostic():
            claim = db_session.scalars(
                select(ExpenseClaim).where(ExpenseClaim.id == view.id)
            ).one()
            claim.vendor = "User Edit"
            db_session.flush()

        # Second attachment with a different high-confidence payload.
        h2 = _put_blob(storage)
        attach_receipt(
            db_session,
            ctx,
            claim_id=view.id,
            blob_hash=h2,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
            extraction_runner=runner,
        )

        with tenant_agnostic():
            claim = db_session.scalars(
                select(ExpenseClaim).where(ExpenseClaim.id == view.id)
            ).one()
        assert claim.vendor == "User Edit"
        # The amount was set to 27_50 by the first autofill; the user
        # didn't override it. The second run must NOT bump it to
        # 55_00 because ``llm_autofill_json`` is already populated.
        assert claim.total_amount_cents == 27_50
