"""Cross-cutting authorization helpers.

This package collects authz primitives that are consumed by multiple
domain contexts (identity, tenancy middleware, future permission
resolver). Putting them under ``app.authz`` rather than
``app.domain.identity`` keeps them reachable from the HTTP middleware
(where a ``WorkspaceContext`` is being *built*, not consumed) without
pulling the whole domain-identity module graph into the middleware
import chain.

Unlike ``app.domain`` modules, ``app.authz`` modules MAY import from
``app.adapters`` directly — the import-linter contract
(``app.domain → app.adapters``) does not apply here. These helpers
are thin DB shims; when the proper ``PermissionGroupRepository``
lands (cd-duv6) the body moves behind a Protocol seam.

See ``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog" and §"Root-only actions (governance)".
"""

from __future__ import annotations

from app.authz.owners import is_owner_member, resolve_is_owner

__all__ = ["is_owner_member", "resolve_is_owner"]
