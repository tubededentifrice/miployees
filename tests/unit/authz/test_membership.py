"""Unit tests for :mod:`app.authz.membership`.

Covers :func:`is_member_of` — the single dispatch that the permission
resolver calls for its ``default_allow`` fallback. The module has two
shapes to test:

* **Explicit groups** (``owners``) — delegates to
  :func:`app.authz.owners.is_owner_member`; covered by that module's
  own tests. Here we only confirm the dispatch hop works.
* **Derived groups** (``managers`` / ``all_workers`` / ``all_clients``)
  — backed by ``role_grant``. §02 "Derived group membership" is
  explicit that a workspace-scope derived group is populated only by
  workspace-scope grants (``scope_property_id IS NULL``);
  property-scoped grants do not silently escalate.

The property-scope filter (``scope_property_id IS NULL``) is the one
invariant the reviewer flagged — a property-scoped manager grant must
not make the user a workspace-scope ``managers`` member. That
assertion is the spine of this file; the happy paths anchor it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.authz.membership import UnknownSystemGroup, is_member_of
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with every ORM table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed_workspace_and_user(session: Session, *, tag: str) -> tuple[str, str]:
    """Seed a workspace + user; return ``(workspace_id, user_id)``.

    The FK chain (role_grant → workspace / user) forces us to
    materialise the real rows before any grant can land — matches the
    shape used by the enforce-module tests.
    """
    workspace_id = new_ulid()
    user_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug="membership-ws",
            name="Membership WS",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    email = f"{tag}@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=tag.capitalize(),
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id, user_id


class TestDerivedGroups:
    """Derived system groups are computed from ``role_grant``."""

    def test_workspace_scope_manager_grant_makes_member(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Workspace-scope ``grant_role='manager'`` → ``managers`` member."""
        with factory() as s:
            workspace_id, user_id = _seed_workspace_and_user(s, tag="manager")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            assert is_member_of(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                group_slug="managers",
            )

    def test_worker_grant_makes_all_workers_member(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            workspace_id, user_id = _seed_workspace_and_user(s, tag="worker")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            assert is_member_of(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                group_slug="all_workers",
            )

    def test_no_grant_not_member(self, factory: sessionmaker[Session]) -> None:
        """A user with no grants is not a member of any derived group."""
        with factory() as s:
            workspace_id, user_id = _seed_workspace_and_user(s, tag="stranger")
            s.commit()
            assert not is_member_of(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                group_slug="managers",
            )

    def test_property_scoped_manager_is_not_workspace_member(
        self, factory: sessionmaker[Session]
    ) -> None:
        """§02: property-level manager grants do not silently escalate.

        A row with ``scope_property_id != NULL`` narrows the grant to
        a single property — it MUST NOT make the holder a member of
        the *workspace* ``managers`` group. The explicit
        ``scope_property_id.is_(None)`` predicate in the derived-group
        query is the guard being pinned.
        """
        with factory() as s:
            workspace_id, user_id = _seed_workspace_and_user(s, tag="prop-mgr")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role="manager",
                    # Property-scoped grant — a soft reference; the
                    # ``property`` table lands with cd-i6u and this
                    # becomes a real FK. Today it's a string id.
                    scope_property_id=new_ulid(),
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            assert not is_member_of(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                group_slug="managers",
            )

    def test_workspace_grant_wins_when_property_grant_also_exists(
        self, factory: sessionmaker[Session]
    ) -> None:
        """If the user has both a workspace-scope and a property-scope
        manager grant, they are still a workspace-scope member — the
        workspace-scope row qualifies, the property row is correctly
        ignored but doesn't poison the answer.
        """
        with factory() as s:
            workspace_id, user_id = _seed_workspace_and_user(s, tag="both")
            s.add_all(
                [
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=workspace_id,
                        user_id=user_id,
                        grant_role="manager",
                        scope_property_id=None,
                        created_at=_PINNED,
                        created_by_user_id=None,
                    ),
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=workspace_id,
                        user_id=user_id,
                        grant_role="manager",
                        scope_property_id=new_ulid(),
                        created_at=_PINNED,
                        created_by_user_id=None,
                    ),
                ]
            )
            s.commit()
            assert is_member_of(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                group_slug="managers",
            )


class TestUnknownSlug:
    """Catalog drift — a default_allow referring to a non-v1 slug."""

    def test_raises(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            workspace_id, user_id = _seed_workspace_and_user(s, tag="stranger")
            s.commit()
            with pytest.raises(UnknownSystemGroup):
                is_member_of(
                    s,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    group_slug="admins",  # not a v1 system group slug
                )
