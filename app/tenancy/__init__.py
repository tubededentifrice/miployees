"""Shared tenancy primitives.

Exports the :class:`WorkspaceContext` dataclass, slug validation,
current-context helpers, and the scoped-table registry.

See ``docs/specs/01-architecture.md`` §"Multi-tenancy runtime".
"""

from app.tenancy.context import ActorGrantRole, ActorKind, WorkspaceContext
from app.tenancy.current import (
    get_current,
    is_tenant_agnostic,
    reset_current,
    set_current,
    tenant_agnostic,
)
from app.tenancy.deployment import (
    DEPLOYMENT_SCOPE_CATALOG,
    DEPLOYMENT_SCOPE_PREFIX,
    DeploymentActorKind,
    DeploymentContext,
)
from app.tenancy.registry import is_scoped, register, scoped_tables
from app.tenancy.slug import (
    RESERVED_SLUGS,
    SLUG_PATTERN,
    InvalidSlug,
    is_homoglyph_collision,
    is_reserved,
    normalise_for_collision,
    normalise_slug,
    validate_slug,
)

__all__ = [
    "DEPLOYMENT_SCOPE_CATALOG",
    "DEPLOYMENT_SCOPE_PREFIX",
    "RESERVED_SLUGS",
    "SLUG_PATTERN",
    "ActorGrantRole",
    "ActorKind",
    "DeploymentActorKind",
    "DeploymentContext",
    "InvalidSlug",
    "WorkspaceContext",
    "get_current",
    "is_homoglyph_collision",
    "is_reserved",
    "is_scoped",
    "is_tenant_agnostic",
    "normalise_for_collision",
    "normalise_slug",
    "register",
    "reset_current",
    "scoped_tables",
    "set_current",
    "tenant_agnostic",
    "validate_slug",
]
