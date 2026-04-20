"""Permission-group CRUD + membership domain service.

Permission groups are the deployable unit of authority inside a
workspace (§02 "permission_group", §05 "Permissions"). Owners +
managers define custom groups, add/remove members, and rely on this
module to enforce the spec's protection rules on the four system
groups (``owners``, ``managers``, ``all_workers``, ``all_clients``):

* System groups are never deletable; attempting to delete one
  raises :class:`SystemGroupProtected`.
* ``update_group`` on a system group only accepts a ``name`` change.
  Mutating ``capabilities`` on a system group raises
  :class:`SystemGroupProtected`; the ``slug`` column is frozen by
  design (the service never exposes a slug-change surface).
* Membership writes (``add_member`` / ``remove_member``) are
  allowed on every group including system ones, and both are
  idempotent: a duplicate add or a missing remove is a no-op that
  still emits an audit row (§02 "Audit"). The ``owners``
  last-member guard is deferred to cd-ckr (owners-group governance).

Every write goes through :func:`app.audit.write_audit` so the domain
side of the audit trail lives here — §02 "Permission resolution"
§"Audit".

Capability payloads are validated against the v1 action catalog in
:mod:`app.domain.identity._action_catalog`. Unknown keys raise
:class:`UnknownCapability`. Values are kept free-form (``Any``)
for v1 — a tighter shape can be layered on once the resolver lands.

**Capability gating is not enforced here.** The domain layer trusts
the caller; whatever permission check the HTTP router applies before
invoking these functions (see cd-dzp / cd-rpxd) is where
``permission_groups.manage`` is resolved. The service signatures
take a :class:`~app.tenancy.WorkspaceContext` purely so audit rows
carry the right actor / correlation fields.

**Architecture note.** This module imports SQLAlchemy model classes
from :mod:`app.adapters.db.authz.models` directly. Contract 1 of the
import-linter (``app.domain → app.adapters``) forbids that in
principle, so the pyproject carries a narrow ``ignore_imports``
stopgap for this path and a follow-up Beads task is filed to replace
the direct import with a Protocol seam (``PermissionGroupRepository``
in :mod:`app.adapters.db.authz.repositories`). The interim coupling
keeps this v1 slice shippable without blocking on a broader
refactor of every domain context.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import PermissionGroup, PermissionGroupMember
from app.audit import write_audit
from app.domain.identity._action_catalog import ACTION_CATALOG
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "PermissionGroupMemberRef",
    "PermissionGroupNotFound",
    "PermissionGroupRef",
    "PermissionGroupSlugTaken",
    "SystemGroupProtected",
    "UnknownCapability",
    "add_member",
    "create_group",
    "delete_group",
    "get_group",
    "list_groups",
    "list_members",
    "remove_member",
    "update_group",
]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionGroupRef:
    """Immutable projection of a ``permission_group`` row.

    Returned by every read and write on :mod:`permission_groups`. The
    domain service never hands back SQLAlchemy ``PermissionGroup``
    instances — callers manipulate these frozen dataclasses, so a
    second call can't mutate a shared row through the ORM identity
    map.
    """

    id: str
    slug: str
    name: str
    system: bool
    capabilities: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PermissionGroupMemberRef:
    """Immutable projection of a ``permission_group_member`` row."""

    group_id: str
    user_id: str
    added_at: datetime
    added_by_user_id: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PermissionGroupNotFound(LookupError):
    """The requested group does not exist in the caller's workspace."""


class PermissionGroupSlugTaken(ValueError):
    """The (workspace, slug) unique constraint rejected the insert."""


class SystemGroupProtected(ValueError):
    """Attempted to mutate a system group in a forbidden way.

    409-equivalent: delete of any system group, or an ``update_group``
    call that tries to change ``capabilities`` / ``slug`` on one.
    """


class UnknownCapability(ValueError):
    """``capabilities`` payload carries a key absent from the catalog.

    422-equivalent. The offending key is the first unknown one
    encountered during validation; use :meth:`__str__` for display.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_ref(row: PermissionGroup) -> PermissionGroupRef:
    """Project a loaded ORM row into an immutable :class:`PermissionGroupRef`.

    Copying ``capabilities_json`` into a fresh ``dict`` severs the
    reference to SQLAlchemy's mutable JSON payload so a caller who
    mutates the returned mapping doesn't poison the identity map.
    """
    return PermissionGroupRef(
        id=row.id,
        slug=row.slug,
        name=row.name,
        system=row.system,
        capabilities=dict(row.capabilities_json),
        created_at=row.created_at,
    )


def _member_to_ref(row: PermissionGroupMember) -> PermissionGroupMemberRef:
    """Project a member row into an immutable :class:`PermissionGroupMemberRef`."""
    return PermissionGroupMemberRef(
        group_id=row.group_id,
        user_id=row.user_id,
        added_at=row.added_at,
        added_by_user_id=row.added_by_user_id,
    )


def _validate_capabilities(capabilities: dict[str, Any]) -> None:
    """Raise :class:`UnknownCapability` if any key is absent from the catalog.

    Only the **keys** are validated in v1; the values are arbitrary
    JSON-compatible payloads (bool / dict / list) that the resolver
    will interpret once it lands. The ``{"all": True}`` payload on
    the seeded ``owners`` group is not re-validated — the bootstrap
    helper (:mod:`app.adapters.db.authz.bootstrap`) writes directly
    through SQLAlchemy and never reaches this service, so the
    catalog gate never sees that legacy shape.
    """
    for key in capabilities:
        if key not in ACTION_CATALOG:
            raise UnknownCapability(key)


def _load_group(
    session: Session, ctx: WorkspaceContext, *, group_id: str
) -> PermissionGroup:
    """Load ``group_id`` scoped to the caller's workspace or raise.

    The ORM tenant filter already constrains SELECTs to the active
    :class:`~app.tenancy.WorkspaceContext`, but we also assert
    ``workspace_id`` explicitly so a misconfigured context fails
    loudly instead of silently returning a sibling workspace's row.
    """
    row = session.scalars(
        select(PermissionGroup).where(
            PermissionGroup.id == group_id,
            PermissionGroup.workspace_id == ctx.workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise PermissionGroupNotFound(group_id)
    return row


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------


def list_groups(
    session: Session, ctx: WorkspaceContext
) -> Sequence[PermissionGroupRef]:
    """Return every permission group in the caller's workspace.

    Ordered by ``created_at`` ascending so system groups seeded at
    workspace creation come first and user-defined groups appear in
    the order the owner added them.
    """
    rows = session.scalars(
        select(PermissionGroup)
        .where(PermissionGroup.workspace_id == ctx.workspace_id)
        .order_by(PermissionGroup.created_at.asc(), PermissionGroup.id.asc())
    ).all()
    return [_to_ref(row) for row in rows]


def get_group(
    session: Session, ctx: WorkspaceContext, *, group_id: str
) -> PermissionGroupRef:
    """Return the single group identified by ``group_id`` or raise.

    Raises :class:`PermissionGroupNotFound` if the group is missing
    from the caller's workspace — a sibling workspace's row counts as
    missing thanks to the explicit ``workspace_id`` filter in
    :func:`_load_group`.
    """
    return _to_ref(_load_group(session, ctx, group_id=group_id))


def create_group(
    session: Session,
    ctx: WorkspaceContext,
    *,
    slug: str,
    name: str,
    capabilities: dict[str, Any],
    clock: Clock | None = None,
) -> PermissionGroupRef:
    """Insert a new non-system group in the caller's workspace.

    The unique ``(workspace_id, slug)`` constraint is enforced by the
    DB; an :class:`~sqlalchemy.exc.IntegrityError` from the flush is
    caught and re-raised as :class:`PermissionGroupSlugTaken`.
    Unknown capability keys raise :class:`UnknownCapability` before
    the insert is attempted.

    ``system=True`` groups are only created by the workspace
    bootstrap (:mod:`app.adapters.db.authz.bootstrap`); the public
    service surface here always writes ``system=False``.
    """
    _validate_capabilities(capabilities)

    now = (clock if clock is not None else SystemClock()).now()
    group = PermissionGroup(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        slug=slug,
        name=name,
        system=False,
        # Snapshot the payload so a later mutation on the caller's
        # dict doesn't bleed into the persisted row.
        capabilities_json=dict(capabilities),
        created_at=now,
    )
    # Wrap the flush in a SAVEPOINT so an IntegrityError rolls back
    # only the failed INSERT — the caller's outer transaction (and
    # any prior successful writes inside it) stays intact. A bare
    # ``session.rollback()`` on IntegrityError would nuke the whole
    # transaction, including earlier successful ``create_group``
    # calls in the same request.
    nested = session.begin_nested()
    session.add(group)
    try:
        session.flush()
    except IntegrityError as exc:
        # The only unique constraint on ``permission_group`` in v1
        # is ``(workspace_id, slug)`` — any other IntegrityError is
        # unexpected and should propagate as-is.
        nested.rollback()
        raise PermissionGroupSlugTaken(slug) from exc
    nested.commit()

    write_audit(
        session,
        ctx,
        entity_kind="permission_group",
        entity_id=group.id,
        action="created",
        diff={
            "slug": slug,
            "name": name,
            "capabilities": dict(capabilities),
        },
        clock=clock,
    )
    return _to_ref(group)


def update_group(
    session: Session,
    ctx: WorkspaceContext,
    *,
    group_id: str,
    name: str | None = None,
    capabilities: dict[str, Any] | None = None,
    clock: Clock | None = None,
) -> PermissionGroupRef:
    """Mutate an existing group's ``name`` and/or ``capabilities``.

    System groups accept only a ``name`` change; any attempt to pass
    ``capabilities`` on a system group raises
    :class:`SystemGroupProtected`. ``slug`` is never mutable from
    this surface — the service doesn't expose it as a kwarg.

    A call with neither field set is a no-op write that still emits
    an audit row (caller has reason to emit it explicitly — think
    "user clicked Save with no changes").

    ``clock`` is optional; tests pin the audit row's ``created_at``
    via a :class:`~app.util.clock.FrozenClock`.
    """
    row = _load_group(session, ctx, group_id=group_id)

    if row.system and capabilities is not None:
        raise SystemGroupProtected(
            f"permission_group {row.slug!r} is a system group; capabilities are frozen."
        )

    if capabilities is not None:
        _validate_capabilities(capabilities)

    before: dict[str, Any] = {
        "name": row.name,
        "capabilities": dict(row.capabilities_json),
    }

    if name is not None:
        row.name = name
    if capabilities is not None:
        row.capabilities_json = dict(capabilities)

    session.flush()

    after: dict[str, Any] = {
        "name": row.name,
        "capabilities": dict(row.capabilities_json),
    }
    write_audit(
        session,
        ctx,
        entity_kind="permission_group",
        entity_id=row.id,
        action="updated",
        diff={"before": before, "after": after},
        clock=clock,
    )
    return _to_ref(row)


def delete_group(
    session: Session,
    ctx: WorkspaceContext,
    *,
    group_id: str,
    clock: Clock | None = None,
) -> None:
    """Remove a non-system group and its membership rows.

    System groups raise :class:`SystemGroupProtected` — the spec's
    §02 invariant "every workspace has exactly the four system groups
    at any time" forbids deletion.

    Membership rows cascade via the FK (``ondelete="CASCADE"`` on
    ``permission_group_member.group_id``) so the caller does not
    sweep them by hand.

    ``clock`` is optional; tests pin the audit row's ``created_at``
    via a :class:`~app.util.clock.FrozenClock`.
    """
    row = _load_group(session, ctx, group_id=group_id)
    if row.system:
        raise SystemGroupProtected(
            f"permission_group {row.slug!r} is a system group; it cannot be deleted."
        )

    slug = row.slug
    name = row.name
    session.delete(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="permission_group",
        entity_id=group_id,
        action="deleted",
        diff={"slug": slug, "name": name},
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


def list_members(
    session: Session, ctx: WorkspaceContext, *, group_id: str
) -> Sequence[PermissionGroupMemberRef]:
    """List every explicit member of ``group_id``.

    Raises :class:`PermissionGroupNotFound` if the group is missing
    from the caller's workspace so a bad ID can't silently return
    "no members" (§02 "permission_group_member" — membership queries
    carry their parent scope).

    v1 ignores the ``revoked_at`` column (it doesn't exist on the
    schema yet — cd-zkr proper will land it as a follow-up migration
    per :mod:`app.adapters.db.authz.models` module docstring).
    """
    _load_group(session, ctx, group_id=group_id)
    rows = session.scalars(
        select(PermissionGroupMember)
        .where(
            PermissionGroupMember.group_id == group_id,
            PermissionGroupMember.workspace_id == ctx.workspace_id,
        )
        .order_by(
            PermissionGroupMember.added_at.asc(),
            PermissionGroupMember.user_id.asc(),
        )
    ).all()
    return [_member_to_ref(row) for row in rows]


def add_member(
    session: Session,
    ctx: WorkspaceContext,
    *,
    group_id: str,
    user_id: str,
    clock: Clock | None = None,
) -> PermissionGroupMemberRef:
    """Add ``user_id`` to ``group_id``; idempotent on duplicate rows.

    Records the acting user via ``added_by_user_id`` from ``ctx`` so
    the membership row's audit pointer survives a later deletion of
    the actor (FK ``ondelete="SET NULL"`` on
    ``permission_group_member.added_by_user_id``).

    If the ``(group_id, user_id)`` row already exists, the call is a
    no-op write that still emits an audit row — matching the symmetry
    with :func:`remove_member` and §02 "Audit" expectations for
    idempotent admin operations. Returns the existing (or freshly
    inserted) :class:`PermissionGroupMemberRef` either way.

    ``clock`` is optional; tests pin the audit row's ``created_at``
    via a :class:`~app.util.clock.FrozenClock`.
    """
    _load_group(session, ctx, group_id=group_id)

    # Idempotency check: a duplicate ``(group_id, user_id)`` INSERT
    # would trip the composite PK and raise :class:`IntegrityError`,
    # poisoning the outer transaction. Looking up the row first keeps
    # the happy path cheap and matches :func:`remove_member`'s
    # no-throw behaviour on a missing row.
    existing = session.get(PermissionGroupMember, (group_id, user_id))
    if existing is not None:
        write_audit(
            session,
            ctx,
            entity_kind="permission_group_member",
            entity_id=f"{group_id}:{user_id}",
            action="member_added",
            diff={"group_id": group_id, "user_id": user_id},
            clock=clock,
        )
        return _member_to_ref(existing)

    now = (clock if clock is not None else SystemClock()).now()
    member = PermissionGroupMember(
        group_id=group_id,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
        added_at=now,
        added_by_user_id=ctx.actor_id,
    )
    session.add(member)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="permission_group_member",
        entity_id=f"{group_id}:{user_id}",
        action="member_added",
        diff={"group_id": group_id, "user_id": user_id},
        clock=clock,
    )
    return _member_to_ref(member)


def remove_member(
    session: Session,
    ctx: WorkspaceContext,
    *,
    group_id: str,
    user_id: str,
    clock: Clock | None = None,
) -> None:
    """Remove the ``(group_id, user_id)`` membership row.

    Raises :class:`PermissionGroupNotFound` if the group is missing.
    A missing *member* row (the user was never in the group, or was
    already removed) is a no-op write — we still emit the audit row
    because the caller deliberately acted on the membership, and
    absence + re-emit is what §02 "Audit" expects for idempotent
    admin operations.

    ``clock`` is optional; tests pin the audit row's ``created_at``
    via a :class:`~app.util.clock.FrozenClock`.
    """
    _load_group(session, ctx, group_id=group_id)

    row = session.get(PermissionGroupMember, (group_id, user_id))
    if row is not None:
        session.delete(row)
        session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="permission_group_member",
        entity_id=f"{group_id}:{user_id}",
        action="member_removed",
        diff={"group_id": group_id, "user_id": user_id},
        clock=clock,
    )
