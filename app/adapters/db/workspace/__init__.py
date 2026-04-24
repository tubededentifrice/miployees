"""workspace + user_workspace + work_role + user_work_role tables.

Importing this package:

* Registers ``user_workspace``, ``work_role``, and
  ``user_work_role`` as workspace-scoped tables (so the ORM tenant
  filter auto-injects a ``workspace_id`` predicate on every SELECT /
  UPDATE / DELETE against them — see
  :mod:`app.tenancy.orm_filter`).
* Does **not** register ``workspace``. The slug→id resolver in the
  signup + request middleware has to scan this table *before* any
  :class:`~app.tenancy.WorkspaceContext` exists, so the table is
  tenant-agnostic by design (see
  ``docs/specs/01-architecture.md`` §"Workspace addressing").

See ``docs/specs/02-domain-model.md`` §"workspaces" and
§"user_workspace"; ``docs/specs/05-employees-and-roles.md`` §"Work
role" / §"User work role".
"""

from __future__ import annotations

from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkRole,
    Workspace,
)
from app.tenancy.registry import register

# ``workspace`` is intentionally NOT registered — the tenancy anchor
# is tenant-agnostic by design (slug lookup before ctx exists).
for _table in ("user_workspace", "work_role", "user_work_role"):
    register(_table)

__all__ = ["UserWorkRole", "UserWorkspace", "WorkRole", "Workspace"]
