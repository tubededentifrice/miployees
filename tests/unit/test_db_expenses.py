"""Unit tests for :mod:`app.adapters.db.expenses.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, index shape, tenancy-registry membership). Integration
coverage (migrations, FK cascade / RESTRICT, CHECK / UNIQUE
violations against a real DB, tenant filter behaviour, CRUD round-
trips, JSON round-trip, multi-attachment cardinality) lives in
``tests/integration/test_db_expenses.py`` (follow-up cd-48c1, which
also tracks the soft-ref → hard-FK promotions this slice defers).

See ``docs/specs/02-domain-model.md`` §"Core entities (by document)"
(§09 row), §"Money", §"Enums"; and
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index

# ``workspace`` is imported for its side effect of registering the
# ``workspace`` and ``work_engagement`` tables against the shared
# :class:`sqlalchemy.MetaData`. Without it, resolving the
# ``expense_claim.work_engagement_id`` FK's target raises
# :class:`~sqlalchemy.exc.NoReferencedTableError` at attribute access.
import app.adapters.db.workspace  # noqa: F401 -- registers FK targets
from app.adapters.db.expenses import ExpenseAttachment, ExpenseClaim, ExpenseLine
from app.adapters.db.expenses import models as expenses_models

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_DECIDED_AT = datetime(2026, 4, 25, 9, 30, 0, tzinfo=UTC)

_EXPENSE_TABLES: tuple[str, ...] = (
    "expense_claim",
    "expense_line",
    "expense_attachment",
)


class TestExpenseClaimModel:
    """The ``ExpenseClaim`` mapped class constructs from the v1 slice."""

    def test_minimal_draft_construction(self) -> None:
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCA",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            vendor="Monoprix",
            purchased_at=_PINNED,
            currency="EUR",
            total_amount_cents=4299,
            category="supplies",
            state="draft",
            created_at=_PINNED,
        )
        assert claim.id == "01HWA00000000000000000EXCA"
        assert claim.workspace_id == "01HWA00000000000000000WSPA"
        assert claim.work_engagement_id == "01HWA00000000000000000WEGA"
        assert claim.vendor == "Monoprix"
        assert claim.purchased_at == _PINNED
        assert claim.currency == "EUR"
        assert claim.total_amount_cents == 4299
        assert claim.category == "supplies"
        assert claim.state == "draft"
        # Nullable columns default to ``None``.
        assert claim.submitted_at is None
        assert claim.exchange_rate_to_default is None
        assert claim.owed_destination_id is None
        assert claim.owed_currency is None
        assert claim.owed_amount_cents is None
        assert claim.owed_exchange_rate is None
        assert claim.owed_rate_source is None
        assert claim.property_id is None
        assert claim.llm_autofill_json is None
        assert claim.autofill_confidence_overall is None
        assert claim.decided_by is None
        assert claim.decided_at is None
        assert claim.decision_note_md is None
        assert claim.reimbursement_destination_id is None
        assert claim.reimbursed_at is None
        assert claim.reimbursed_via is None
        assert claim.reimbursed_by is None
        assert claim.deleted_at is None

    def test_approved_construction_with_snapshots(self) -> None:
        """An approved claim carries the §09 payout-snapshot fields."""
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCB",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            submitted_at=_PINNED,
            vendor="Uber",
            purchased_at=_PINNED,
            currency="GBP",
            total_amount_cents=2345,
            exchange_rate_to_default=Decimal("1.1234"),
            owed_destination_id="01HWA00000000000000000PDSA",
            owed_currency="EUR",
            owed_amount_cents=2634,
            owed_exchange_rate=Decimal("1.12341234"),
            owed_rate_source="ecb",
            category="transport",
            property_id="01HWA00000000000000000PROA",
            note_md="Client airport pickup",
            state="approved",
            decided_by="01HWA00000000000000000USRM",
            decided_at=_DECIDED_AT,
            decision_note_md="OK per policy.",
            reimbursement_destination_id="01HWA00000000000000000PDSB",
            created_at=_PINNED,
        )
        assert claim.submitted_at == _PINNED
        assert claim.exchange_rate_to_default == Decimal("1.1234")
        assert claim.owed_currency == "EUR"
        assert claim.owed_amount_cents == 2634
        assert claim.owed_exchange_rate == Decimal("1.12341234")
        assert claim.owed_rate_source == "ecb"
        assert claim.decided_by == "01HWA00000000000000000USRM"
        assert claim.decided_at == _DECIDED_AT
        assert claim.decision_note_md == "OK per policy."
        assert claim.reimbursement_destination_id == "01HWA00000000000000000PDSB"

    def test_llm_autofill_json_roundtrip(self) -> None:
        """JSON payload round-trips through ``Mapped[Any]``."""
        payload = {
            "vendor": {"value": "Monoprix", "confidence": 0.95},
            "total_amount": {"value": 4299, "confidence": 0.91},
            "lines": [
                {
                    "description": "Bread",
                    "quantity": 2,
                    "unit_price_cents": 150,
                    "confidence": 0.88,
                },
            ],
        }
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCC",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            vendor="Monoprix",
            purchased_at=_PINNED,
            currency="EUR",
            total_amount_cents=4299,
            category="food",
            llm_autofill_json=payload,
            autofill_confidence_overall=Decimal("0.88"),
            state="draft",
            created_at=_PINNED,
        )
        assert claim.llm_autofill_json == payload
        assert claim.autofill_confidence_overall == Decimal("0.88")

    def test_tablename(self) -> None:
        assert ExpenseClaim.__tablename__ == "expense_claim"

    def test_state_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("state")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for state in (
            "draft",
            "submitted",
            "approved",
            "rejected",
            "reimbursed",
        ):
            assert state in sql, f"{state} missing from state CHECK"

    def test_category_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("category")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for cat in (
            "supplies",
            "fuel",
            "food",
            "transport",
            "maintenance",
            "other",
        ):
            assert cat in sql, f"{cat} missing from category CHECK"

    def test_currency_length_check_present(self) -> None:
        # Match the exact rendered name (the naming convention emits
        # ``ck_<table>_<constraint>``); plain ``endswith("currency_length")``
        # would also match the ``owed_currency_length`` sibling.
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and str(c.name) == "ck_expense_claim_currency_length"
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "LENGTH(currency)" in sql
        assert "3" in sql

    def test_owed_currency_length_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and str(c.name) == "ck_expense_claim_owed_currency_length"
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # Nullable guard — the CHECK must short-circuit on NULL.
        assert "owed_currency IS NULL" in sql
        assert "LENGTH(owed_currency)" in sql

    def test_total_amount_cents_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and str(c.name) == "ck_expense_claim_total_amount_cents_nonneg"
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "total_amount_cents" in sql
        assert ">= 0" in sql

    def test_owed_amount_cents_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and str(c.name) == "ck_expense_claim_owed_amount_cents_nonneg"
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # Nullable guard — a NULL snapshot (not yet approved) must
        # not trip the CHECK.
        assert "owed_amount_cents IS NULL" in sql
        assert ">= 0" in sql

    def test_autofill_confidence_bounds_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("autofill_confidence_overall_bounds")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # The 0..1 range with the nullable guard.
        assert "autofill_confidence_overall IS NULL" in sql
        assert ">= 0" in sql
        assert "<= 1" in sql

    def test_workspace_state_index_present(self) -> None:
        """Manager "claims awaiting approval" inbox rides this B-tree."""
        indexes = [i for i in ExpenseClaim.__table_args__ if isinstance(i, Index)]
        target = next(
            (i for i in indexes if i.name == "ix_expense_claim_workspace_state"),
            None,
        )
        assert target is not None, "workspace-state index missing"
        assert [c.name for c in target.columns] == ["workspace_id", "state"]

    def test_workspace_engagement_submitted_index_present(self) -> None:
        """Worker "my claims, newest first" view rides this B-tree."""
        indexes = [i for i in ExpenseClaim.__table_args__ if isinstance(i, Index)]
        target = next(
            (
                i
                for i in indexes
                if i.name == "ix_expense_claim_workspace_engagement_submitted"
            ),
            None,
        )
        assert target is not None, "workspace-engagement-submitted index missing"
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "work_engagement_id",
            "submitted_at",
        ]

    def test_reimbursed_construction_with_snapshot(self) -> None:
        """A reimbursed claim carries the cd-9guk settlement snapshot."""
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCG",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            submitted_at=_PINNED,
            vendor="Cabify",
            purchased_at=_PINNED,
            currency="EUR",
            total_amount_cents=2599,
            category="transport",
            state="reimbursed",
            decided_by="01HWA00000000000000000USRM",
            decided_at=_DECIDED_AT,
            decision_note_md="OK.",
            reimbursed_at=_DECIDED_AT,
            reimbursed_via="bank",
            reimbursed_by="01HWA00000000000000000USRO",
            created_at=_PINNED,
        )
        assert claim.reimbursed_at == _DECIDED_AT
        assert claim.reimbursed_via == "bank"
        # Reimbursed_by may differ from decided_by — the approver and
        # the settler are independent roles.
        assert claim.reimbursed_by == "01HWA00000000000000000USRO"
        assert claim.decided_by == "01HWA00000000000000000USRM"

    def test_reimbursed_via_check_present(self) -> None:
        """The ``reimbursed_via`` CHECK clamps the v1 enum and admits
        NULL (the column is null until the transition)."""
        checks = [
            c
            for c in ExpenseClaim.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("reimbursed_via")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # Nullable guard — NULL is the "not yet reimbursed" state.
        assert "reimbursed_via IS NULL" in sql
        for via in ("cash", "bank", "card", "other"):
            assert via in sql, f"{via} missing from reimbursed_via CHECK"

    def test_reimbursed_columns_nullable(self) -> None:
        """All three reimbursement-snapshot columns are nullable —
        they're populated only at the ``approved → reimbursed``
        transition."""
        cols = ExpenseClaim.__table__.c
        assert cols.reimbursed_at.nullable is True
        assert cols.reimbursed_via.nullable is True
        assert cols.reimbursed_by.nullable is True

    def test_total_amount_cents_is_int(self) -> None:
        """Per cd-lbn acceptance: amount stored as integer cents."""
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCD",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            vendor="Corner shop",
            purchased_at=_PINNED,
            currency="EUR",
            total_amount_cents=9999,
            category="other",
            state="draft",
            created_at=_PINNED,
        )
        assert isinstance(claim.total_amount_cents, int)
        assert claim.total_amount_cents == 9999

    def test_note_md_defaults_to_empty_string(self) -> None:
        """``note_md`` is NOT NULL with an empty-string default.

        Mirrors ``work_engagement.notes_md`` so seeders / API
        callers don't have to thread an explicit empty string.
        """
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCE",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            vendor="Vendor",
            purchased_at=_PINNED,
            currency="EUR",
            total_amount_cents=100,
            category="other",
            state="draft",
            created_at=_PINNED,
        )
        # The default only kicks in once the session flushes, but the
        # column is readable as an attribute even unflushed when the
        # default is set — ``MappedColumn.default`` is the Python-side
        # default callable.
        assert ExpenseClaim.__table__.c.note_md.nullable is False
        assert ExpenseClaim.__table__.c.note_md.default.arg == ""
        # Explicitly passing empty string or a body both work.
        claim.note_md = ""
        assert claim.note_md == ""
        claim.note_md = "Client reimbursement — conference travel."
        assert claim.note_md == "Client reimbursement — conference travel."


class TestExpenseLineModel:
    """The ``ExpenseLine`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        line = ExpenseLine(
            id="01HWA00000000000000000EXLA",
            workspace_id="01HWA00000000000000000WSPA",
            claim_id="01HWA00000000000000000EXCA",
            description="Fresh bread",
            quantity=Decimal("2"),
            unit_price_cents=150,
            line_total_cents=300,
            source="manual",
        )
        assert line.id == "01HWA00000000000000000EXLA"
        assert line.claim_id == "01HWA00000000000000000EXCA"
        assert line.description == "Fresh bread"
        assert line.quantity == Decimal("2")
        assert line.unit_price_cents == 150
        assert line.line_total_cents == 300
        assert line.source == "manual"
        # Nullable / defaulted columns.
        assert line.asset_id is None
        # ``edited_by_user`` is NOT NULL with a server_default of '0';
        # the ORM-side default lands on flush, so at pre-flush time
        # we only assert the column shape.
        assert ExpenseLine.__table__.c.edited_by_user.nullable is False

    def test_ocr_construction_with_asset(self) -> None:
        line = ExpenseLine(
            id="01HWA00000000000000000EXLB",
            workspace_id="01HWA00000000000000000WSPA",
            claim_id="01HWA00000000000000000EXCA",
            description="Cordless drill",
            quantity=Decimal("1"),
            unit_price_cents=12999,
            line_total_cents=12999,
            asset_id="01HWA00000000000000000ASTA",
            source="ocr",
            edited_by_user=True,
        )
        assert line.asset_id == "01HWA00000000000000000ASTA"
        assert line.source == "ocr"
        assert line.edited_by_user is True

    def test_fractional_quantity(self) -> None:
        """Fractional quantities support non-integer purchases."""
        line = ExpenseLine(
            id="01HWA00000000000000000EXLC",
            workspace_id="01HWA00000000000000000WSPA",
            claim_id="01HWA00000000000000000EXCA",
            description="Camembert",
            quantity=Decimal("0.5"),
            unit_price_cents=998,
            line_total_cents=499,
            source="ocr",
        )
        assert line.quantity == Decimal("0.5")

    def test_tablename(self) -> None:
        assert ExpenseLine.__tablename__ == "expense_line"

    def test_source_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseLine.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("source")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for source in ("ocr", "manual"):
            assert source in sql

    def test_quantity_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseLine.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("quantity_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "quantity" in sql
        assert ">= 0" in sql

    def test_unit_price_cents_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseLine.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("unit_price_cents_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "unit_price_cents" in sql
        assert ">= 0" in sql

    def test_line_total_cents_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseLine.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("line_total_cents_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "line_total_cents" in sql
        assert ">= 0" in sql

    def test_workspace_claim_index_present(self) -> None:
        """ "Fetch all lines for this claim" read path rides this B-tree."""
        indexes = [i for i in ExpenseLine.__table_args__ if isinstance(i, Index)]
        target = next(
            (i for i in indexes if i.name == "ix_expense_line_workspace_claim"),
            None,
        )
        assert target is not None, "workspace-claim index missing"
        assert [c.name for c in target.columns] == ["workspace_id", "claim_id"]


class TestExpenseAttachmentModel:
    """The ``ExpenseAttachment`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        att = ExpenseAttachment(
            id="01HWA00000000000000000EXAA",
            workspace_id="01HWA00000000000000000WSPA",
            claim_id="01HWA00000000000000000EXCA",
            blob_hash="sha256-abc123",
            kind="receipt",
            created_at=_PINNED,
        )
        assert att.id == "01HWA00000000000000000EXAA"
        assert att.claim_id == "01HWA00000000000000000EXCA"
        assert att.blob_hash == "sha256-abc123"
        assert att.kind == "receipt"
        assert att.pages is None

    def test_multipage_pdf_invoice(self) -> None:
        att = ExpenseAttachment(
            id="01HWA00000000000000000EXAB",
            workspace_id="01HWA00000000000000000WSPA",
            claim_id="01HWA00000000000000000EXCA",
            blob_hash="sha256-def456",
            kind="invoice",
            pages=7,
            created_at=_PINNED,
        )
        assert att.kind == "invoice"
        assert att.pages == 7

    def test_other_kind(self) -> None:
        att = ExpenseAttachment(
            id="01HWA00000000000000000EXAC",
            workspace_id="01HWA00000000000000000WSPA",
            claim_id="01HWA00000000000000000EXCA",
            blob_hash="sha256-other",
            kind="other",
            created_at=_PINNED,
        )
        assert att.kind == "other"

    def test_tablename(self) -> None:
        assert ExpenseAttachment.__tablename__ == "expense_attachment"

    def test_kind_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseAttachment.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in ("receipt", "invoice", "other"):
            assert kind in sql, f"{kind} missing from kind CHECK"

    def test_pages_positive_check_present(self) -> None:
        checks = [
            c
            for c in ExpenseAttachment.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("pages_positive")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # Nullable guard — NULL is the "single-image" case.
        assert "pages IS NULL" in sql
        assert ">= 1" in sql

    def test_workspace_claim_index_present(self) -> None:
        """ "Every attachment for this claim" read path rides this B-tree."""
        indexes = [i for i in ExpenseAttachment.__table_args__ if isinstance(i, Index)]
        target = next(
            (i for i in indexes if i.name == "ix_expense_attachment_workspace_claim"),
            None,
        )
        assert target is not None, "workspace-claim index missing"
        assert [c.name for c in target.columns] == ["workspace_id", "claim_id"]


class TestClaimAttachmentCardinality:
    """An ``ExpenseClaim`` supports 0..N attachments (per cd-lbn ACs).

    These tests only verify the model-level shape — the DB-level
    cardinality (insert 0, 1, and N real rows + cascade-on-delete)
    is covered in the integration suite.
    """

    def test_no_attachments_construction(self) -> None:
        """A claim without attachments is a legal shape."""
        claim = ExpenseClaim(
            id="01HWA00000000000000000EXCF",
            workspace_id="01HWA00000000000000000WSPA",
            work_engagement_id="01HWA00000000000000000WEGA",
            vendor="Online shop",
            purchased_at=_PINNED,
            currency="EUR",
            total_amount_cents=1500,
            category="supplies",
            state="draft",
            created_at=_PINNED,
        )
        # No attachments attached — the claim is a standalone row.
        assert claim.id == "01HWA00000000000000000EXCF"

    def test_attachment_fk_targets_expense_claim(self) -> None:
        """``expense_attachment.claim_id`` FK cascades on claim delete."""
        claim_id_col = ExpenseAttachment.__table__.c.claim_id
        fks = list(claim_id_col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        # Target table + column.
        assert fk.column.table.name == "expense_claim"
        assert fk.column.name == "id"
        # CASCADE on delete — deleting a claim drops its attachments.
        assert fk.ondelete == "CASCADE"

    def test_line_fk_targets_expense_claim(self) -> None:
        """``expense_line.claim_id`` FK cascades on claim delete."""
        claim_id_col = ExpenseLine.__table__.c.claim_id
        fks = list(claim_id_col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "expense_claim"
        assert fk.column.name == "id"
        assert fk.ondelete == "CASCADE"

    def test_claim_fk_work_engagement_restricts(self) -> None:
        """Archiving an engagement must not silently sweep claim history."""
        engagement_col = ExpenseClaim.__table__.c.work_engagement_id
        fks = list(engagement_col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "work_engagement"
        assert fk.column.name == "id"
        assert fk.ondelete == "RESTRICT"

    def test_claim_workspace_fk_cascades(self) -> None:
        """Sweeping a workspace sweeps its expense history."""
        workspace_col = ExpenseClaim.__table__.c.workspace_id
        fks = list(workspace_col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "workspace"
        assert fk.column.name == "id"
        assert fk.ondelete == "CASCADE"


class TestPackageReExports:
    """``app.adapters.db.expenses`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert ExpenseClaim is expenses_models.ExpenseClaim
        assert ExpenseLine is expenses_models.ExpenseLine
        assert ExpenseAttachment is expenses_models.ExpenseAttachment


class TestRegistryIntent:
    """Every expense table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.expenses``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls ``registry._reset_for_tests()`` which wipes
    the process-wide set, so asserting presence after that reset
    would be flaky. The tests below encode the invariant — "every
    expense table is scoped" — without over-coupling to import
    ordering. Mirrors the sibling ``test_db_payroll`` pattern.
    """

    def test_every_expense_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in _EXPENSE_TABLES:
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in _EXPENSE_TABLES:
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in _EXPENSE_TABLES:
            registry.register(table)
        for table in _EXPENSE_TABLES:
            assert registry.is_scoped(table) is True
