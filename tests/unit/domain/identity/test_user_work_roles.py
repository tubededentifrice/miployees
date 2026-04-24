"""Unit tests for :mod:`app.domain.identity.user_work_roles` (cd-dcfw)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.workspace.models import UserWorkRole
from app.domain.identity.user_work_roles import (
    UserWorkRoleCreate,
    UserWorkRoleInvariantViolated,
    UserWorkRoleNotFound,
    UserWorkRoleUpdate,
    create_user_work_role,
    delete_user_work_role,
    get_user_work_role,
    list_user_work_roles,
    update_user_work_role,
)
from app.util.clock import FrozenClock
from tests.unit.domain.identity.conftest import (
    attach_user,
    ctx_for,
    make_user,
    make_work_role,
    make_workspace,
)

_STARTED = date(2026, 4, 1)


def _audit_actions(session: Session, *, entity_id: str) -> list[str]:
    rows = list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )
    return [r.action for r in rows]


class TestCreate:
    def test_happy_path(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        role = make_work_role(session, ws=ws, key="maid")

        view = create_user_work_role(
            session,
            ctx,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role.id, started_on=_STARTED
            ),
            clock=clock,
        )
        assert view.user_id == user.id
        assert view.work_role_id == role.id
        assert view.workspace_id == ws.id
        assert view.ended_on is None
        assert _audit_actions(session, entity_id=view.id) == ["user_work_role.created"]

    def test_rejects_non_member(self, session: Session, clock: FrozenClock) -> None:
        """§05 invariant: user must be a member of the caller's workspace."""
        ws = make_workspace(session, slug="ws")
        other_ws = make_workspace(session, slug="other")
        stranger = make_user(session, email="s@e.com", display_name="Stranger")
        # Stranger is in ``other_ws`` but NOT in ``ws``.
        attach_user(session, user_id=stranger.id, workspace_id=other_ws.id)
        owner = make_user(session, email="o@e.com", display_name="Owner")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)
        role = make_work_role(session, ws=ws, key="maid")

        with pytest.raises(UserWorkRoleInvariantViolated):
            create_user_work_role(
                session,
                ctx,
                body=UserWorkRoleCreate(
                    user_id=stranger.id, work_role_id=role.id, started_on=_STARTED
                ),
                clock=clock,
            )

    def test_rejects_cross_workspace_work_role(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """§05 invariant: work_role must belong to the caller's workspace."""
        ws_a = make_workspace(session, slug="ws-a")
        ws_b = make_workspace(session, slug="ws-b")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws_a.id)
        role_b = make_work_role(session, ws=ws_b, key="maid")
        ctx_a = ctx_for(workspace=ws_a, actor_id=user.id)

        with pytest.raises(UserWorkRoleInvariantViolated):
            create_user_work_role(
                session,
                ctx_a,
                body=UserWorkRoleCreate(
                    user_id=user.id, work_role_id=role_b.id, started_on=_STARTED
                ),
                clock=clock,
            )

    def test_duplicate_identity_tuple_raises(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """DB unique on (user, ws, role, started_on) collapses into typed error."""
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        role = make_work_role(session, ws=ws, key="maid")

        create_user_work_role(
            session,
            ctx,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role.id, started_on=_STARTED
            ),
            clock=clock,
        )
        with pytest.raises(UserWorkRoleInvariantViolated):
            create_user_work_role(
                session,
                ctx,
                body=UserWorkRoleCreate(
                    user_id=user.id, work_role_id=role.id, started_on=_STARTED
                ),
                clock=clock,
            )

    def test_dto_rejects_ended_before_started(self) -> None:
        with pytest.raises(ValueError):
            UserWorkRoleCreate(
                user_id="u",
                work_role_id="r",
                started_on=date(2026, 4, 10),
                ended_on=date(2026, 4, 5),
            )


class TestUpdate:
    def test_patch_ended_on_only_records_diff(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        role = make_work_role(session, ws=ws, key="maid")
        created = create_user_work_role(
            session,
            ctx,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role.id, started_on=_STARTED
            ),
            clock=clock,
        )

        view = update_user_work_role(
            session,
            ctx,
            user_work_role_id=created.id,
            body=UserWorkRoleUpdate(ended_on=date(2026, 5, 1)),
            clock=clock,
        )
        assert view.ended_on == date(2026, 5, 1)
        # created + updated
        assert _audit_actions(session, entity_id=view.id) == [
            "user_work_role.created",
            "user_work_role.updated",
        ]

    def test_ended_before_started_raises(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        role = make_work_role(session, ws=ws, key="maid")
        created = create_user_work_role(
            session,
            ctx,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role.id, started_on=date(2026, 4, 10)
            ),
            clock=clock,
        )

        with pytest.raises(UserWorkRoleInvariantViolated):
            update_user_work_role(
                session,
                ctx,
                user_work_role_id=created.id,
                body=UserWorkRoleUpdate(ended_on=date(2026, 4, 5)),
                clock=clock,
            )

    def test_empty_patch_is_noop(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        role = make_work_role(session, ws=ws, key="maid")
        created = create_user_work_role(
            session,
            ctx,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role.id, started_on=_STARTED
            ),
            clock=clock,
        )

        update_user_work_role(
            session,
            ctx,
            user_work_role_id=created.id,
            body=UserWorkRoleUpdate(),
            clock=clock,
        )
        # Only the `created` audit row, no update row.
        assert _audit_actions(session, entity_id=created.id) == [
            "user_work_role.created"
        ]

    def test_unknown_id_raises(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)

        with pytest.raises(UserWorkRoleNotFound):
            update_user_work_role(
                session,
                ctx,
                user_work_role_id="unknown",
                body=UserWorkRoleUpdate(ended_on=date.today()),
                clock=clock,
            )


class TestDelete:
    def test_soft_delete_stamps_deleted_at(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        role = make_work_role(session, ws=ws, key="maid")
        created = create_user_work_role(
            session,
            ctx,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role.id, started_on=_STARTED
            ),
            clock=clock,
        )

        view = delete_user_work_role(
            session, ctx, user_work_role_id=created.id, clock=clock
        )
        assert view.deleted_at is not None

        # Subsequent read (tenant-filtered) raises 404.
        with pytest.raises(UserWorkRoleNotFound):
            get_user_work_role(session, ctx, user_work_role_id=created.id)

        # Row is still there but soft-flagged.
        row = session.get(UserWorkRole, created.id)
        assert row is not None
        assert row.deleted_at is not None

        assert "user_work_role.deleted" in _audit_actions(session, entity_id=created.id)

    def test_delete_unknown_id_raises_404(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)

        with pytest.raises(UserWorkRoleNotFound):
            delete_user_work_role(session, ctx, user_work_role_id="nope", clock=clock)


class TestList:
    def test_cross_workspace_isolation(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws_a = make_workspace(session, slug="ws-a")
        ws_b = make_workspace(session, slug="ws-b")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws_a.id)
        attach_user(session, user_id=user.id, workspace_id=ws_b.id)
        role_a = make_work_role(session, ws=ws_a, key="maid")
        role_b = make_work_role(session, ws=ws_b, key="cook")
        ctx_a = ctx_for(workspace=ws_a, actor_id=user.id)
        ctx_b = ctx_for(workspace=ws_b, actor_id=user.id)

        create_user_work_role(
            session,
            ctx_a,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role_a.id, started_on=_STARTED
            ),
            clock=clock,
        )
        create_user_work_role(
            session,
            ctx_b,
            body=UserWorkRoleCreate(
                user_id=user.id, work_role_id=role_b.id, started_on=_STARTED
            ),
            clock=clock,
        )

        page_a = list(list_user_work_roles(session, ctx_a, user_id=user.id, limit=50))
        assert len(page_a) == 1
        assert page_a[0].work_role_id == role_a.id
