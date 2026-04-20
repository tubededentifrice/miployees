"""Owners-membership lookup + (future) resolver cache seam.

The ``owners`` permission group is the governance anchor on every
workspace (§02 "permission_group" §"Invariants" — at least one active
member at all times). Several callers need to ask the simple question
"is user ``U`` an ``owners@<workspace>`` member?":

* The tenancy middleware populating :attr:`WorkspaceContext.actor_was_owner_member`
  (cd-7y4).
* The domain service :mod:`app.domain.identity.role_grants` enforcing
  owner-authority on ``grant`` / ``revoke`` (§05 "Surface grants at a
  glance").
* The domain service :mod:`app.domain.identity.permission_groups`
  enforcing the last-owner-member guard on ``remove_member`` (cd-ckr).

Keeping the check in one place means:

* **DRY.** A single SELECT shape, a single set of join gates
  (``system=True`` + ``slug='owners'`` + ``workspace_id``). No two
  copies disagreeing on what "owner" means.
* **Future cache seam.** When the permission resolver (cd-79r / cd-zkr)
  lands, it will cache this decision per request. Caching at the
  helper is premature — caller knows whether it has already asked —
  so we keep the helper stateless and let the caller memoize.

``resolve_is_owner`` is the task-spec name for the public surface; it
aliases :func:`is_owner_member` so middleware code can use either
depending on the calling idiom.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import PermissionGroup, PermissionGroupMember

__all__ = ["is_owner_member", "resolve_is_owner"]


def is_owner_member(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> bool:
    """Return ``True`` iff ``user_id`` is a member of ``owners@<workspace_id>``.

    Joins ``permission_group_member`` to ``permission_group`` so the
    match requires the membership row AND the target group to be the
    system ``owners`` group. The DB uniqueness is scoped to
    ``(workspace_id, slug)`` and a workspace may in theory carry a
    non-system group named ``owners`` (the service surface forbids
    it, but the schema doesn't), so we also gate on ``system=True``
    as defence-in-depth.

    **Single SELECT.** The caller is expected to be a per-request
    path (middleware + domain gates); caching is the caller's job.

    Does not honour ``ctx`` because the middleware calls this before
    any :class:`~app.tenancy.WorkspaceContext` exists. Instead it
    takes the ``workspace_id`` directly and short-circuits the ORM
    tenant filter: the SELECT carries explicit ``workspace_id``
    predicates that satisfy the filter's scope requirement. Callers
    inside a live context pass their ``ctx.workspace_id``.
    """
    stmt = (
        select(PermissionGroupMember.user_id)
        .join(
            PermissionGroup,
            PermissionGroup.id == PermissionGroupMember.group_id,
        )
        .where(
            PermissionGroupMember.workspace_id == workspace_id,
            PermissionGroupMember.user_id == user_id,
            PermissionGroup.workspace_id == workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


# Alias matching the cd-ckr task spec. Middleware at cd-7y4 will call
# ``resolve_is_owner(session, workspace_id=..., user_id=...)`` — both
# names resolve to the same helper so neither call site has to learn
# the other's idiom.
resolve_is_owner = is_owner_member
