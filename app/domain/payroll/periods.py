"""Pay-period lifecycle service (§09 "Pay period")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.audit import write_audit
from app.authz import require
from app.domain.payroll.ports import (
    PayPeriodRecomputeScheduler,
    PayPeriodRepository,
    PayPeriodRow,
)
from app.events import EventBus
from app.events import bus as default_event_bus
from app.events.types import PayPeriodLocked, PayPeriodPaid
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "PayPeriodInvariantViolated",
    "PayPeriodNotFound",
    "PayPeriodTransitionConflict",
    "PayPeriodView",
    "create_period",
    "delete_period",
    "get_period",
    "list_periods",
    "lock_period",
    "mark_paid",
    "reopen_period",
]


class PayPeriodNotFound(LookupError):
    """The requested pay period is absent from the caller's workspace."""


class PayPeriodInvariantViolated(ValueError):
    """A pay-period write would violate a §09 invariant."""


class PayPeriodTransitionConflict(RuntimeError):
    """The requested state transition is not legal from the current state."""


@dataclass(frozen=True, slots=True)
class PayPeriodView:
    id: str
    workspace_id: str
    starts_at: datetime
    ends_at: datetime
    state: str
    locked_at: datetime | None
    locked_by: str | None
    created_at: datetime


def _row_to_view(row: PayPeriodRow) -> PayPeriodView:
    return PayPeriodView(
        id=row.id,
        workspace_id=row.workspace_id,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        state=row.state,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        created_at=row.created_at,
    )


def _view_to_diff_dict(view: PayPeriodView) -> dict[str, Any]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "starts_at": view.starts_at.isoformat(),
        "ends_at": view.ends_at.isoformat(),
        "state": view.state,
        "locked_at": view.locked_at.isoformat() if view.locked_at else None,
        "locked_by": view.locked_by,
        "created_at": view.created_at.isoformat(),
    }


def _enforce_manage(repo: PayPeriodRepository, ctx: WorkspaceContext) -> None:
    require(
        repo.session,
        ctx,
        action_key="payroll.lock_period",
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )


def _validate_window(*, starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise PayPeriodInvariantViolated("ends_at must be strictly after starts_at")


def _load_row(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
) -> PayPeriodRow:
    row = repo.get(workspace_id=ctx.workspace_id, period_id=period_id)
    if row is None:
        raise PayPeriodNotFound(period_id)
    return row


def list_periods(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
) -> Sequence[PayPeriodView]:
    _enforce_manage(repo, ctx)
    return [_row_to_view(row) for row in repo.list(workspace_id=ctx.workspace_id)]


def get_period(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
) -> PayPeriodView:
    _enforce_manage(repo, ctx)
    return _row_to_view(_load_row(repo, ctx, period_id=period_id))


def create_period(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    starts_at: datetime,
    ends_at: datetime,
    clock: Clock | None = None,
) -> PayPeriodView:
    _enforce_manage(repo, ctx)
    _validate_window(starts_at=starts_at, ends_at=ends_at)
    if repo.has_overlap(
        workspace_id=ctx.workspace_id,
        starts_at=starts_at,
        ends_at=ends_at,
    ):
        raise PayPeriodInvariantViolated("pay period overlaps an existing period")

    resolved_clock = clock if clock is not None else SystemClock()
    row = repo.insert(
        period_id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        starts_at=starts_at,
        ends_at=ends_at,
        now=resolved_clock.now(),
    )
    view = _row_to_view(row)
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_period",
        entity_id=view.id,
        action="pay_period.created",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def delete_period(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
    clock: Clock | None = None,
) -> None:
    _enforce_manage(repo, ctx)
    row = _load_row(repo, ctx, period_id=period_id)
    if row.state != "open":
        raise PayPeriodTransitionConflict("only open pay periods can be deleted")
    view = _row_to_view(row)
    repo.delete(workspace_id=ctx.workspace_id, period_id=period_id)
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_period",
        entity_id=period_id,
        action="pay_period.deleted",
        diff={"before": _view_to_diff_dict(view)},
        clock=clock,
    )


def lock_period(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
    recompute_scheduler: PayPeriodRecomputeScheduler,
    event_bus: EventBus | None = None,
    clock: Clock | None = None,
) -> PayPeriodView:
    _enforce_manage(repo, ctx)
    row = _load_row(repo, ctx, period_id=period_id)
    if row.state != "open":
        raise PayPeriodTransitionConflict("only open pay periods can be locked")

    resolved_clock = clock if clock is not None else SystemClock()
    before = _row_to_view(row)
    after = _row_to_view(
        repo.lock(
            workspace_id=ctx.workspace_id,
            period_id=period_id,
            locked_at=resolved_clock.now(),
            locked_by=ctx.actor_id,
        )
    )
    recompute_scheduler.schedule_period_recompute(
        workspace_id=ctx.workspace_id,
        period_id=period_id,
    )
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_period",
        entity_id=period_id,
        action="pay_period.locked",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    (event_bus if event_bus is not None else default_event_bus).publish(
        PayPeriodLocked(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            pay_period_id=period_id,
        )
    )
    return after


def reopen_period(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
    clock: Clock | None = None,
) -> PayPeriodView:
    _enforce_manage(repo, ctx)
    row = _load_row(repo, ctx, period_id=period_id)
    if row.state != "locked":
        raise PayPeriodTransitionConflict("only locked pay periods can be reopened")
    if repo.has_paid_payslip(workspace_id=ctx.workspace_id, period_id=period_id):
        raise PayPeriodTransitionConflict(
            "pay periods with paid payslips cannot reopen"
        )

    before = _row_to_view(row)
    after = _row_to_view(
        repo.reopen(workspace_id=ctx.workspace_id, period_id=period_id)
    )
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_period",
        entity_id=period_id,
        action="pay_period.reopened",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )
    return after


def mark_paid(
    repo: PayPeriodRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
    event_bus: EventBus | None = None,
    clock: Clock | None = None,
) -> PayPeriodView:
    _enforce_manage(repo, ctx)
    row = _load_row(repo, ctx, period_id=period_id)
    if row.state != "locked":
        raise PayPeriodTransitionConflict("only locked pay periods can be marked paid")
    if repo.has_unpaid_payslip(workspace_id=ctx.workspace_id, period_id=period_id):
        raise PayPeriodTransitionConflict(
            "all payslips must be paid before period paid"
        )

    resolved_clock = clock if clock is not None else SystemClock()
    before = _row_to_view(row)
    after = _row_to_view(
        repo.mark_paid(workspace_id=ctx.workspace_id, period_id=period_id)
    )
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_period",
        entity_id=period_id,
        action="pay_period.paid",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    (event_bus if event_bus is not None else default_event_bus).publish(
        PayPeriodPaid(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            pay_period_id=period_id,
        )
    )
    return after
