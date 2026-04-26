"""Unit tests for :mod:`app.domain.expenses.autofill` (cd-95zb).

Drives the receipt-OCR / autofill pipeline against an in-memory
SQLite engine + a stubbed :class:`~app.adapters.llm.ports.LLMClient`
that returns canned JSON. Coverage mirrors the cd-95zb acceptance
criteria:

* Happy path: confidence > threshold + draft + first-run → fields
  autofilled, payload + overall_confidence persisted.
* Low confidence: payload still persisted, fields untouched.
* Second attachment on same claim: ``llm_autofill_json`` already
  populated → no field overwrite even on high confidence.
* Already-submitted claim: state guard refuses autofill.
* LLM body is malformed JSON → :class:`ExtractionParseError`,
  ``receipt.ocr_failed`` audit row, claim untouched.
* LLM raises adapter-level failures → mapped to
  :class:`ExtractionRateLimited` / :class:`ExtractionTimeout` with
  the same audit + no-mutate guarantee.
* Naive ``purchased_at`` and unknown currency in the LLM body →
  :class:`ExtractionParseError`.
* :func:`overall_confidence` is the ``min()`` of the per-field map.
* Feature disabled (``settings.llm_ocr_model is None``) →
  ``attach_receipt(extraction_runner=None)`` is a no-op on the
  autofill columns.
* :class:`LlmUsage` row persisted with ``capability=
  "expenses.autofill"`` and the workspace id.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import WorkEngagement, Workspace
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
from app.domain.expenses.autofill import (
    AUTOFILL_CAPABILITY,
    AUTOFILL_CONFIDENCE_THRESHOLD,
    AttachmentNotFound,
    ClaimNotFound,
    ExtractionParseError,
    ExtractionProviderError,
    ExtractionRateLimited,
    ExtractionTimeout,
    ReceiptExtraction,
    overall_confidence,
    run_extraction,
)
from app.domain.expenses.claims import ExpenseClaimCreate
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)
_OCR_MODEL = "test/gemma-vision"


# ---------------------------------------------------------------------------
# Seam compat shims (cd-0e8i)
# ---------------------------------------------------------------------------
#
# The cd-0e8i refactor flipped :mod:`app.domain.expenses.claims`'s
# public API to ``(repo, checker, ctx, *, ...)``. Autofill is still on
# the old session-based shape (cd-sxmz follow-up); these wrappers
# rebuild the SA seam pair on each call so the autofill-side coverage
# can keep using the legacy session-based call shape until then.


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
# Fixtures (mirror tests/unit/test_expense_claims.py — same DB harness)
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
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
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


@pytest.fixture
def settings() -> Settings:
    """Minimal :class:`Settings` with the OCR model wired."""
    return Settings(
        database_url="sqlite:///:memory:",
        llm_ocr_model=_OCR_MODEL,
    )


@pytest.fixture
def settings_disabled() -> Settings:
    """:class:`Settings` with the OCR feature disabled."""
    return Settings(
        database_url="sqlite:///:memory:",
        llm_ocr_model=None,
    )


# ---------------------------------------------------------------------------
# Stub LLM client
# ---------------------------------------------------------------------------


class StubLLMClient:
    """Test double — returns canned JSON / raises canned errors.

    Each instance carries one ``ocr_text`` (the verbatim OCR pass
    output) and one ``chat_payload`` (the dict the chat call should
    serialise into JSON). Either side can be replaced with a callable
    that raises to exercise the failure paths.
    """

    def __init__(
        self,
        *,
        ocr_text: str = "Vendor: Acme\nTotal: 12.50 EUR\n2026-04-17",
        chat_payload: dict[str, Any] | str | Exception | None = None,
        ocr_error: Exception | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self._ocr_text = ocr_text
        self._chat_payload = chat_payload
        self._ocr_error = ocr_error
        self._chat_error = chat_error
        self.calls: list[tuple[str, str]] = []  # (method, model_id)

    def complete(  # pragma: no cover - unused
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
        self.calls.append(("chat", model_id))
        if self._chat_error is not None:
            raise self._chat_error
        if isinstance(self._chat_payload, str):
            text = self._chat_payload
        elif isinstance(self._chat_payload, dict):
            text = json.dumps(self._chat_payload)
        else:
            text = "{}"
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                prompt_tokens=42,
                completion_tokens=17,
                total_tokens=59,
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        self.calls.append(("ocr", model_id))
        if self._ocr_error is not None:
            raise self._ocr_error
        return self._ocr_text

    def stream_chat(  # pragma: no cover - unused
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        raise LLMCapabilityMissing("stream_chat")


# Adapter-style errors — copy the class names so the autofill module's
# string-matched mapping (avoids an import cycle through the adapter
# package) routes them correctly.
class LlmRateLimited(RuntimeError):
    pass


class LlmTransportError(RuntimeError):
    pass


class LlmProviderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Bootstrap helpers (re-implemented locally rather than imported so the
# test module stays self-contained — same pattern as the sibling
# ``test_expense_claims.py``).
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


def _bootstrap_engagement(s: Session, *, workspace_id: str, user_id: str) -> str:
    eng_id = new_ulid()
    s.add(
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
    ws_id = _bootstrap_workspace(session, slug="autofill-env")
    user_id = _bootstrap_user(session, email="w@example.com", display_name="W")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="worker")
    eng_id = _bootstrap_engagement(session, workspace_id=ws_id, user_id=user_id)
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="worker")
    return ctx, user_id, eng_id, clock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _high_confidence_payload(
    *,
    vendor: str = "Bistro 42",
    amount: str = "27.50",
    currency: str = "EUR",
    purchased_at: str = "2026-04-17T12:30:00+00:00",
    category: str = "food",
    score: float = 0.95,
) -> dict[str, Any]:
    return {
        "vendor": vendor,
        "amount": amount,
        "currency": currency,
        "purchased_at": purchased_at,
        "category": category,
        "confidence": {
            "vendor": score,
            "amount": score,
            "currency": score,
            "purchased_at": score,
            "category": score,
        },
    }


def _low_confidence_payload() -> dict[str, Any]:
    payload = _high_confidence_payload()
    payload["confidence"]["amount"] = 0.40
    return payload


def _audit_rows(session: Session, *, workspace_id: str) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    return list(session.scalars(stmt).all())


def _llm_usage_rows(session: Session, *, workspace_id: str) -> list[LlmUsageRow]:
    stmt = (
        select(LlmUsageRow)
        .where(LlmUsageRow.workspace_id == workspace_id)
        .order_by(LlmUsageRow.created_at.asc(), LlmUsageRow.id.asc())
    )
    return list(session.scalars(stmt).all())


def _attach_with_blob(
    session: Session,
    ctx: WorkspaceContext,
    *,
    storage: InMemoryStorage,
    claim_id: str,
    clock: FrozenClock,
) -> tuple[str, str]:
    """Attach a fresh blob to ``claim_id`` and return ``(attachment_id, hash)``."""
    h = _put_blob(storage)
    view = attach_receipt(
        session,
        ctx,
        claim_id=claim_id,
        blob_hash=h,
        content_type="image/jpeg",
        size_bytes=128,
        storage=storage,
        clock=clock,
    )
    return view.id, h


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_claim_not_found_is_lookup_error(self) -> None:
        assert issubclass(ClaimNotFound, LookupError)

    def test_attachment_not_found_is_lookup_error(self) -> None:
        assert issubclass(AttachmentNotFound, LookupError)

    def test_parse_error_is_value_error(self) -> None:
        assert issubclass(ExtractionParseError, ValueError)

    def test_timeout_is_timeout_error(self) -> None:
        assert issubclass(ExtractionTimeout, TimeoutError)

    def test_rate_limited_is_runtime_error(self) -> None:
        assert issubclass(ExtractionRateLimited, RuntimeError)

    def test_provider_error_is_runtime_error(self) -> None:
        assert issubclass(ExtractionProviderError, RuntimeError)


# ---------------------------------------------------------------------------
# ReceiptExtraction schema
# ---------------------------------------------------------------------------


class TestReceiptExtractionSchema:
    def test_happy_path_validates(self) -> None:
        e = ReceiptExtraction.model_validate(
            {
                "vendor": "Acme",
                "amount_cents": 1250,
                "currency": "EUR",
                "purchased_at": "2026-04-17T10:00:00+00:00",
                "category": "supplies",
                "confidence": {
                    "vendor": 0.9,
                    "amount": 0.9,
                    "currency": 0.99,
                    "purchased_at": 0.95,
                    "category": 0.85,
                },
            }
        )
        assert e.vendor == "Acme"
        assert e.amount_cents == 1250
        assert e.currency == "EUR"
        assert e.category == "supplies"

    def test_naive_purchased_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiptExtraction.model_validate(
                {
                    "vendor": "Acme",
                    "amount_cents": 1250,
                    "currency": "EUR",
                    # NO offset → naive timestamp.
                    "purchased_at": "2026-04-17T10:00:00",
                    "category": "supplies",
                    "confidence": {
                        "vendor": 0.9,
                        "amount": 0.9,
                        "currency": 0.99,
                        "purchased_at": 0.95,
                        "category": 0.85,
                    },
                }
            )

    def test_unknown_currency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiptExtraction.model_validate(
                {
                    "vendor": "Acme",
                    "amount_cents": 1250,
                    "currency": "ZZZ",  # not in the allow-list
                    "purchased_at": "2026-04-17T10:00:00+00:00",
                    "category": "supplies",
                    "confidence": {
                        "vendor": 0.9,
                        "amount": 0.9,
                        "currency": 0.9,
                        "purchased_at": 0.9,
                        "category": 0.9,
                    },
                }
            )

    def test_currency_uppercased(self) -> None:
        e = ReceiptExtraction.model_validate(
            {
                "vendor": "Acme",
                "amount_cents": 1250,
                "currency": "eur",
                "purchased_at": "2026-04-17T10:00:00+00:00",
                "category": "supplies",
                "confidence": {
                    "vendor": 0.9,
                    "amount": 0.9,
                    "currency": 0.9,
                    "purchased_at": 0.9,
                    "category": 0.9,
                },
            }
        )
        assert e.currency == "EUR"

    def test_confidence_missing_required_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiptExtraction.model_validate(
                {
                    "vendor": "Acme",
                    "amount_cents": 1250,
                    "currency": "EUR",
                    "purchased_at": "2026-04-17T10:00:00+00:00",
                    "category": "supplies",
                    # missing 'category'
                    "confidence": {
                        "vendor": 0.9,
                        "amount": 0.9,
                        "currency": 0.9,
                        "purchased_at": 0.9,
                    },
                }
            )

    def test_confidence_score_outside_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReceiptExtraction.model_validate(
                {
                    "vendor": "Acme",
                    "amount_cents": 1250,
                    "currency": "EUR",
                    "purchased_at": "2026-04-17T10:00:00+00:00",
                    "category": "supplies",
                    "confidence": {
                        "vendor": 1.5,  # > 1
                        "amount": 0.9,
                        "currency": 0.9,
                        "purchased_at": 0.9,
                        "category": 0.9,
                    },
                }
            )


# ---------------------------------------------------------------------------
# overall_confidence
# ---------------------------------------------------------------------------


class TestOverallConfidence:
    def test_returns_minimum_quantised_to_two_decimals(self) -> None:
        e = ReceiptExtraction.model_validate(
            {
                "vendor": "Acme",
                "amount_cents": 1250,
                "currency": "EUR",
                "purchased_at": "2026-04-17T10:00:00+00:00",
                "category": "supplies",
                "confidence": {
                    "vendor": 0.92,
                    "amount": 0.87,
                    "currency": 0.99,
                    "purchased_at": 0.95,
                    "category": 0.81,
                },
            }
        )
        assert overall_confidence(e) == Decimal("0.81")

    def test_threshold_constant_is_decimal(self) -> None:
        assert isinstance(AUTOFILL_CONFIDENCE_THRESHOLD, Decimal)
        assert Decimal("0.85") == AUTOFILL_CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# run_extraction — happy path + autofill rules
# ---------------------------------------------------------------------------


class TestRunExtractionHappyPath:
    def test_high_confidence_autofills_first_attachment(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        att_id, _h = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        llm = StubLLMClient(chat_payload=_high_confidence_payload())

        result = run_extraction(
            session,
            ctx,
            claim_id=view.id,
            attachment_id=att_id,
            llm=llm,
            storage=storage,
            clock=clock,
            settings=settings,
        )

        assert result.autofilled is True
        assert set(result.autofilled_fields) == {
            "vendor",
            "purchased_at",
            "currency",
            "total_amount_cents",
            "category",
        }

        # Re-read claim row → fields rewritten.
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        assert claim.vendor == "Bistro 42"
        assert claim.currency == "EUR"
        assert claim.total_amount_cents == 27_50
        assert claim.category == "food"
        assert claim.llm_autofill_json is not None
        assert claim.autofill_confidence_overall == Decimal("0.95")

    def test_payload_persisted_even_below_threshold(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        att_id, _h = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        llm = StubLLMClient(chat_payload=_low_confidence_payload())

        result = run_extraction(
            session,
            ctx,
            claim_id=view.id,
            attachment_id=att_id,
            llm=llm,
            storage=storage,
            clock=clock,
            settings=settings,
        )

        assert result.autofilled is False
        assert result.autofilled_fields == ()

        # Fields untouched — original "Original Vendor" still in place.
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        assert claim.vendor == "Original Vendor"
        # But payload + overall confidence persisted.
        assert claim.llm_autofill_json is not None
        assert claim.autofill_confidence_overall == Decimal("0.40")

    def test_second_attachment_does_not_overwrite_fields(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )

        # First attachment + high-confidence run → autofills.
        att_id_1, _h1 = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        llm = StubLLMClient(chat_payload=_high_confidence_payload())
        run_extraction(
            session,
            ctx,
            claim_id=view.id,
            attachment_id=att_id_1,
            llm=llm,
            storage=storage,
            clock=clock,
            settings=settings,
        )
        # Worker types over the autofilled fields — simulated by a
        # raw column write.
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        claim.vendor = "User-typed Vendor"
        claim.total_amount_cents = 99_99
        session.flush()

        # Second attachment + a different high-confidence payload.
        att_id_2, _h2 = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        llm2 = StubLLMClient(
            chat_payload=_high_confidence_payload(vendor="Other Vendor", amount="55.00")
        )
        result = run_extraction(
            session,
            ctx,
            claim_id=view.id,
            attachment_id=att_id_2,
            llm=llm2,
            storage=storage,
            clock=clock,
            settings=settings,
        )

        assert result.autofilled is False
        # User-typed values survive — autofill is "first run only".
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        assert claim.vendor == "User-typed Vendor"
        assert claim.total_amount_cents == 99_99

    def test_submitted_claim_state_guard(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        """A run on a non-draft claim still persists the payload (so
        the audit history is complete) but does not autofill the
        scalar fields."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        att_id, _h = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        # Submit before the OCR — manager queue scenario where the
        # extraction runs against an already-submitted claim.
        submit_claim(session, ctx, claim_id=view.id, clock=clock)
        llm = StubLLMClient(chat_payload=_high_confidence_payload())

        result = run_extraction(
            session,
            ctx,
            claim_id=view.id,
            attachment_id=att_id,
            llm=llm,
            storage=storage,
            clock=clock,
            settings=settings,
        )

        assert result.autofilled is False
        # Worker-typed fields untouched.
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        assert claim.vendor == "Original Vendor"


# ---------------------------------------------------------------------------
# run_extraction — failure modes
# ---------------------------------------------------------------------------


class TestRunExtractionFailures:
    def _setup_draft(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> tuple[WorkspaceContext, str, str, FrozenClock]:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        att_id, _h = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        return ctx, view.id, att_id, clock

    def test_malformed_json_raises_parse_error(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        llm = StubLLMClient(chat_payload="not-json-at-all")

        with pytest.raises(ExtractionParseError):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

        # Claim untouched: no autofill payload, original vendor.
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == claim_id)
        ).one()
        assert claim.llm_autofill_json is None
        assert claim.vendor == "Original Vendor"

        # Audit row written.
        audit_actions = [
            r.action
            for r in _audit_rows(session, workspace_id=ctx.workspace_id)
            if r.entity_kind == "expense_claim"
        ]
        assert "receipt.ocr_failed" in audit_actions

    def test_naive_purchased_at_raises_parse_error(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        bad = _high_confidence_payload(purchased_at="2026-04-17T12:30:00")
        llm = StubLLMClient(chat_payload=bad)

        with pytest.raises(ExtractionParseError):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

    def test_unknown_currency_raises_parse_error(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        bad = _high_confidence_payload(currency="ZZZ")
        llm = StubLLMClient(chat_payload=bad)

        with pytest.raises(ExtractionParseError):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

    def test_rate_limited_maps_to_extraction_rate_limited(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        llm = StubLLMClient(chat_error=LlmRateLimited("rate-limited 3x"))

        with pytest.raises(ExtractionRateLimited):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

        # Audit row written; claim untouched.
        audit_actions = [
            r.action
            for r in _audit_rows(session, workspace_id=ctx.workspace_id)
            if r.entity_kind == "expense_claim"
        ]
        assert audit_actions[-1] == "receipt.ocr_failed"
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == claim_id)
        ).one()
        assert claim.llm_autofill_json is None

    def test_provider_error_maps_to_extraction_provider_error(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        llm = StubLLMClient(chat_error=LlmProviderError("400 Bad Request"))

        with pytest.raises(ExtractionProviderError):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

    def test_transport_error_maps_to_provider_error(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        llm = StubLLMClient(chat_error=LlmTransportError("502 Bad Gateway"))

        with pytest.raises(ExtractionProviderError):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

    def test_timeout_maps_to_extraction_timeout(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, claim_id, att_id, clock = self._setup_draft(session, worker_env, storage)
        llm = StubLLMClient(chat_error=TimeoutError("read timeout"))

        with pytest.raises(ExtractionTimeout):
            run_extraction(
                session,
                ctx,
                claim_id=claim_id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

        # Usage row written with status="timeout".
        rows = _llm_usage_rows(session, workspace_id=ctx.workspace_id)
        assert any(r.status == "timeout" for r in rows)


# ---------------------------------------------------------------------------
# LLM usage row
# ---------------------------------------------------------------------------


class TestLlmUsagePersisted:
    def test_usage_row_carries_capability_and_workspace(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        att_id, _h = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        llm = StubLLMClient(chat_payload=_high_confidence_payload())

        result = run_extraction(
            session,
            ctx,
            claim_id=view.id,
            attachment_id=att_id,
            llm=llm,
            storage=storage,
            clock=clock,
            settings=settings,
        )

        rows = _llm_usage_rows(session, workspace_id=ctx.workspace_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.id == result.llm_usage_id
        assert row.workspace_id == ctx.workspace_id
        assert row.capability == AUTOFILL_CAPABILITY
        assert row.model_id == _OCR_MODEL
        assert row.tokens_in == 42
        assert row.tokens_out == 17
        assert row.status == "ok"
        assert row.actor_user_id == ctx.actor_id

    def test_parse_error_usage_row_carries_real_token_counts(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        """When the chat call lands but the body fails to parse, the
        provider has already burnt tokens — the failure-mode
        ``LlmUsage`` row must reflect the real counts so /admin/usage
        does not under-report spend."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        att_id, _h = _attach_with_blob(
            session, ctx, storage=storage, claim_id=view.id, clock=clock
        )
        # Stub returns garbage JSON — chat call succeeds (tokens
        # spent), parse fails downstream.
        llm = StubLLMClient(chat_payload="not-json")

        with pytest.raises(ExtractionParseError):
            run_extraction(
                session,
                ctx,
                claim_id=view.id,
                attachment_id=att_id,
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

        rows = _llm_usage_rows(session, workspace_id=ctx.workspace_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "error"
        # The stub reports prompt_tokens=42, completion_tokens=17 on
        # every chat call — those are the spent tokens we MUST
        # surface even though the parse failed.
        assert row.tokens_in == 42
        assert row.tokens_out == 17
        assert row.model_id == _OCR_MODEL


# ---------------------------------------------------------------------------
# attach_receipt extraction_runner integration
# ---------------------------------------------------------------------------


class TestAttachReceiptRunner:
    def test_runner_none_skips_autofill(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings_disabled: Settings,
    ) -> None:
        """When ``settings.llm_ocr_model`` is unset, the typical
        wiring passes ``extraction_runner=None`` — the attach is a
        clean no-op on the autofill columns and works as before
        cd-95zb. Asserted by passing ``None`` directly."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)
        attach_receipt(
            session,
            ctx,
            claim_id=view.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
            extraction_runner=None,
        )

        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        assert claim.llm_autofill_json is None
        # Reference settings_disabled to anchor the "feature off"
        # contract — the test verifies the seam handles ``None``
        # gracefully regardless of settings.
        assert settings_disabled.llm_ocr_model is None

    def test_runner_invoked_with_attachment_id(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        """A non-``None`` runner is called once per successful attach,
        with the freshly-minted attachment id."""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        h = _put_blob(storage)

        captured: list[tuple[str, str]] = []

        def fake_runner(
            _session: Session,
            _ctx: WorkspaceContext,
            *,
            claim_id: str,
            attachment_id: str,
        ) -> None:
            captured.append((claim_id, attachment_id))

        view_att = attach_receipt(
            session,
            ctx,
            claim_id=view.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
            extraction_runner=fake_runner,
        )
        assert captured == [(view.id, view_att.id)]

    def test_runner_skipped_on_non_draft(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        """The runner only fires on a draft claim. Once submitted,
        further attach calls are blocked at the service level
        anyway — we assert the runner contract by configuring the
        attach to short-circuit before the call site reads the
        runner. (A non-draft attach raises :class:`ClaimNotEditable`
        before the runner is consulted.)"""
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        # Attach + submit, then verify the second attach fails before
        # the runner runs.
        h1 = _put_blob(storage)
        attach_receipt(
            session,
            ctx,
            claim_id=view.id,
            blob_hash=h1,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
        )
        submit_claim(session, ctx, claim_id=view.id, clock=clock)

        runner_calls = 0

        def fake_runner(
            _session: Session,
            _ctx: WorkspaceContext,
            *,
            claim_id: str,
            attachment_id: str,
        ) -> None:
            nonlocal runner_calls
            runner_calls += 1

        h2 = _put_blob(storage)
        from app.domain.expenses import ClaimNotEditable

        with pytest.raises(ClaimNotEditable):
            attach_receipt(
                session,
                ctx,
                claim_id=view.id,
                blob_hash=h2,
                content_type="image/jpeg",
                size_bytes=128,
                storage=storage,
                clock=clock,
                extraction_runner=fake_runner,
            )
        assert runner_calls == 0

    def test_runner_failure_does_not_roll_back_attach(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
    ) -> None:
        """cd-95zb contract: "failure modes leave the claim untouched
        and audit the failure". A raising runner must NOT bubble up —
        otherwise the surrounding UoW would roll back, wiping out the
        attach row, the attach audit row, and the runner's failure
        audit row, leaving the caller with a 5xx and a stranded blob.
        ``attach_receipt`` swallows the runner's exception so all
        rows survive the commit.
        """
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )

        def boom_runner(
            _session: Session,
            _ctx: WorkspaceContext,
            *,
            claim_id: str,
            attachment_id: str,
        ) -> None:
            raise RuntimeError("LLM provider returned 503")

        h = _put_blob(storage)
        att = attach_receipt(
            session,
            ctx,
            claim_id=view.id,
            blob_hash=h,
            content_type="image/jpeg",
            size_bytes=128,
            storage=storage,
            clock=clock,
            extraction_runner=boom_runner,
        )
        # Attach row landed despite the runner crash.
        assert att.claim_id == view.id

        # Re-read the claim — the attach is in the session and its
        # autofill columns stay NULL (the runner never wrote them).
        claim = session.scalars(
            select(ExpenseClaim).where(ExpenseClaim.id == view.id)
        ).one()
        assert claim.llm_autofill_json is None
        # Confirm the attach row also survived the runner crash.
        from app.adapters.db.expenses.models import ExpenseAttachment

        attachments = list(
            session.scalars(
                select(ExpenseAttachment).where(ExpenseAttachment.claim_id == view.id)
            )
        )
        assert len(attachments) == 1


# ---------------------------------------------------------------------------
# Lookup errors
# ---------------------------------------------------------------------------


class TestLookups:
    def test_unknown_claim_raises(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, _user_id, _eng_id, clock = worker_env
        llm = StubLLMClient(chat_payload=_high_confidence_payload())

        with pytest.raises(ClaimNotFound):
            run_extraction(
                session,
                ctx,
                claim_id="nonexistent",
                attachment_id="nope",
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )

    def test_unknown_attachment_raises(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, str, FrozenClock],
        storage: InMemoryStorage,
        settings: Settings,
    ) -> None:
        ctx, _user_id, eng_id, clock = worker_env
        view = create_claim(
            session, ctx, body=_create_body(work_engagement_id=eng_id), clock=clock
        )
        llm = StubLLMClient(chat_payload=_high_confidence_payload())

        with pytest.raises(AttachmentNotFound):
            run_extraction(
                session,
                ctx,
                claim_id=view.id,
                attachment_id="nope",
                llm=llm,
                storage=storage,
                clock=clock,
                settings=settings,
            )
