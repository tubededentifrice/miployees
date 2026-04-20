"""Cross-cutting authorization helpers.

This package collects authz primitives that are consumed by multiple
domain contexts (identity, tenancy middleware, the permission
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

Public surface:

* :func:`is_owner_member` / :func:`resolve_is_owner` — explicit
  owners-group membership lookup (cd-ckr).
* :func:`is_member_of` — dispatch on system-group slug, derived-vs-
  explicit (cd-dzp).
* :class:`Permission` / :func:`require` — the canonical permission
  check (cd-dzp). Routers wire the former, service callers the
  latter.
* :class:`PermissionRuleRepository` / :class:`EmptyPermissionRuleRepository`
  — v1 seam so the resolver is complete before the
  ``permission_rule`` table ships (cd-dzp).

See ``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog" and
``docs/specs/02-domain-model.md`` §"Permission resolution".
"""

from __future__ import annotations

from app.authz.enforce import (
    CatalogDrift,
    EmptyPermissionRuleRepository,
    InvalidScope,
    Permission,
    PermissionCheck,
    PermissionDenied,
    PermissionRuleRepository,
    RuleEffect,
    RuleRow,
    UnknownActionKey,
    require,
    validate_catalog_integrity,
)
from app.authz.membership import UnknownSystemGroup, is_member_of
from app.authz.owners import is_owner_member, resolve_is_owner

__all__ = [
    "CatalogDrift",
    "EmptyPermissionRuleRepository",
    "InvalidScope",
    "Permission",
    "PermissionCheck",
    "PermissionDenied",
    "PermissionRuleRepository",
    "RuleEffect",
    "RuleRow",
    "UnknownActionKey",
    "UnknownSystemGroup",
    "is_member_of",
    "is_owner_member",
    "require",
    "resolve_is_owner",
    "validate_catalog_integrity",
]
