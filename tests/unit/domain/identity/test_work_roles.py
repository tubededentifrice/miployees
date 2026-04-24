"""Unit tests for :mod:`app.domain.identity.work_roles`.

Covers the CRUD surface landed in cd-dcfw:

* Happy-path create / list / get / update.
* Unique-per-workspace ``key`` conflict.
* Cross-workspace isolation (a role in ws-A is invisible from ws-B).
* Partial-update semantics — only sent fields land; zero-delta PATCH
  skips the audit row.
* Pagination overflow + ``after_id`` cursor walk.

See ``docs/specs/05-employees-and-roles.md`` §"Work role" and
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.domain.identity.work_roles import (
    WorkRoleCreate,
    WorkRoleKeyConflict,
    WorkRoleNotFound,
    WorkRoleUpdate,
    create_work_role,
    get_work_role,
    list_work_roles,
    update_work_role,
)
from app.util.clock import FrozenClock
from tests.unit.domain.identity.conftest import (
    attach_user,
    ctx_for,
    make_user,
    make_work_role,
    make_workspace,
)


def _audit_rows(session: Session, *, entity_id: str) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )


class TestCreate:
    def test_happy_path(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="owner@example.com", display_name="Owner")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)

        view = create_work_role(
            session,
            ctx,
            body=WorkRoleCreate(key="maid", name="Maid", icon_name="BrushCleaning"),
            clock=clock,
        )
        assert view.key == "maid"
        assert view.name == "Maid"
        assert view.icon_name == "BrushCleaning"
        assert view.workspace_id == ws.id
        assert view.deleted_at is None

        audit = _audit_rows(session, entity_id=view.id)
        assert [r.action for r in audit] == ["work_role.created"]

    def test_rejects_duplicate_key_in_same_workspace(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)

        create_work_role(
            session, ctx, body=WorkRoleCreate(key="maid", name="Maid"), clock=clock
        )
        with pytest.raises(WorkRoleKeyConflict):
            create_work_role(
                session,
                ctx,
                body=WorkRoleCreate(key="maid", name="Duplicate"),
                clock=clock,
            )

    def test_same_key_different_workspace_is_allowed(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """Per §05 — ``key`` is unique per-workspace, not globally."""
        ws_a = make_workspace(session, slug="ws-a")
        ws_b = make_workspace(session, slug="ws-b")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws_a.id)
        attach_user(session, user_id=owner.id, workspace_id=ws_b.id)

        view_a = create_work_role(
            session,
            ctx_for(workspace=ws_a, actor_id=owner.id),
            body=WorkRoleCreate(key="maid", name="Maid"),
            clock=clock,
        )
        view_b = create_work_role(
            session,
            ctx_for(workspace=ws_b, actor_id=owner.id),
            body=WorkRoleCreate(key="maid", name="Maid"),
            clock=clock,
        )
        assert view_a.id != view_b.id
        assert view_a.workspace_id != view_b.workspace_id


class TestList:
    def test_empty_workspace_returns_no_rows(self, session: Session) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)

        assert list(list_work_roles(session, ctx, limit=50)) == []

    def test_cross_workspace_isolation(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """A role in ws-B does not appear when listing from ws-A."""
        ws_a = make_workspace(session, slug="ws-a")
        ws_b = make_workspace(session, slug="ws-b")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws_a.id)
        attach_user(session, user_id=owner.id, workspace_id=ws_b.id)
        # Seed a maid in each workspace.
        create_work_role(
            session,
            ctx_for(workspace=ws_a, actor_id=owner.id),
            body=WorkRoleCreate(key="maid", name="Maid A"),
            clock=clock,
        )
        make_work_role(session, ws=ws_b, key="maid")

        result = list(
            list_work_roles(
                session, ctx_for(workspace=ws_a, actor_id=owner.id), limit=50
            )
        )
        assert len(result) == 1
        assert result[0].name == "Maid A"

    def test_pagination_overflow_returns_limit_plus_one(self, session: Session) -> None:
        """Service returns ``limit + 1`` so the router can detect has_more."""
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)
        for i in range(5):
            make_work_role(session, ws=ws, key=f"role-{i}")

        page = list(list_work_roles(session, ctx, limit=3))
        assert len(page) == 4  # limit + 1

    def test_after_id_cursor_walks_forward(self, session: Session) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)

        roles = sorted(
            [make_work_role(session, ws=ws, key=f"role-{i}") for i in range(5)],
            key=lambda r: r.id,
        )
        # Cursor on the second row → page should skip the first two.
        after = roles[1].id
        page = list(list_work_roles(session, ctx, limit=10, after_id=after))
        assert [v.id for v in page] == [r.id for r in roles[2:]]


class TestGet:
    def test_unknown_id_raises(self, session: Session) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)

        with pytest.raises(WorkRoleNotFound):
            get_work_role(session, ctx, work_role_id="does-not-exist")

    def test_cross_workspace_id_collapses_to_404(self, session: Session) -> None:
        ws_a = make_workspace(session, slug="ws-a")
        ws_b = make_workspace(session, slug="ws-b")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws_a.id)
        attach_user(session, user_id=owner.id, workspace_id=ws_b.id)
        role = make_work_role(session, ws=ws_b, key="maid")

        # Looking up ws-B's role from ws-A must 404.
        with pytest.raises(WorkRoleNotFound):
            get_work_role(
                session,
                ctx_for(workspace=ws_a, actor_id=owner.id),
                work_role_id=role.id,
            )


class TestUpdate:
    def test_partial_patch_only_touches_sent_fields(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)
        role = make_work_role(session, ws=ws, key="maid")

        view = update_work_role(
            session,
            ctx,
            work_role_id=role.id,
            body=WorkRoleUpdate(name="Housekeeper"),
            clock=clock,
        )
        assert view.name == "Housekeeper"
        # ``key`` stays the same — not sent.
        assert view.key == "maid"

        audit = _audit_rows(session, entity_id=role.id)
        assert [r.action for r in audit] == ["work_role.updated"]
        assert audit[0].diff["after"] == {"name": "Housekeeper"}

    def test_rename_conflicting_key_raises(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)
        make_work_role(session, ws=ws, key="maid")
        driver = make_work_role(session, ws=ws, key="driver")

        with pytest.raises(WorkRoleKeyConflict):
            update_work_role(
                session,
                ctx,
                work_role_id=driver.id,
                body=WorkRoleUpdate(key="maid"),
                clock=clock,
            )

    def test_noop_patch_skips_audit(self, session: Session, clock: FrozenClock) -> None:
        """Re-submitting the current values should NOT emit an audit row."""
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)
        role = make_work_role(session, ws=ws, key="maid")

        update_work_role(
            session,
            ctx,
            work_role_id=role.id,
            body=WorkRoleUpdate(name="Maid"),  # same as seed
            clock=clock,
        )
        assert _audit_rows(session, entity_id=role.id) == []

    def test_empty_patch_is_noop(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)
        role = make_work_role(session, ws=ws, key="maid")

        view = update_work_role(
            session, ctx, work_role_id=role.id, body=WorkRoleUpdate(), clock=clock
        )
        assert view.key == "maid"
        assert _audit_rows(session, entity_id=role.id) == []

    def test_unknown_id_raises(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        owner = make_user(session, email="o@e.com", display_name="O")
        attach_user(session, user_id=owner.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=owner.id)

        with pytest.raises(WorkRoleNotFound):
            update_work_role(
                session,
                ctx,
                work_role_id="nope",
                body=WorkRoleUpdate(name="X"),
                clock=clock,
            )


class TestDtoValidation:
    """Pydantic-level guards at the DTO boundary."""

    def test_blank_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkRoleCreate(key="  ", name="Maid")

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkRoleCreate(key="maid", name="")

    def test_update_rejects_null_on_not_null(self) -> None:
        with pytest.raises(ValueError):
            WorkRoleUpdate.model_validate({"key": None})
