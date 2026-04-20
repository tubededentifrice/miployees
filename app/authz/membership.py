"""System-group membership lookup.

The permission resolver (:mod:`app.authz.enforce`) falls back to each
action's ``default_allow`` list when no rule matches (§02 "Permission
resolution" #5). To answer *"is user U a member of system group G on
workspace W?"* it consults this module.

Two classes of system group exist in v1 (§02 "permission_group"
§"System groups"):

* **Explicit** — ``owners``. Membership lives in
  ``permission_group_member``. Covered by
  :func:`app.authz.owners.is_owner_member`.
* **Derived** — ``managers``, ``all_workers``, ``all_clients``.
  Membership is computed from ``role_grants`` (§02 "Derived group
  membership"):

  * ``managers`` iff an active ``role_grant`` with
    ``grant_role='manager'`` exists on the workspace.
  * ``all_workers`` iff ``grant_role='worker'``.
  * ``all_clients`` iff ``grant_role='client'``.

  There is NO ``permission_group_member`` row for derived groups —
  mutating one directly would be a spec violation.

A single entry point (:func:`is_member_of`) dispatches on the slug so
the resolver has a uniform ``is_member_of(ws, u, slug)`` call site
without having to know which groups are explicit vs derived. Keeping
that dispatch here (and not inside the resolver) means §02's
"derived" rule lives in one place.

See ``docs/specs/02-domain-model.md`` §"Permission resolution"
§"Derived group membership" and
``docs/specs/05-employees-and-roles.md`` §"Action catalog".
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.authz.owners import is_owner_member

__all__ = ["UnknownSystemGroup", "is_member_of"]


# Slug → ``role_grants.grant_role`` value for the three derived
# system groups (§02 "Derived group membership"). ``owners`` is NOT
# here — it is explicit, handled by :func:`is_owner_member`.
_DERIVED_GROUP_TO_ROLE: dict[str, str] = {
    "managers": "manager",
    "all_workers": "worker",
    "all_clients": "client",
}


class UnknownSystemGroup(ValueError):
    """``is_member_of`` was asked about a slug that isn't a v1 system group.

    Catalog entries reference ``owners | managers | all_workers |
    all_clients`` only — any other slug is a catalog error. Raised as a
    :class:`ValueError` so callers mapping to HTTP can surface it as a
    5xx (an unknown slug is server misconfiguration, not user input).
    """


def is_member_of(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    group_slug: str,
) -> bool:
    """Return ``True`` iff ``user_id`` is a member of
    ``<group_slug>@<workspace_id>``.

    Dispatches on ``group_slug``:

    * ``owners`` → delegates to :func:`app.authz.owners.is_owner_member`
      (explicit membership on ``permission_group_member``).
    * ``managers`` / ``all_workers`` / ``all_clients`` → single SELECT
      on ``role_grant`` looking for any active row matching the
      mapped ``grant_role``. §02 "Derived group membership" is
      explicit that these groups never carry ``permission_group_member``
      rows, so consulting that table would be misleading.

    A membership row is "active" if it exists — v1 has no
    ``revoked_at`` column on ``role_grant`` (see
    :mod:`app.adapters.db.authz.models` docstring); the future column
    will extend this query with an ``IS NULL`` predicate without
    changing the caller contract.

    **Workspace-scope means workspace-scope.** §02's "Derived group
    membership" says that a property-level manager grant contributes
    to the *workspace* managers group only when an explicit
    property-scope rule names the property as subject — it does not
    silently promote the holder to workspace-scope membership. This
    helper answers the workspace-scope question, so it filters out
    rows with a non-NULL ``scope_property_id``. Callers asking about
    a specific property would compose a property-scope-aware query
    on top; today the resolver consults this helper only for the
    workspace-scope ``default_allow`` fallback, which is exactly the
    shape the spec wants.
    """
    if group_slug == "owners":
        return is_owner_member(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
        )

    mapped_role = _DERIVED_GROUP_TO_ROLE.get(group_slug)
    if mapped_role is None:
        raise UnknownSystemGroup(group_slug)

    stmt = (
        select(RoleGrant.id)
        .where(
            RoleGrant.workspace_id == workspace_id,
            RoleGrant.user_id == user_id,
            RoleGrant.grant_role == mapped_role,
            RoleGrant.scope_property_id.is_(None),
        )
        .limit(1)
    )
    return session.scalars(stmt).first() is not None
