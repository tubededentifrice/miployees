"""DeploymentContext — canonical per-request deployment-admin record.

Parallel of :class:`~app.tenancy.context.WorkspaceContext` for the
deployment-scoped admin surface (``/admin/api/v1/...``, §12 "Admin
surface"). The admin auth dep
(:func:`app.api.admin.deps.current_deployment_admin_principal`)
resolves one of these per request from either:

* a passkey session whose user holds an active
  ``role_grant`` row with ``scope_kind='deployment'`` (the SPA's
  ``/admin`` chrome runs through this principal exclusively); or
* a deployment-scoped API token — a ``scoped`` row carrying one or
  more ``deployment:*`` keys (§12 "Admin surface" lists the family),
  or a ``delegated`` row owned by a deployment admin (the agent
  inherits the human's deployment grants).

Mixing ``deployment:*`` keys with workspace scopes on the same token
is a 422 ``deployment_scope_conflict`` per §12 — the dep enforces
that at the seam so every downstream admin route shares one
rejection envelope.

The context is **read-only** to downstream services, mirroring the
:class:`WorkspaceContext` invariant. Downstream code keys
authorisation on :attr:`deployment_scopes` (a frozenset of
``deployment:*`` keys); session-principal admins carry the full
deployment scope catalogue because the spec collapses every
fine-grained admin capability onto the single
``scope_kind='deployment'`` grant in v1.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/03-auth-and-tokens.md`` §"API tokens".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "DEPLOYMENT_SCOPE_CATALOG",
    "DEPLOYMENT_SCOPE_PREFIX",
    "DeploymentActorKind",
    "DeploymentContext",
]


# Scope-key prefix every deployment-scoped scope MUST carry. §12
# "Admin surface" pins the ``deployment:*`` family. The dot
# separator between ``deployment`` and the resource narrows the
# family so it cannot be confused with a workspace scope (e.g.
# ``llm:read``); mixing the two on the same token is a 422
# ``deployment_scope_conflict``.
DEPLOYMENT_SCOPE_PREFIX: Final[str] = "deployment."


# Canonical catalogue of deployment scope keys (§12 "Admin surface").
# Session-principal admins carry every entry — the spec collapses
# every fine-grained admin capability onto the
# ``scope_kind='deployment'`` grant in v1. Token-principal admins
# carry whichever subset the token row pins. Kept as a frozenset so
# membership tests are O(1) and the value is hashable / immutable.
DEPLOYMENT_SCOPE_CATALOG: Final[frozenset[str]] = frozenset(
    {
        "deployment.llm:read",
        "deployment.llm:write",
        "deployment.usage:read",
        "deployment.workspaces:read",
        "deployment.workspaces:write",
        "deployment.workspaces:archive",
        "deployment.signup:read",
        "deployment.signup:write",
        "deployment.settings:write",
        "deployment.audit:read",
    }
)


# The admin surface accepts three actor flavours, mirroring the
# token-kind discriminator:
#
# * ``user`` — passkey session whose user has an active deployment
#   ``role_grant``. Holds the full :data:`DEPLOYMENT_SCOPE_CATALOG`.
# * ``delegated`` — ``delegated`` API token whose delegating user is
#   a deployment admin; inherits the human's deployment grants.
# * ``agent`` — ``scoped`` API token bearing one or more
#   ``deployment:*`` scopes; authority is bounded by
#   :attr:`DeploymentContext.deployment_scopes`.
DeploymentActorKind = Literal["user", "delegated", "agent"]


@dataclass(frozen=True, slots=True)
class DeploymentContext:
    """Canonical per-request deployment-admin record.

    Immutable: equality compares every field. The admin auth dep
    builds one instance per request; downstream services consume it
    read-only.

    :attr:`principal` is the row id of the auth material the dep
    consumed — the session row's PK for session callers, the
    :class:`~app.adapters.db.identity.models.ApiToken` row's
    ``id`` (key_id) for token callers. Audit writers key on this
    so a deployment-side action is traceable back to the exact
    credential the operator used.

    :attr:`user_id` is always the human (or delegating human) who
    owns the credential. Token-principal callers carry their token's
    ``user_id`` (the minter); delegated tokens carry the delegating
    user's id (the human the agent acts for).

    :attr:`actor_kind` discriminates the principal family — see
    :data:`DeploymentActorKind`.

    :attr:`deployment_scopes` is the set of ``deployment:*`` scope
    keys this principal currently holds. Session-principal admins
    carry the full :data:`DEPLOYMENT_SCOPE_CATALOG`; token-principal
    admins carry the subset their token row pins.
    """

    principal: str
    user_id: str
    actor_kind: DeploymentActorKind
    deployment_scopes: frozenset[str]
