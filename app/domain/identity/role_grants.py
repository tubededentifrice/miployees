"""``role_grant`` CRUD + owner-authority policy.

Role grants are the **surface** model: they say "user U has a
persona on workspace W (optionally narrowed to a property)". A row
does not carry per-action authority — that lives on
``permission_rule`` — but the domain enforces **who may mint which
``grant_role``** right here, because it is part of the workspace's
governance invariants (§05 "Surface grants at a glance").

See ``docs/specs/05-employees-and-roles.md`` §"Role grants" /
§"Surface grants at a glance" / §"Permissions: surface, groups, and
action catalog" and ``docs/specs/02-domain-model.md`` §"role_grants".

Summary of the rules enforced in this module:

* ``grant_role`` must be in :data:`_VALID_GRANT_ROLES`; anything else
  raises :class:`GrantRoleInvalid` before we reach the DB — the
  CHECK constraint is a safety net, not the primary gate.
* **Owner-authority (§05).** Only a member of the scope's ``owners``
  permission group may mint a ``manager`` grant. ``worker`` /
  ``client`` / ``guest`` grants may additionally be minted by a
  caller who already holds an active ``manager`` grant on the
  workspace. Every other caller is rejected with
  :class:`NotAuthorizedForRole`.
* When ``scope_property_id`` is provided, it MUST reference a
  ``property_workspace`` row pinned to the caller's workspace; a
  property from a sibling workspace raises
  :class:`CrossWorkspaceProperty` so a grant cannot silently leak
  across tenants. (The ``role_grant.scope_property_id`` column is a
  soft reference to ``property.id`` today — the promoted FK lands
  with cd-8u5; until then the junction join is the authoritative
  scoping gate.)
* **Last-owner protection.** ``revoke`` refuses to remove a
  ``manager`` grant that belongs to the **only** member of the
  workspace's ``owners`` permission group. Owners-membership is a
  distinct concept from role_grant (§02 "permission_group" —
  owners is an explicit group), so losing the last
  ``owners@<workspace>`` seat would lock every subsequent
  governance operation out of the tenant. Other revokes are
  unconstrained — the test matrix in
  ``tests/integration/identity/test_role_grants.py`` documents the
  full V1 boundary.

**Capability gates are NOT enforced here.** ``users.grant_role`` /
``users.revoke_grant`` (the §05 action-catalog entries) are the
HTTP router's job (cd-dzp + cd-rpxd). The domain service trusts its
caller on those and only enforces the workspace-governance
invariants listed above. Audit rows still record ``actor_*`` fields
so the trail survives whichever layer made the call.

Every mutation writes one :mod:`app.audit` row in the **same**
transaction as the INSERT / DELETE. The caller owns the
transaction boundary — the service never calls
``session.commit()`` (§01 "Key runtime invariants" #3).

**Architecture note.** This module imports SQLAlchemy model classes
from :mod:`app.adapters.db.authz.models` and
:mod:`app.adapters.db.places.models` directly. Contract 1 of the
import-linter (``app.domain → app.adapters``) forbids that in
principle, so the pyproject carries two narrow ``ignore_imports``
stopgaps for this path and the follow-up refactor is tracked by
cd-duv6 (extended scope: covers both ``permission_groups`` and
``role_grants``). The interim coupling keeps this v1 slice
shippable without blocking on a broader Protocol-seam refactor of
every domain context.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.places.models import PropertyWorkspace
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "CrossWorkspaceProperty",
    "GrantRoleInvalid",
    "LastOwnerGrantProtected",
    "NotAuthorizedForRole",
    "RoleGrantNotFound",
    "RoleGrantRef",
    "grant",
    "list_grants",
    "revoke",
]


# Accepted ``grant_role`` values at the domain surface. Matches the
# DB-level CHECK on ``role_grant.grant_role`` (§02 v1 enum); we also
# match the admin UI: only these four ever reach a write here. The
# ``admin`` grant_role (§05 "Admin surface") is a deployment-scope
# concept — not a workspace-scope grant — so it intentionally does
# not appear in this set.
_VALID_GRANT_ROLES: frozenset[str] = frozenset({"manager", "worker", "client", "guest"})


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleGrantRef:
    """Immutable projection of a ``role_grant`` row.

    Returned by every read and write on :mod:`role_grants`. The
    domain service never hands back SQLAlchemy ``RoleGrant``
    instances — callers manipulate these frozen dataclasses, so a
    second call can't mutate a shared row through the ORM identity
    map.
    """

    id: str
    workspace_id: str
    user_id: str
    grant_role: str
    scope_property_id: str | None
    created_at: datetime
    created_by_user_id: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RoleGrantNotFound(LookupError):
    """The requested grant does not exist in the caller's workspace."""


class GrantRoleInvalid(ValueError):
    """``grant_role`` is not one of :data:`_VALID_GRANT_ROLES`.

    422-equivalent — raised before any DB write so the CHECK
    constraint never trips on a value the service never meant to
    accept.
    """


class NotAuthorizedForRole(PermissionError):
    """The caller may not mint the requested ``grant_role``.

    403-equivalent. Raised when the owner-authority rules (§05) would
    reject the mint: only ``owners@<workspace>`` may grant
    ``manager``; ``worker`` / ``client`` / ``guest`` grants require
    the caller to be in ``owners@<workspace>`` **or** hold an active
    ``manager`` role grant.
    """


class CrossWorkspaceProperty(ValueError):
    """``scope_property_id`` names a property not linked to this workspace.

    422-equivalent — a property-scoped grant may only reference a
    property the caller's workspace already owns or shares through
    ``property_workspace``. Anything else silently widens the grant
    across tenants.
    """


class LastOwnerGrantProtected(ValueError):
    """Refuse to revoke the last ``manager`` grant of the sole owner.

    409-equivalent. Removing that row would leave the workspace with
    a sole ``owners@<workspace>`` member who no longer carries the
    manager surface — every governance UI would be out of reach
    even though the permission-group row still exists. The caller
    must transfer ``owners`` membership (or grant a replacement
    ``manager`` grant first) before revoking the seat.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_ref(row: RoleGrant) -> RoleGrantRef:
    """Project a loaded ORM row into an immutable :class:`RoleGrantRef`."""
    return RoleGrantRef(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        grant_role=row.grant_role,
        scope_property_id=row.scope_property_id,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


def _load_grant(session: Session, ctx: WorkspaceContext, *, grant_id: str) -> RoleGrant:
    """Load ``grant_id`` scoped to the caller's workspace or raise.

    The ORM tenant filter already constrains SELECTs to the active
    :class:`~app.tenancy.WorkspaceContext`, but we also assert
    ``workspace_id`` explicitly so a misconfigured context fails
    loudly instead of silently returning a sibling workspace's row.
    """
    row = session.scalars(
        select(RoleGrant).where(
            RoleGrant.id == grant_id,
            RoleGrant.workspace_id == ctx.workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise RoleGrantNotFound(grant_id)
    return row


def _is_owner_member(session: Session, ctx: WorkspaceContext, *, user_id: str) -> bool:
    """Return ``True`` iff ``user_id`` is a member of ``owners@<workspace>``.

    Joins ``permission_group_member`` to ``permission_group`` on the
    system ``owners`` slug — the membership row alone is
    insufficient because a workspace may carry a non-system group
    with the same slug (the DB uniqueness is scoped to
    ``(workspace_id, slug)`` and we also gate on ``system=True``).
    """
    stmt = (
        select(PermissionGroupMember)
        .join(
            PermissionGroup,
            PermissionGroup.id == PermissionGroupMember.group_id,
        )
        .where(
            PermissionGroupMember.workspace_id == ctx.workspace_id,
            PermissionGroupMember.user_id == user_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


def _has_active_manager_grant(
    session: Session, ctx: WorkspaceContext, *, user_id: str
) -> bool:
    """Return ``True`` iff ``user_id`` holds a ``manager`` grant here.

    v1 slice has no ``revoked_at`` column on ``role_grant`` (§02's
    full schema is deferred to a follow-up migration). Any row with
    ``grant_role = 'manager'`` in the caller's workspace counts as
    an active manager grant for the purpose of mint-authority
    checks.
    """
    stmt = (
        select(RoleGrant)
        .where(
            RoleGrant.workspace_id == ctx.workspace_id,
            RoleGrant.user_id == user_id,
            RoleGrant.grant_role == "manager",
        )
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


def _assert_authorized_to_grant(
    session: Session, ctx: WorkspaceContext, *, grant_role: str
) -> None:
    """Raise :class:`NotAuthorizedForRole` if the caller can't mint ``grant_role``.

    Owner-authority matrix (§05):

    * ``manager`` — only ``owners@<workspace>`` members.
    * ``worker`` / ``client`` / ``guest`` — ``owners@<workspace>`` OR
      an active ``manager`` role grant in the workspace.
    """
    if _is_owner_member(session, ctx, user_id=ctx.actor_id):
        return
    if grant_role == "manager":
        raise NotAuthorizedForRole(
            "only members of 'owners' may grant the manager role"
        )
    # Non-owner: still OK if they already hold the manager surface.
    if _has_active_manager_grant(session, ctx, user_id=ctx.actor_id):
        return
    raise NotAuthorizedForRole(
        f"caller is not authorized to mint a {grant_role!r} grant"
    )


def _assert_scope_property_in_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    scope_property_id: str,
) -> None:
    """Fail if ``scope_property_id`` isn't linked to the caller's workspace.

    The check runs against ``property_workspace`` — the junction
    table is the authoritative "this property belongs to this
    workspace" relation (§02 "property_workspace"). The ``property``
    table itself is tenant-agnostic and therefore cannot be filtered
    through the ORM tenant filter directly; the junction is
    workspace-scoped, so its own tenant predicate runs automatically.
    """
    stmt = select(
        exists().where(
            PropertyWorkspace.property_id == scope_property_id,
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if not session.scalar(stmt):
        raise CrossWorkspaceProperty(
            f"property {scope_property_id!r} is not linked to this workspace"
        )


def _count_owner_members(session: Session, ctx: WorkspaceContext) -> int:
    """Return the number of ``owners@<workspace>`` members.

    Used by :func:`revoke`'s last-owner guard so a manager-grant
    revoke cannot leave the workspace with a single
    ``owners@<workspace>`` member who no longer carries the manager
    surface.
    """
    stmt = (
        select(func.count())
        .select_from(PermissionGroupMember)
        .join(
            PermissionGroup,
            PermissionGroup.id == PermissionGroupMember.group_id,
        )
        .where(
            PermissionGroupMember.workspace_id == ctx.workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
    )
    count = session.scalar(stmt)
    # ``select(func.count())`` always returns a scalar (zero if no
    # rows match); the ``or 0`` keeps mypy honest against the
    # ``scalar()`` Optional return type without masking a real bug
    # — an unexpected ``None`` would surface as "treat the workspace
    # as having zero owners", which then trips the lockout guard
    # immediately on any revoke, which is the correct failure mode.
    return count or 0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_grants(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    scope_property_id: str | None = None,
) -> Sequence[RoleGrantRef]:
    """Return every role grant in the caller's workspace, optionally filtered.

    Ordered by ``created_at`` ascending (with ``id`` as a stable
    tiebreaker inside the same millisecond) so the seeded owner
    grant always leads and subsequent mints appear in the order the
    workspace emitted them.

    ``user_id`` / ``scope_property_id`` are pure equality filters —
    the callers who need "grants for this user regardless of
    property" pass ``user_id`` alone; "grants on this property
    regardless of user" pass ``scope_property_id`` alone; passing
    both narrows to the intersection.
    """
    stmt = select(RoleGrant).where(RoleGrant.workspace_id == ctx.workspace_id)
    if user_id is not None:
        stmt = stmt.where(RoleGrant.user_id == user_id)
    if scope_property_id is not None:
        stmt = stmt.where(RoleGrant.scope_property_id == scope_property_id)
    stmt = stmt.order_by(RoleGrant.created_at.asc(), RoleGrant.id.asc())
    rows = session.scalars(stmt).all()
    return [_to_ref(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def grant(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    grant_role: str,
    scope_property_id: str | None = None,
    clock: Clock | None = None,
) -> RoleGrantRef:
    """Mint a fresh ``role_grant`` row for ``user_id``.

    Enforces owner-authority (§05 "Surface grants at a glance") and
    the property-scope sanity rule (``scope_property_id`` must live
    in the caller's workspace through ``property_workspace``). Every
    successful mint emits one ``audit_log`` row with action
    ``granted``.

    Raises:

    * :class:`GrantRoleInvalid` — ``grant_role`` is not in
      :data:`_VALID_GRANT_ROLES`.
    * :class:`NotAuthorizedForRole` — caller is not a member of
      ``owners@<workspace>`` and does not hold a ``manager`` grant
      sufficient for the requested role.
    * :class:`CrossWorkspaceProperty` — ``scope_property_id`` does
      not reference a property linked to the caller's workspace.

    ``clock`` is optional; tests pin ``created_at`` via a
    :class:`~app.util.clock.FrozenClock`.
    """
    if grant_role not in _VALID_GRANT_ROLES:
        raise GrantRoleInvalid(grant_role)

    _assert_authorized_to_grant(session, ctx, grant_role=grant_role)

    if scope_property_id is not None:
        _assert_scope_property_in_workspace(
            session, ctx, scope_property_id=scope_property_id
        )

    now = (clock if clock is not None else SystemClock()).now()
    row = RoleGrant(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        grant_role=grant_role,
        scope_property_id=scope_property_id,
        created_at=now,
        created_by_user_id=ctx.actor_id,
    )
    session.add(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="role_grant",
        entity_id=row.id,
        action="granted",
        diff={
            "user_id": user_id,
            "grant_role": grant_role,
            "scope_property_id": scope_property_id,
        },
        clock=clock,
    )
    return _to_ref(row)


def revoke(
    session: Session,
    ctx: WorkspaceContext,
    *,
    grant_id: str,
    clock: Clock | None = None,
) -> None:
    """Delete the role grant identified by ``grant_id``.

    The v1 schema has no ``revoked_at`` column (§02's soft-retire
    pattern lands with a follow-up migration), so revocation is a
    hard DELETE today. Audit still records the mutation.

    Raises:

    * :class:`RoleGrantNotFound` — no row in the caller's workspace
      with that id.
    * :class:`LastOwnerGrantProtected` — the grant is a ``manager``
      grant belonging to the sole member of
      ``owners@<workspace>``. Transfer ``owners`` membership or
      grant a replacement ``manager`` before removing this seat.

    ``clock`` is optional; tests pin the audit row's ``created_at``
    via a :class:`~app.util.clock.FrozenClock`.
    """
    row = _load_grant(session, ctx, grant_id=grant_id)

    # V1 pragmatic rule (see module docstring): only ``manager`` revokes
    # interact with owners-membership. Worker / client / guest revokes
    # never affect the owners governance anchor, so they always pass.
    if row.grant_role == "manager" and _is_owner_member(
        session, ctx, user_id=row.user_id
    ):
        owner_count = _count_owner_members(session, ctx)
        # The caller's workspace always has ≥ 1 owner (§02
        # "permission_group" §"Invariants"); the count check protects
        # against the ``owner_count == 1`` lockout specifically.
        if owner_count <= 1:
            raise LastOwnerGrantProtected(
                "cannot remove last owner's manager grant; "
                "transfer owners-membership first"
            )

    # Snapshot the fields the audit row needs before the DELETE; once the
    # row is gone SQLAlchemy may expire the instance and a later
    # attribute read would issue a SELECT against a missing row. We
    # also carry ``scope_property_id`` into the audit payload so
    # operational forensics ("which property grant was removed?") can
    # reconstruct the deleted row without walking back to the earlier
    # ``granted`` entry.
    user_id = row.user_id
    grant_role = row.grant_role
    scope_property_id = row.scope_property_id
    session.delete(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="role_grant",
        entity_id=grant_id,
        action="revoked",
        diff={
            "user_id": user_id,
            "grant_role": grant_role,
            "scope_property_id": scope_property_id,
        },
        clock=clock,
    )
