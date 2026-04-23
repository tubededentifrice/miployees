"""Cross-dialect locking primitive for the last-owner invariant.

§02 "permission_group" §"Invariants" requires the system ``owners``
group on every workspace to have **at least one active member at all
times**. Both :func:`app.domain.identity.permission_groups.remove_member`
(cd-ckr) and :func:`app.domain.identity.role_grants.revoke` (cd-79r)
enforce that invariant with a count-then-act pattern. Without a lock,
the pattern is a textbook TOCTOU: two concurrent transactions both
read ``owner_count == 2`` and both commit their DELETE, leaving the
``owners`` group empty (cd-mb5n).

This module exposes a single helper —
:func:`count_owner_members_locked` — that both guards call. It:

1. Locks the system ``owners`` ``permission_group`` row for the
   caller's workspace, using the dialect's native write-lock
   primitive:

   * **PostgreSQL**: ``SELECT ... FOR UPDATE`` on the owners-group
     row. The row-level lock survives until the caller commits or
     rolls back, so any concurrent transaction that reaches this
     helper will block on step 1 until the first one settles.
   * **SQLite**: a no-op ``UPDATE permission_group SET slug = slug
     WHERE id = :owners_group_id``. SQLite promotes the connection
     from SHARED to RESERVED on the first write, and Python's
     ``sqlite3`` driver waits up to the default 5 s ``busy_timeout``
     on contention — the second writer blocks until the first
     commits, then re-reads the (now post-delete) member count.

2. Returns the current ``permission_group_member`` count for the
   owners group. Callers raise their own domain-specific exception
   when the count would drop to zero after the pending write.

Keeping the lock-then-count pair in one helper means the two guards
share the primitive by construction (DRY — cd-duv6 will take a
second pass once the repository refactor lands). The helper **never
commits**: the caller owns the transaction boundary, and releasing
the lock mid-transaction would re-open the TOCTOU window.

**Why lock the ``permission_group`` row, not the member rows?** The
member count is a function of the owners-group identity; serialising
on the parent row gives every concurrent guard the same single rendez-
vous point regardless of which member each thread is trying to
remove. Locking individual member rows would leave the count itself
unprotected (thread A locks member X, thread B locks member Y, both
count 2 and proceed).

See:

* ``docs/specs/02-domain-model.md`` §"permission_group"
  §"Invariants".
* cd-mb5n — the TOCTOU fix task.
* cd-ckr — the v1 last-owner guard on ``remove_member``.
* cd-79r — the v1 last-owner guard on ``revoke``.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import PermissionGroup, PermissionGroupMember

__all__ = ["count_owner_members_locked"]


def count_owner_members_locked(
    session: Session,
    *,
    workspace_id: str,
) -> int:
    """Lock the ``owners@workspace_id`` group row and return its member count.

    The returned count reflects the state of
    ``permission_group_member`` at the instant the lock was acquired;
    a concurrent transaction cannot mutate the count until the caller
    commits or rolls back.

    If the owners group does not exist for the given workspace the
    helper returns ``0`` without raising — every workspace bootstraps
    one in :mod:`app.adapters.db.authz.bootstrap` so the absence
    condition is pathological, and the caller's last-owner guard
    will trip on the zero count anyway. Raising here would hide that
    corruption behind a different error shape.

    **Tenant filter.** The helper is called from inside a live
    :class:`~app.tenancy.WorkspaceContext`; both SELECTs run with
    the ORM tenant filter active, so the ``workspace_id`` predicate
    is belt-and-braces but kept explicit to match the rest of the
    identity module's style (a misconfigured filter should fail
    loud, not leak a sibling workspace's count).
    """
    dialect = session.get_bind().dialect.name

    # Step 1: locate the owners-group row. We need the id for both
    # branches — Postgres to attach ``FOR UPDATE``, SQLite to issue
    # the lock-acquiring UPDATE.
    owners_stmt = select(PermissionGroup.id).where(
        PermissionGroup.workspace_id == workspace_id,
        PermissionGroup.slug == "owners",
        PermissionGroup.system.is_(True),
    )

    if dialect == "postgresql":
        # Row-level ``FOR UPDATE`` on the owners-group row. Any
        # concurrent transaction that reaches this line blocks until
        # the current one commits or rolls back.
        owners_group_id = session.scalar(owners_stmt.with_for_update())
    else:
        # SQLite (and any non-Postgres dialect): acquire the write
        # lock via a no-op UPDATE. On SQLite this promotes the
        # connection to RESERVED, serialising writers across the whole
        # database; the driver's default ``busy_timeout`` (5 s) gives
        # the loser of an upgrade race time to wait. A conditional
        # ``WHERE`` keeps the write scoped so Postgres (were it to
        # ever fall into this branch) wouldn't burn a row.
        owners_group_id = session.scalar(owners_stmt)
        if owners_group_id is not None:
            session.execute(
                update(PermissionGroup)
                .where(PermissionGroup.id == owners_group_id)
                # No-op self-assignment: ``slug`` is the uniqueness
                # anchor for the group within a workspace, so writing
                # it back to its current value changes nothing but
                # still counts as a row-level write to the lock
                # manager.
                .values(slug=PermissionGroup.slug)
            )

    if owners_group_id is None:
        # No owners group — workspace is already in an invalid state
        # per §02 invariants. Return zero so the caller's guard
        # triggers; do not mask this with an exception because the
        # two callers have different exception vocabularies and the
        # zero-count path is the one they already exercise.
        return 0

    # Step 2: count members under the lock. On Postgres this runs
    # under the ``FOR UPDATE`` row lock; on SQLite it runs after the
    # UPDATE promoted us to RESERVED. Either way, no other transaction
    # can change this count until ours commits.
    count_stmt = (
        select(func.count())
        .select_from(PermissionGroupMember)
        .where(
            PermissionGroupMember.group_id == owners_group_id,
            PermissionGroupMember.workspace_id == workspace_id,
        )
    )
    count = session.scalar(count_stmt)
    # ``select(func.count())`` always returns a scalar (zero when no
    # rows match); ``or 0`` keeps mypy honest against the ``scalar()``
    # Optional return type. An unexpected ``None`` would be treated as
    # "no members", the safe-fails-closed default for both guards.
    return count or 0
