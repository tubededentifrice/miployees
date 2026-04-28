"""payroll — pay_rule / pay_period / payslip.

All three tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A
bare read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

Unlike the places package — where ``property`` intentionally stays
tenant-agnostic — every payroll row belongs to exactly one
workspace's payroll cycle (the rule binds a workspace's user to a
rate, the period is the workspace's payroll window, the payslip is
the workspace's issued document), so scoping is unambiguous.

``created_by`` / ``locked_by`` are persisted as soft-ref
:class:`str` columns (no SQL foreign key) — the actor on a payroll
mutation may be a user or a system process (the period-close
worker, a scheduled billing job), and audit-linkage semantics live
in :mod:`app.adapters.db.audit`, not here.

See ``docs/specs/02-domain-model.md`` §"pay_rule", §"pay_period",
§"payslip", and ``docs/specs/09-time-payroll-expenses.md`` §"Pay
rules", §"Pay period", §"Payslip".
"""

from __future__ import annotations

from app.adapters.db.payroll.models import PayPeriod, PayRule, PayoutDestination, Payslip
from app.tenancy.registry import register

for _table in ("pay_rule", "pay_period", "payslip", "payout_destination"):
    register(_table)

__all__ = ["PayPeriod", "PayRule", "PayoutDestination", "Payslip"]
