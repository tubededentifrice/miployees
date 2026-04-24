"""Public surface of the employees context.

Re-exports the employees service so callers in other bounded
contexts (API handlers, membership acceptance, worker invites)
import from here rather than reaching directly into
:mod:`app.services.employees.service`. Keeps the concrete module
free to restructure its internals.

The employees service owns the lifecycle of a *worker* within a
workspace — profile edits, archive, reinstate, and the accept-time
seed of a pending :class:`WorkEngagement` row when an invite
completes. Invite creation itself stays inside
:mod:`app.domain.identity.membership` (nothing workspace-scoped is
seeded until the invitee completes their passkey challenge per §03);
the employees service is called from ``_activate_invite`` to land
that seed in the same transaction.

See ``docs/specs/05-employees-and-roles.md`` §"User (as worker)",
§"Work engagement", §"Archive / reinstate" and
``docs/specs/01-architecture.md`` §"Contexts & boundaries".
"""

from __future__ import annotations

from app.services.employees.service import (
    EmployeeNotFound,
    EmployeeProfileUpdate,
    EmployeeView,
    ProfileFieldForbidden,
    archive_employee,
    get_employee,
    iter_active_engagements,
    reinstate_employee,
    seed_pending_work_engagement,
    update_profile,
)

__all__ = [
    "EmployeeNotFound",
    "EmployeeProfileUpdate",
    "EmployeeView",
    "ProfileFieldForbidden",
    "archive_employee",
    "get_employee",
    "iter_active_engagements",
    "reinstate_employee",
    "seed_pending_work_engagement",
    "update_profile",
]
