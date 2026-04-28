"""Integration tests for :mod:`app.adapters.db.payroll` against a real DB.

Covers the post-migration schema shape (tables, unique composites,
FKs, CHECK constraints, indexes), the referential-integrity
contract on all three tables (``workspace_id`` CASCADE; ``user_id``
RESTRICT; ``pay_period_id`` CASCADE), happy-path round-trip of
every model (insert + select + update + delete), CHECK + UNIQUE
violations, the state-transition path via ORM update, JSON
``deductions_cents`` round-trip, and tenant-filter behaviour (all
three tables scoped; SELECT without a :class:`WorkspaceContext`
raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_payroll.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"pay_rule", §"pay_period",
§"payslip", and ``docs/specs/09-time-payroll-expenses.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.payroll.models import PayPeriod, PayRule, Payslip
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_EFFECTIVE_FROM = _PINNED
_EFFECTIVE_TO = _PINNED + timedelta(days=365)
_PERIOD_START = _PINNED + timedelta(days=1)
_PERIOD_END = _PINNED + timedelta(days=31)


_PAYROLL_TABLES: tuple[str, ...] = ("pay_rule", "pay_period", "payslip")


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_payroll_registered() -> None:
    """Re-register the three payroll tables as workspace-scoped.

    ``app.adapters.db.payroll.__init__`` registers them at import
    time, but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite.
    """
    for table in _PAYROLL_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _bootstrap(
    session: Session, *, email: str, display: str, slug: str, name: str
) -> tuple[Workspace, User]:
    """Seed a user + workspace pair for a test."""
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(session, email=email, display_name=display, clock=clock)
    workspace = bootstrap_workspace(
        session, slug=slug, name=name, owner_user_id=user.id, clock=clock
    )
    return workspace, user


class TestMigrationShape:
    """The migration lands all three tables with correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _PAYROLL_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_pay_rule_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("pay_rule")}
        expected = {
            "id",
            "workspace_id",
            "user_id",
            "currency",
            "base_cents_per_hour",
            "overtime_multiplier",
            "night_multiplier",
            "weekend_multiplier",
            "effective_from",
            "effective_to",
            "created_by",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("effective_to", "created_by"):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {"effective_to", "created_by"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_pay_rule_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("pay_rule")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("user_id",)]["referred_table"] == "user"
        # ``RESTRICT`` — pay_rule rates feed every payslip that cites
        # this window; a raw ``DELETE FROM user`` must not silently
        # disconnect that evidence. Erasure routes through
        # ``crewday admin purge --person`` (§15) which anonymises.
        assert fks[("user_id",)]["options"].get("ondelete") == "RESTRICT"
        assert ("created_by",) not in fks

    def test_pay_rule_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("pay_rule")}
        assert "ix_pay_rule_workspace_user_effective_from" in indexes
        assert indexes["ix_pay_rule_workspace_user_effective_from"]["column_names"] == [
            "workspace_id",
            "user_id",
            "effective_from",
        ]

    def test_pay_period_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("pay_period")}
        expected = {
            "id",
            "workspace_id",
            "starts_at",
            "ends_at",
            "state",
            "locked_at",
            "locked_by",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("locked_at", "locked_by"):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"

    def test_pay_period_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("pay_period")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # ``locked_by`` is a soft-ref; no FK.
        assert ("locked_by",) not in fks

    def test_pay_period_unique_window(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u for u in inspect(engine).get_unique_constraints("pay_period")
        }
        assert "uq_pay_period_workspace_window" in uniques
        assert uniques["uq_pay_period_workspace_window"]["column_names"] == [
            "workspace_id",
            "starts_at",
            "ends_at",
        ]

    def test_payslip_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("payslip")}
        expected = {
            "id",
            "workspace_id",
            "pay_period_id",
            "user_id",
            "status",
            "issued_at",
            "paid_at",
            "shift_hours_decimal",
            "overtime_hours_decimal",
            "gross_cents",
            "deductions_cents",
            "net_cents",
            "pdf_blob_hash",
            "payout_snapshot_json",
            "payout_manifest_purged_at",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in (
            "pdf_blob_hash",
            "issued_at",
            "paid_at",
            "payout_snapshot_json",
            "payout_manifest_purged_at",
        ):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {
            "pdf_blob_hash",
            "issued_at",
            "paid_at",
            "payout_snapshot_json",
            "payout_manifest_purged_at",
        }:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_payslip_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("payslip")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("pay_period_id",)]["referred_table"] == "pay_period"
        assert fks[("pay_period_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("user_id",)]["referred_table"] == "user"
        assert fks[("user_id",)]["options"].get("ondelete") == "RESTRICT"

    def test_payslip_unique_per_period_user(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u for u in inspect(engine).get_unique_constraints("payslip")
        }
        assert "uq_payslip_pay_period_user" in uniques
        assert uniques["uq_payslip_pay_period_user"]["column_names"] == [
            "pay_period_id",
            "user_id",
        ]


class TestPayRuleCrud:
    """Insert + select + update + delete round-trip on :class:`PayRule`."""

    def test_round_trip_and_current_rule_query(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="pay-rule-crud@example.com",
            display="PayRuleCrud",
            slug="pay-rule-crud-ws",
            name="PayRuleCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            older = PayRule(
                id="01HWA00000000000000000PR01",
                workspace_id=workspace.id,
                user_id=user.id,
                currency="EUR",
                base_cents_per_hour=1500,
                effective_from=_EFFECTIVE_FROM - timedelta(days=365),
                effective_to=_EFFECTIVE_FROM - timedelta(days=1),
                created_at=_PINNED,
            )
            current = PayRule(
                id="01HWA00000000000000000PR02",
                workspace_id=workspace.id,
                user_id=user.id,
                currency="EUR",
                base_cents_per_hour=1800,
                overtime_multiplier=Decimal("1.75"),
                night_multiplier=Decimal("1.30"),
                weekend_multiplier=Decimal("2.00"),
                effective_from=_EFFECTIVE_FROM,
                created_by=user.id,
                created_at=_PINNED,
            )
            db_session.add_all([older, current])
            db_session.flush()

            # "Current rule for (workspace, user)" — the index's target.
            rows = db_session.scalars(
                select(PayRule)
                .where(PayRule.workspace_id == workspace.id)
                .where(PayRule.user_id == user.id)
                .order_by(PayRule.effective_from.desc())
            ).all()
            assert [r.id for r in rows] == [
                "01HWA00000000000000000PR02",
                "01HWA00000000000000000PR01",
            ]

            # Reload current; ``Numeric`` comes back as ``Decimal``.
            reloaded = db_session.get(PayRule, current.id)
            assert reloaded is not None
            assert isinstance(reloaded.overtime_multiplier, Decimal)
            assert reloaded.overtime_multiplier == Decimal("1.75")
            assert reloaded.base_cents_per_hour == 1800

            # Update: close the current rule.
            reloaded.effective_to = _EFFECTIVE_TO
            db_session.flush()
            db_session.expire_all()
            re_reloaded = db_session.get(PayRule, current.id)
            assert re_reloaded is not None
            assert re_reloaded.effective_to is not None

            # Delete the older rule.
            db_session.delete(older)
            db_session.flush()
            assert db_session.get(PayRule, older.id) is None
        finally:
            reset_current(token)


class TestPayPeriodCrud:
    """Insert + select + update + delete round-trip on :class:`PayPeriod`."""

    def test_round_trip_and_state_transitions(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="pay-period-crud@example.com",
            display="PayPeriodCrud",
            slug="pay-period-crud-ws",
            name="PayPeriodCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PP01",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()

            loaded = db_session.get(PayPeriod, period.id)
            assert loaded is not None
            assert loaded.state == "open"
            assert loaded.locked_at is None

            # State transition: open -> locked.
            loaded.state = "locked"
            loaded.locked_at = _PINNED + timedelta(days=32)
            loaded.locked_by = user.id
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(PayPeriod, period.id)
            assert reloaded is not None
            assert reloaded.state == "locked"
            assert reloaded.locked_by == user.id
            assert reloaded.locked_at is not None

            # State transition: locked -> paid.
            reloaded.state = "paid"
            db_session.flush()
            db_session.expire_all()
            paid = db_session.get(PayPeriod, period.id)
            assert paid is not None
            assert paid.state == "paid"

            db_session.delete(paid)
            db_session.flush()
            assert db_session.get(PayPeriod, period.id) is None
        finally:
            reset_current(token)


class TestPayslipCrud:
    """Insert + select + update + delete round-trip on :class:`Payslip`."""

    def test_round_trip_with_json_deductions(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="payslip-crud@example.com",
            display="PayslipCrud",
            slug="payslip-crud-ws",
            name="PayslipCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PP02",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()

            slip = Payslip(
                id="01HWA00000000000000000PS01",
                workspace_id=workspace.id,
                pay_period_id=period.id,
                user_id=user.id,
                shift_hours_decimal=Decimal("151.67"),
                overtime_hours_decimal=Decimal("12.00"),
                gross_cents=400000,
                deductions_cents={"tax": 80000, "advance": 20000},
                net_cents=300000,
                created_at=_PINNED,
            )
            db_session.add(slip)
            db_session.flush()

            reloaded = db_session.get(Payslip, slip.id)
            assert reloaded is not None
            assert isinstance(reloaded.shift_hours_decimal, Decimal)
            assert reloaded.shift_hours_decimal == Decimal("151.67")
            assert reloaded.overtime_hours_decimal == Decimal("12.00")
            assert reloaded.gross_cents == 400000
            assert reloaded.net_cents == 300000
            # JSON round-trip: dict comes back intact.
            assert reloaded.deductions_cents == {
                "tax": 80000,
                "advance": 20000,
            }
            assert reloaded.pdf_blob_hash is None
            assert reloaded.status == "draft"
            assert reloaded.issued_at is None
            assert reloaded.paid_at is None

            # Issue and pay the payslip.
            reloaded.pdf_blob_hash = "sha256-deadbeefcafebabe"
            reloaded.status = "issued"
            reloaded.issued_at = _PINNED + timedelta(days=32)
            reloaded.status = "paid"
            reloaded.paid_at = _PINNED + timedelta(days=33)
            db_session.flush()
            db_session.expire_all()

            issued = db_session.get(Payslip, slip.id)
            assert issued is not None
            assert issued.pdf_blob_hash == "sha256-deadbeefcafebabe"
            assert issued.status == "paid"
            assert issued.paid_at is not None

            db_session.delete(issued)
            db_session.flush()
            assert db_session.get(Payslip, slip.id) is None
        finally:
            reset_current(token)

    def test_empty_deductions_default_round_trip(self, db_session: Session) -> None:
        """A payslip with ``deductions_cents={}`` round-trips as a dict."""
        workspace, user = _bootstrap(
            db_session,
            email="payslip-empty@example.com",
            display="PayslipEmpty",
            slug="payslip-empty-ws",
            name="PayslipEmptyWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PP03",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            slip = Payslip(
                id="01HWA00000000000000000PS02",
                workspace_id=workspace.id,
                pay_period_id=period.id,
                user_id=user.id,
                shift_hours_decimal=Decimal("40.00"),
                overtime_hours_decimal=Decimal("0"),
                gross_cents=80000,
                deductions_cents={},
                net_cents=80000,
                created_at=_PINNED,
            )
            db_session.add_all([period, slip])
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(Payslip, slip.id)
            assert reloaded is not None
            assert reloaded.deductions_cents == {}
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums / bounds."""

    def test_bogus_currency_length_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-curr@example.com",
            display="BogusCurr",
            slug="bogus-curr-ws",
            name="BogusCurrWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRX1",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EURO",  # 4 chars
                    base_cents_per_hour=1500,
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_two_char_currency_rejected(self, db_session: Session) -> None:
        """Two-letter currency also trips ``LENGTH(currency) = 3``."""
        workspace, user = _bootstrap(
            db_session,
            email="short-curr@example.com",
            display="ShortCurr",
            slug="short-curr-ws",
            name="ShortCurrWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRX2",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EU",
                    base_cents_per_hour=1500,
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_negative_base_cents_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-base@example.com",
            display="NegBase",
            slug="neg-base-ws",
            name="NegBaseWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRX3",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EUR",
                    base_cents_per_hour=-1,
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_overtime_multiplier_below_one_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="ot-low@example.com",
            display="OtLow",
            slug="ot-low-ws",
            name="OtLowWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRX4",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EUR",
                    base_cents_per_hour=1500,
                    overtime_multiplier=Decimal("0.5"),
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_night_multiplier_below_one_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="night-low@example.com",
            display="NightLow",
            slug="night-low-ws",
            name="NightLowWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRX5",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EUR",
                    base_cents_per_hour=1500,
                    night_multiplier=Decimal("0.9"),
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_weekend_multiplier_below_one_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="wk-low@example.com",
            display="WkLow",
            slug="wk-low-ws",
            name="WkLowWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRX6",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EUR",
                    base_cents_per_hour=1500,
                    weekend_multiplier=Decimal("0.75"),
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_period_state_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-state@example.com",
            display="BogusState",
            slug="bogus-state-ws",
            name="BogusStateWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayPeriod(
                    id="01HWA00000000000000000PPX1",
                    workspace_id=workspace.id,
                    starts_at=_PERIOD_START,
                    ends_at=_PERIOD_END,
                    state="maybe_later",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_period_ends_before_starts_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="period-reverse@example.com",
            display="PeriodReverse",
            slug="period-reverse-ws",
            name="PeriodReverseWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayPeriod(
                    id="01HWA00000000000000000PPX2",
                    workspace_id=workspace.id,
                    starts_at=_PERIOD_END,
                    ends_at=_PERIOD_START,
                    state="open",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_period_zero_length_rejected(self, db_session: Session) -> None:
        """``ends_at > starts_at`` — zero-length period is a data bug."""
        workspace, user = _bootstrap(
            db_session,
            email="period-zero@example.com",
            display="PeriodZero",
            slug="period-zero-ws",
            name="PeriodZeroWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayPeriod(
                    id="01HWA00000000000000000PPX3",
                    workspace_id=workspace.id,
                    starts_at=_PERIOD_START,
                    ends_at=_PERIOD_START,
                    state="open",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_payslip_negative_shift_hours_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-shift@example.com",
            display="NegShift",
            slug="neg-shift-ws",
            name="NegShiftWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPX4",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSX1",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("-1"),
                    overtime_hours_decimal=Decimal("0"),
                    gross_cents=0,
                    deductions_cents={},
                    net_cents=0,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_payslip_negative_overtime_hours_rejected(
        self, db_session: Session
    ) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-ot@example.com",
            display="NegOt",
            slug="neg-ot-ws",
            name="NegOtWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPX5",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSX2",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("0"),
                    overtime_hours_decimal=Decimal("-0.5"),
                    gross_cents=0,
                    deductions_cents={},
                    net_cents=0,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_payslip_negative_gross_cents_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-gross@example.com",
            display="NegGross",
            slug="neg-gross-ws",
            name="NegGrossWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPX6",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSX3",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("0"),
                    overtime_hours_decimal=Decimal("0"),
                    gross_cents=-100,
                    deductions_cents={},
                    net_cents=-100,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestUniqueConstraints:
    """UNIQUE composites enforce the v1 invariants."""

    def test_duplicate_pay_period_window_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="period-dup@example.com",
            display="PeriodDup",
            slug="period-dup-ws",
            name="PeriodDupWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayPeriod(
                    id="01HWA00000000000000000PPD1",
                    workspace_id=workspace.id,
                    starts_at=_PERIOD_START,
                    ends_at=_PERIOD_END,
                    state="open",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                PayPeriod(
                    id="01HWA00000000000000000PPD2",
                    workspace_id=workspace.id,
                    starts_at=_PERIOD_START,
                    ends_at=_PERIOD_END,
                    state="open",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_duplicate_payslip_per_period_user_rejected(
        self, db_session: Session
    ) -> None:
        """The key acceptance: one payslip per (period, user)."""
        workspace, user = _bootstrap(
            db_session,
            email="payslip-dup@example.com",
            display="PayslipDup",
            slug="payslip-dup-ws",
            name="PayslipDupWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPD3",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()

            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSD1",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("40"),
                    overtime_hours_decimal=Decimal("0"),
                    gross_cents=80000,
                    deductions_cents={},
                    net_cents=80000,
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSD2",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("50"),
                    overtime_hours_decimal=Decimal("0"),
                    gross_cents=100000,
                    deductions_cents={},
                    net_cents=100000,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_different_users_share_period(self, db_session: Session) -> None:
        """Different users each get their own payslip in the same period."""
        workspace, user_a = _bootstrap(
            db_session,
            email="payslip-share-a@example.com",
            display="PayslipShareA",
            slug="payslip-share-ws",
            name="PayslipShareWS",
        )
        user_b = bootstrap_user(
            db_session,
            email="payslip-share-b@example.com",
            display_name="PayslipShareB",
            clock=FrozenClock(_PINNED),
        )
        token = set_current(_ctx_for(workspace, user_a.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPDS",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            db_session.add_all(
                [
                    Payslip(
                        id="01HWA00000000000000000PSS1",
                        workspace_id=workspace.id,
                        pay_period_id=period.id,
                        user_id=user_a.id,
                        shift_hours_decimal=Decimal("40"),
                        overtime_hours_decimal=Decimal("0"),
                        gross_cents=80000,
                        deductions_cents={},
                        net_cents=80000,
                        created_at=_PINNED,
                    ),
                    Payslip(
                        id="01HWA00000000000000000PSS2",
                        workspace_id=workspace.id,
                        pay_period_id=period.id,
                        user_id=user_b.id,
                        shift_hours_decimal=Decimal("50"),
                        overtime_hours_decimal=Decimal("0"),
                        gross_cents=100000,
                        deductions_cents={},
                        net_cents=100000,
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()
            rows = db_session.scalars(
                select(Payslip).where(Payslip.pay_period_id == period.id)
            ).all()
            assert {r.user_id for r in rows} == {user_a.id, user_b.id}
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps every payroll row belonging to it."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="cascade-payroll@example.com",
            display="CascadePayroll",
            slug="cascade-payroll-ws",
            name="CascadePayrollWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRCD",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EUR",
                    base_cents_per_hour=1500,
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            period = PayPeriod(
                id="01HWA00000000000000000PPCD",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSCD",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("40"),
                    overtime_hours_decimal=Decimal("0"),
                    gross_cents=60000,
                    deductions_cents={},
                    net_cents=60000,
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # justification: workspace delete is a platform-level op; no
        # :class:`WorkspaceContext` applies once the tenant itself is
        # the target.
        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        token = set_current(_ctx_for(workspace, user.id))
        try:
            assert (
                db_session.scalars(
                    select(PayRule).where(PayRule.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(PayPeriod).where(PayPeriod.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(Payslip).where(Payslip.workspace_id == workspace.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestCascadeOnPayPeriodDelete:
    """Deleting a ``pay_period`` sweeps its draft payslips."""

    def test_delete_pay_period_cascades_to_payslip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cascade-period@example.com",
            display="CascadePeriod",
            slug="cascade-period-ws",
            name="CascadePeriodWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPCP",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            slip = Payslip(
                id="01HWA00000000000000000PSCP",
                workspace_id=workspace.id,
                pay_period_id=period.id,
                user_id=user.id,
                shift_hours_decimal=Decimal("40"),
                overtime_hours_decimal=Decimal("0"),
                gross_cents=60000,
                deductions_cents={},
                net_cents=60000,
                created_at=_PINNED,
            )
            db_session.add(slip)
            db_session.flush()

            slip_id = slip.id
            db_session.delete(period)
            db_session.flush()
            # Cascade swept the dependent payslip at the DB level. The
            # ORM identity map still references the stale instance; drop
            # it before re-querying so ``get`` doesn't refresh-raise, and
            # observe absence via a fresh SELECT.
            db_session.expunge(slip)
            survivors = db_session.scalars(
                select(Payslip).where(Payslip.id == slip_id)
            ).all()
            assert survivors == []
            assert db_session.get(PayPeriod, period.id) is None
        finally:
            reset_current(token)


class TestRestrictOnUserDelete:
    """Hard-deleting a user is blocked while pay_rule / payslip rows exist.

    The FK cascade is ``RESTRICT``, not ``CASCADE`` — labour-law
    records (§09) outlive the user's credentials. The normal
    erasure path is ``crewday admin purge --person`` (§15) which
    anonymises the user row in place and keeps the FK reference
    valid; a raw ``DELETE FROM user`` is the unusual path and must
    be stopped here rather than silently taking the evidence with it.
    """

    def test_delete_user_with_pay_rule_raises(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="restrict-rule@example.com",
            display="RestrictRule",
            slug="restrict-rule-ws",
            name="RestrictRuleWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                PayRule(
                    id="01HWA00000000000000000PRUR",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    currency="EUR",
                    base_cents_per_hour=1500,
                    effective_from=_EFFECTIVE_FROM,
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        with tenant_agnostic():
            loaded_user = db_session.get(User, user.id)
            assert loaded_user is not None
            db_session.delete(loaded_user)
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()

    def test_delete_user_with_payslip_raises(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="restrict-slip@example.com",
            display="RestrictSlip",
            slug="restrict-slip-ws",
            name="RestrictSlipWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPUR",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            db_session.add(
                Payslip(
                    id="01HWA00000000000000000PSUR",
                    workspace_id=workspace.id,
                    pay_period_id=period.id,
                    user_id=user.id,
                    shift_hours_decimal=Decimal("40"),
                    overtime_hours_decimal=Decimal("0"),
                    gross_cents=60000,
                    deductions_cents={},
                    net_cents=60000,
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        with tenant_agnostic():
            loaded_user = db_session.get(User, user.id)
            assert loaded_user is not None
            db_session.delete(loaded_user)
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()


class TestNetCanBeNegative:
    """``net_cents`` may legitimately be negative (cash-advance repayment)."""

    def test_negative_net_cents_accepted(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-net@example.com",
            display="NegNet",
            slug="neg-net-ws",
            name="NegNetWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            period = PayPeriod(
                id="01HWA00000000000000000PPNN",
                workspace_id=workspace.id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                state="open",
                created_at=_PINNED,
            )
            db_session.add(period)
            db_session.flush()
            slip = Payslip(
                id="01HWA00000000000000000PSNN",
                workspace_id=workspace.id,
                pay_period_id=period.id,
                user_id=user.id,
                shift_hours_decimal=Decimal("10"),
                overtime_hours_decimal=Decimal("0"),
                gross_cents=10000,
                # Deductions exceed gross — e.g. a 50_000-cent advance
                # is being clawed back over multiple periods.
                deductions_cents={"advance": 50000},
                net_cents=-40000,
                created_at=_PINNED,
            )
            db_session.add(slip)
            db_session.flush()
            reloaded = db_session.get(Payslip, slip.id)
            assert reloaded is not None
            assert reloaded.net_cents == -40000
        finally:
            reset_current(token)


class TestTenantFilter:
    """All three payroll tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize("model", [PayRule, PayPeriod, Payslip])
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[PayRule] | type[PayPeriod] | type[Payslip],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
