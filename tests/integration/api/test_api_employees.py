"""Integration tests for :mod:`app.api.v1.users` employee routes.

Covers the HTTP surface added by cd-dv2:

* ``PATCH /users/{user_id}`` — partial profile update; self-edit
  passes without a capability check; cross-user edits gated on
  ``users.edit_profile_other``.
* ``POST /users/{user_id}/archive`` — archives engagement + work
  roles; idempotent; gated on ``users.archive``.
* ``POST /users/{user_id}/reinstate`` — reverse archive; idempotent.
* ``GET /users/{user_id}`` — read projection.

Mirrors the harness in ``tests/integration/auth/test_tokens_pg.py``:
mount ``build_users_router()`` in a throwaway :class:`FastAPI`,
override :func:`current_workspace_context` + the DB session dep,
drive the real domain service over HTTP so the full router → service
→ DB chain is exercised.

Note: the ``POST /users/invite`` route is covered by
``tests/integration/identity/test_membership.py`` (which drives the
domain service directly) and the accept-time seed is in
``tests/integration/identity/test_invite_accept.py``. This file
focuses on the four new workspace-scoped profile routes.

See ``docs/specs/12-rest-api.md`` §"Users" and
``docs/specs/05-employees-and-roles.md`` §"Archive / reinstate".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
    Workspace,
)
from app.adapters.mail.ports import Mailer
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Per-test session factory that commits on clean exit."""
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seeded(
    session_factory: sessionmaker[Session],
) -> Iterator[tuple[WorkspaceContext, str]]:
    """Seed an owner + target user + workspace; yield ``(ctx, target_id)``.

    The permission gates on ``users.archive`` and
    ``users.edit_profile_other`` default-allow ``(owners, managers)``.
    Seeding via :func:`bootstrap_workspace` lands the owners group +
    membership so the owner passes.
    """
    tag = new_ulid()[-8:].lower()
    slug = f"emp-{tag}"
    with session_factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        target = bootstrap_user(
            s, email=f"target-{tag}@example.com", display_name="Target"
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Employees",
            owner_user_id=owner.id,
        )
        # Make target a member of the workspace.
        with tenant_agnostic():
            s.add(
                UserWorkspace(
                    user_id=target.id,
                    workspace_id=ws.id,
                    source="workspace_grant",
                    added_at=_PINNED,
                )
            )
            s.flush()
            # Seed a pending engagement + active work role so archive
            # has something to archive.
            s.add(
                WorkEngagement(
                    id=new_ulid(),
                    user_id=target.id,
                    workspace_id=ws.id,
                    engagement_kind="payroll",
                    supplier_org_id=None,
                    pay_destination_id=None,
                    reimbursement_destination_id=None,
                    started_on=_PINNED.date(),
                    archived_on=None,
                    notes_md="",
                    created_at=_PINNED,
                    updated_at=_PINNED,
                )
            )
            work_role = WorkRole(
                id=new_ulid(),
                workspace_id=ws.id,
                key=f"maid-{tag}",
                name="Maid",
                description_md="",
                default_settings_json={},
                icon_name="",
                created_at=_PINNED,
                deleted_at=None,
            )
            s.add(work_role)
            s.flush()
            s.add(
                UserWorkRole(
                    id=new_ulid(),
                    user_id=target.id,
                    workspace_id=ws.id,
                    work_role_id=work_role.id,
                    started_on=_PINNED.date(),
                    ended_on=None,
                    pay_rule_id=None,
                    created_at=_PINNED,
                    deleted_at=None,
                )
            )
            s.flush()
        s.commit()
        owner_id, ws_id, ws_slug, target_id = (
            owner.id,
            ws.id,
            ws.slug,
            target.id,
        )

    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    yield ctx, target_id

    # Scoped cleanup.
    with session_factory() as s:
        with tenant_agnostic():
            for model in (
                AuditLog,
                UserWorkRole,
                WorkEngagement,
                WorkRole,
                RoleGrant,
                PermissionGroupMember,
                PermissionGroup,
                UserWorkspace,
            ):
                for row in s.scalars(
                    select(model).where(model.workspace_id == ws_id)
                ).all():
                    s.delete(row)
            ws_row = s.get(Workspace, ws_id)
            if ws_row is not None:
                s.delete(ws_row)
            for uid in (owner_id, target_id):
                user_row = s.get(User, uid)
                if user_row is not None:
                    s.delete(user_row)
        s.commit()


@pytest.fixture
def mailer() -> Mailer:
    return InMemoryMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    seeded: tuple[WorkspaceContext, str],
    mailer: Mailer,
    throttle: Throttle,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the users router."""
    ctx, _ = seeded
    app = FastAPI()
    app.include_router(
        build_users_router(mailer=mailer, throttle=throttle),
        prefix="/api/v1",
    )

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPatchUser:
    """Partial profile update via HTTP."""

    def test_owner_updates_target_display_name(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, target_id = seeded
        r = client.patch(
            f"/api/v1/users/{target_id}",
            json={"display_name": "Target Renamed"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == target_id
        assert body["display_name"] == "Target Renamed"

    def test_empty_body_returns_current_view(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, target_id = seeded
        r = client.patch(f"/api/v1/users/{target_id}", json={})
        assert r.status_code == 200, r.text

    def test_missing_user_is_404(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/users/01HZNONEXISTENTUSERID00000",
            json={"display_name": "X"},
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "employee_not_found"

    def test_unknown_field_is_422(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, target_id = seeded
        r = client.patch(
            f"/api/v1/users/{target_id}",
            json={"full_legal_name": "Target Legal"},  # not in DTO
        )
        assert r.status_code == 422, r.text


class TestArchiveReinstate:
    """Archive + reinstate via HTTP."""

    def test_archive_then_reinstate_round_trips(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        ctx, target_id = seeded

        r = client.post(f"/api/v1/users/{target_id}/archive")
        assert r.status_code == 200, r.text

        with session_factory() as s:
            with tenant_agnostic():
                rows = list(
                    s.scalars(
                        select(WorkEngagement).where(
                            WorkEngagement.user_id == target_id,
                            WorkEngagement.workspace_id == ctx.workspace_id,
                        )
                    ).all()
                )
            assert len(rows) == 1
            assert rows[0].archived_on is not None

        r = client.post(f"/api/v1/users/{target_id}/reinstate")
        assert r.status_code == 200, r.text

        with session_factory() as s:
            with tenant_agnostic():
                rows = list(
                    s.scalars(
                        select(WorkEngagement).where(
                            WorkEngagement.user_id == target_id,
                            WorkEngagement.workspace_id == ctx.workspace_id,
                        )
                    ).all()
                )
            assert len(rows) == 1
            assert rows[0].archived_on is None

    def test_archive_is_idempotent(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, target_id = seeded
        first = client.post(f"/api/v1/users/{target_id}/archive")
        assert first.status_code == 200, first.text
        second = client.post(f"/api/v1/users/{target_id}/archive")
        assert second.status_code == 200, second.text

    def test_archive_missing_user_is_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/users/01HZNONEXISTENTUSERID00000/archive")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "employee_not_found"


class TestGetUser:
    """Read projection exposed for the employee detail page."""

    def test_get_returns_projection(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, target_id = seeded
        r = client.get(f"/api/v1/users/{target_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == target_id
        assert body["display_name"] == "Target"
        assert body["engagement_archived_on"] is None

    def test_get_missing_is_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/users/01HZNONEXISTENTUSERID00000")
        assert r.status_code == 404, r.text
