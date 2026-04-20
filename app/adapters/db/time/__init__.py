"""time — shift / leave / geofence_setting.

All three tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A
bare read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

Unlike the places package — where ``property`` intentionally stays
tenant-agnostic because a single villa may belong to several
workspaces — every time row is born inside exactly one workspace's
operations (a shift is logged by a specific workspace's worker, a
leave request belongs to a workspace's manager queue, a geofence
setting configures a property within a workspace's
``property_workspace`` assignment), so scoping is unambiguous.

``property_id`` is persisted as a soft-ref :class:`str` column (no
SQL foreign key) on every table. The domain layer will promote it
to a hard FK once the §05 workforce / §04 places intersection
settles — for now we mirror the pattern used elsewhere for
deliberately-uncertain cross-context refs (``session.workspace_id``,
``audit_log`` pointers).

See ``docs/specs/02-domain-model.md`` §"shift", §"leave",
§"geofence_setting", and ``docs/specs/09-time-payroll-expenses.md``.
"""

from __future__ import annotations

from app.adapters.db.time.models import GeofenceSetting, Leave, Shift
from app.tenancy.registry import register

for _table in ("shift", "leave", "geofence_setting"):
    register(_table)

__all__ = ["GeofenceSetting", "Leave", "Shift"]
