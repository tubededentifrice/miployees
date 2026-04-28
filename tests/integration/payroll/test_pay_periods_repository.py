"""Integration tests for pay-period repository state guards (cd-73i)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.payroll.models import PayPeriod, Payslip
from app.adapters.db.payroll.repositories import SqlAlchemyPayPeriodRepository
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_START = datetime(2026, 5, 1, tzinfo=UTC)
_END = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed(session: Session) -> tuple[str, str, str]:
    tag = new_ulid()[-8:].lower()
    user = bootstrap_user(
        session,
        email=f"payperiod-{tag}@example.com",
        display_name="Pay Period",
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"payperiod-{tag}",
        name="Pay Period",
        owner_user_id=user.id,
    )
    period = PayPeriod(
        id=new_ulid(),
        workspace_id=workspace.id,
        starts_at=_START,
        ends_at=_END,
        state="locked",
        locked_at=_PINNED,
        locked_by=user.id,
        created_at=_PINNED,
    )
    session.add(period)
    session.flush()
    return workspace.id, user.id, period.id


def _slip(
    *,
    workspace_id: str,
    user_id: str,
    period_id: str,
    status: str,
    paid_at: datetime | None,
) -> Payslip:
    return Payslip(
        id=new_ulid(),
        workspace_id=workspace_id,
        pay_period_id=period_id,
        user_id=user_id,
        shift_hours_decimal=Decimal("160.00"),
        overtime_hours_decimal=Decimal("0.00"),
        gross_cents=240_000,
        deductions_cents={},
        net_cents=240_000,
        status=status,
        paid_at=paid_at,
        created_at=_PINNED,
    )


def test_paid_and_unpaid_payslip_guards(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, period_id = _seed(session)
        repo = SqlAlchemyPayPeriodRepository(session)

        session.add(
            _slip(
                workspace_id=workspace_id,
                user_id=user_id,
                period_id=period_id,
                status="draft",
                paid_at=None,
            )
        )
        session.flush()

        assert repo.has_unpaid_payslip(
            workspace_id=workspace_id,
            period_id=period_id,
        )
        assert not repo.has_paid_payslip(
            workspace_id=workspace_id,
            period_id=period_id,
        )

        for slip in session.query(Payslip).filter_by(pay_period_id=period_id):
            slip.status = "paid"
            slip.paid_at = _PINNED + timedelta(days=40)
        session.flush()

        assert not repo.has_unpaid_payslip(
            workspace_id=workspace_id,
            period_id=period_id,
        )
        assert repo.has_paid_payslip(
            workspace_id=workspace_id,
            period_id=period_id,
        )


def test_reopen_resets_contained_payslips(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, period_id = _seed(session)
        repo = SqlAlchemyPayPeriodRepository(session)
        session.add(
            _slip(
                workspace_id=workspace_id,
                user_id=user_id,
                period_id=period_id,
                status="issued",
                paid_at=None,
            )
        )
        session.flush()

        reopened = repo.reopen(workspace_id=workspace_id, period_id=period_id)

        assert reopened.state == "open"
        slip = session.query(Payslip).filter_by(pay_period_id=period_id).one()
        assert slip.status == "draft"
        assert slip.issued_at is None
        assert slip.paid_at is None
