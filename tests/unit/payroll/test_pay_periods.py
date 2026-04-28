"""Unit tests for :mod:`app.domain.payroll.periods` (cd-73i)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.payroll.periods import (
    PayPeriodInvariantViolated,
    PayPeriodTransitionConflict,
    create_period,
    delete_period,
    lock_period,
    mark_paid,
    reopen_period,
)
from app.domain.payroll.ports import PayPeriodRepository, PayPeriodRow
from app.events.bus import EventBus
from app.events.types import PayPeriodLocked, PayPeriodPaid
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_START = datetime(2026, 5, 1, tzinfo=UTC)
_END = datetime(2026, 6, 1, tzinfo=UTC)
_WS_ID = "01HWA00000000000000000WS01"
_ACTOR_ID = "01HWA00000000000000000USR1"


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WS_ID,
        workspace_slug="ws",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)


class _FakeRepo(PayPeriodRepository):
    def __init__(self) -> None:
        self.rows: dict[str, PayPeriodRow] = {}
        self._session = _FakeSession()
        self.paid_payslip = False
        self.unpaid_payslip = False
        self.deleted: list[str] = []

    @property
    def session(self) -> Any:
        return self._session

    def get(self, *, workspace_id: str, period_id: str) -> PayPeriodRow | None:
        row = self.rows.get(period_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return row

    def list(self, *, workspace_id: str) -> Sequence[PayPeriodRow]:
        return [r for r in self.rows.values() if r.workspace_id == workspace_id]

    def has_overlap(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        exclude_period_id: str | None = None,
    ) -> bool:
        rows = [r for r in self.rows.values() if r.workspace_id == workspace_id]
        if exclude_period_id is not None:
            rows = [r for r in rows if r.id != exclude_period_id]
        return any(r.starts_at < ends_at and r.ends_at > starts_at for r in rows)

    def insert(
        self,
        *,
        period_id: str,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> PayPeriodRow:
        row = PayPeriodRow(
            id=period_id,
            workspace_id=workspace_id,
            starts_at=starts_at,
            ends_at=ends_at,
            state="open",
            locked_at=None,
            locked_by=None,
            created_at=now,
        )
        self.rows[period_id] = row
        return row

    def lock(
        self,
        *,
        workspace_id: str,
        period_id: str,
        locked_at: datetime,
        locked_by: str | None,
    ) -> PayPeriodRow:
        row = self.rows[period_id]
        updated = PayPeriodRow(
            id=row.id,
            workspace_id=row.workspace_id,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            state="locked",
            locked_at=locked_at,
            locked_by=locked_by,
            created_at=row.created_at,
        )
        self.rows[period_id] = updated
        return updated

    def reopen(self, *, workspace_id: str, period_id: str) -> PayPeriodRow:
        row = self.rows[period_id]
        updated = PayPeriodRow(
            id=row.id,
            workspace_id=row.workspace_id,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            state="open",
            locked_at=None,
            locked_by=None,
            created_at=row.created_at,
        )
        self.rows[period_id] = updated
        return updated

    def mark_paid(self, *, workspace_id: str, period_id: str) -> PayPeriodRow:
        row = self.rows[period_id]
        updated = PayPeriodRow(
            id=row.id,
            workspace_id=row.workspace_id,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            state="paid",
            locked_at=row.locked_at,
            locked_by=row.locked_by,
            created_at=row.created_at,
        )
        self.rows[period_id] = updated
        return updated

    def delete(self, *, workspace_id: str, period_id: str) -> None:
        self.deleted.append(period_id)
        del self.rows[period_id]

    def has_paid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        return self.paid_payslip

    def has_unpaid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        return self.unpaid_payslip


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def schedule_period_recompute(self, *, workspace_id: str, period_id: str) -> None:
        self.calls.append((workspace_id, period_id))


@pytest.fixture(autouse=True)
def _allow_authz(monkeypatch: pytest.MonkeyPatch) -> None:
    def _allow(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.domain.payroll.periods.require", _allow)


def _seed(repo: _FakeRepo, *, state: str = "open") -> PayPeriodRow:
    row = PayPeriodRow(
        id="01HWA00000000000000000PP01",
        workspace_id=_WS_ID,
        starts_at=_START,
        ends_at=_END,
        state=state,
        locked_at=_PINNED if state in {"locked", "paid"} else None,
        locked_by=_ACTOR_ID if state in {"locked", "paid"} else None,
        created_at=_PINNED,
    )
    repo.rows[row.id] = row
    return row


def test_create_rejects_overlap_and_bad_window() -> None:
    repo = _FakeRepo()
    _seed(repo)

    with pytest.raises(PayPeriodInvariantViolated):
        create_period(
            repo,
            _ctx(),
            starts_at=_START + timedelta(days=1),
            ends_at=_END + timedelta(days=1),
            clock=FrozenClock(_PINNED),
        )

    with pytest.raises(PayPeriodInvariantViolated):
        create_period(
            _FakeRepo(),
            _ctx(),
            starts_at=_END,
            ends_at=_START,
            clock=FrozenClock(_PINNED),
        )


def test_lock_period_schedules_recompute_audits_and_emits_event() -> None:
    repo = _FakeRepo()
    period = _seed(repo)
    scheduler = _Scheduler()
    bus = EventBus()
    events: list[PayPeriodLocked] = []

    @bus.subscribe(PayPeriodLocked)
    def _capture(event: PayPeriodLocked) -> None:
        events.append(event)

    locked = lock_period(
        repo,
        _ctx(),
        period_id=period.id,
        recompute_scheduler=scheduler,
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert locked.state == "locked"
    assert locked.locked_by == _ACTOR_ID
    assert scheduler.calls == [(_WS_ID, period.id)]
    assert len(repo.session.added) == 1
    assert repo.session.added[0].action == "pay_period.locked"
    assert [e.pay_period_id for e in events] == [period.id]


def test_bad_transitions_raise_conflict() -> None:
    repo = _FakeRepo()
    period = _seed(repo, state="paid")

    with pytest.raises(PayPeriodTransitionConflict):
        lock_period(
            repo,
            _ctx(),
            period_id=period.id,
            recompute_scheduler=_Scheduler(),
            clock=FrozenClock(_PINNED),
        )

    with pytest.raises(PayPeriodTransitionConflict):
        delete_period(repo, _ctx(), period_id=period.id, clock=FrozenClock(_PINNED))


def test_reopen_refuses_paid_payslip() -> None:
    repo = _FakeRepo()
    period = _seed(repo, state="locked")
    repo.paid_payslip = True

    with pytest.raises(PayPeriodTransitionConflict):
        reopen_period(repo, _ctx(), period_id=period.id, clock=FrozenClock(_PINNED))


def test_mark_paid_requires_every_payslip_paid_and_emits_event() -> None:
    repo = _FakeRepo()
    period = _seed(repo, state="locked")
    repo.unpaid_payslip = True

    with pytest.raises(PayPeriodTransitionConflict):
        mark_paid(repo, _ctx(), period_id=period.id, clock=FrozenClock(_PINNED))

    repo.unpaid_payslip = False
    bus = EventBus()
    events: list[PayPeriodPaid] = []

    @bus.subscribe(PayPeriodPaid)
    def _capture(event: PayPeriodPaid) -> None:
        events.append(event)

    paid = mark_paid(
        repo,
        _ctx(),
        period_id=period.id,
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert paid.state == "paid"
    assert repo.session.added[-1].action == "pay_period.paid"
    assert [e.pay_period_id for e in events] == [period.id]
