"""Public surface of the workspace context.

Re-exports the workspace service so callers in other bounded contexts
(API handlers, the SSE invalidation seam, the workspace-picker route)
import from here rather than reaching directly into
:mod:`app.services.workspace.settings_service`. Keeps the concrete
module free to restructure its internals.

The workspace service today owns one operation: owner-only edits to
the four identity-level base columns (``name`` / ``default_timezone``
/ ``default_locale`` / ``default_currency``) per §02 "workspaces"
base columns and §05 "Surface grants at a glance" (owners as
governance anchor). Authorisation gates on ``owners`` group
membership rather than a generic capability — these are the
identity-level fields, restricted to the workspace governance anchor
even when a manager would be otherwise capable. The cascade-layer
(``settings_json`` keys) edits live elsewhere.

See ``docs/specs/02-domain-model.md`` §"workspaces" /
§"Settings cascade",
``docs/specs/05-employees-and-roles.md`` §"Surface grants at a
glance", ``docs/specs/14-web-frontend.md`` §"Workspace settings", and
``docs/specs/01-architecture.md`` §"Contexts & boundaries".
"""

from __future__ import annotations

from app.services.workspace.settings_service import (
    OwnersOnlyError,
    WorkspaceBasics,
    WorkspaceFieldInvalid,
    update_basics,
)

__all__ = [
    "OwnersOnlyError",
    "WorkspaceBasics",
    "WorkspaceFieldInvalid",
    "update_basics",
]
