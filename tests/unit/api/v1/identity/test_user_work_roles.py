"""HTTP-level tests for ``/user_work_roles`` (cd-dcfw)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.workspace.models import UserWorkRole, UserWorkspace, WorkRole
from app.api.v1.user_work_roles import (
    build_user_work_roles_router,
    build_users_user_work_roles_router,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.unit.api.v1.identity.conftest import build_client


def _owner_client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [
            ("", build_user_work_roles_router()),
            ("", build_users_user_work_roles_router()),
        ],
        factory,
        ctx,
    )


def _seed_work_role(factory: sessionmaker[Session], workspace_id: str) -> str:
    with factory() as s:
        row = WorkRole(
            id=new_ulid(),
            workspace_id=workspace_id,
            key="maid",
            name="Maid",
            description_md="",
            default_settings_json={},
            icon_name="",
            created_at=datetime.now(tz=UTC),
            deleted_at=None,
        )
        s.add(row)
        s.commit()
        return row.id


def _seed_user_membership(
    factory: sessionmaker[Session], *, user_id: str, workspace_id: str
) -> None:
    with factory() as s:
        s.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=datetime.now(tz=UTC),
            )
        )
        s.commit()


class TestCreate:
    def test_owner_creates_link(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        # The owner is already attached to the workspace via the
        # ``bootstrap_workspace`` seed — no extra membership needed.
        client = _owner_client(ctx, factory)
        resp = client.post(
            "/user_work_roles",
            json={
                "user_id": ctx.actor_id,
                "work_role_id": role_id,
                "started_on": "2026-04-01",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == ctx.actor_id
        assert body["work_role_id"] == role_id
        assert body["ended_on"] is None

    def test_rejects_non_member_user(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        client = _owner_client(ctx, factory)
        resp = client.post(
            "/user_work_roles",
            json={
                "user_id": "01HWNOTMEMBER0000000000000",
                "work_role_id": role_id,
                "started_on": "2026-04-01",
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "user_work_role_invariant"

    def test_worker_cannot_create(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, ws_id, _ = worker_ctx
        role_id = _seed_work_role(factory, ws_id)
        client = build_client([("", build_user_work_roles_router())], factory, ctx)
        resp = client.post(
            "/user_work_roles",
            json={
                "user_id": ctx.actor_id,
                "work_role_id": role_id,
                "started_on": "2026-04-01",
            },
        )
        assert resp.status_code == 403


class TestList:
    def test_list_returns_user_rows(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        client = _owner_client(ctx, factory)
        client.post(
            "/user_work_roles",
            json={
                "user_id": ctx.actor_id,
                "work_role_id": role_id,
                "started_on": "2026-04-01",
            },
        )
        resp = client.get(f"/users/{ctx.actor_id}/user_work_roles")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        assert len(body["data"]) == 1

    def test_list_of_other_user_respects_tenancy(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """An owner can always list any user — tenancy is the only gate."""
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        # Unknown user -> 200 + empty (the user has no links in this ws).
        resp = client.get("/users/01HWUNKNOWN000000000000000/user_work_roles")
        assert resp.status_code == 200
        assert resp.json() == {"data": [], "next_cursor": None, "has_more": False}

    def test_pagination_envelope(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        # Seed three distinct roles + three user_work_role rows.
        role_ids: list[str] = []
        with factory() as s:
            for key in ("maid", "cook", "driver"):
                role = WorkRole(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    key=key,
                    name=key.title(),
                    description_md="",
                    default_settings_json={},
                    icon_name="",
                    created_at=datetime.now(tz=UTC),
                    deleted_at=None,
                )
                s.add(role)
                role_ids.append(role.id)
            s.commit()
        client = _owner_client(ctx, factory)
        for rid in role_ids:
            client.post(
                "/user_work_roles",
                json={
                    "user_id": ctx.actor_id,
                    "work_role_id": rid,
                    "started_on": "2026-04-01",
                },
            )
        resp = client.get(f"/users/{ctx.actor_id}/user_work_roles?limit=2")
        body = resp.json()
        assert resp.status_code == 200
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        assert len(body["data"]) == 2

        # Walk forward with the cursor.
        resp2 = client.get(
            f"/users/{ctx.actor_id}/user_work_roles"
            f"?cursor={body['next_cursor']}&limit=2"
        )
        body2 = resp2.json()
        assert body2["has_more"] is False
        assert len(body2["data"]) == 1


class TestPatch:
    def test_patch_ended_on(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        client = _owner_client(ctx, factory)
        created = client.post(
            "/user_work_roles",
            json={
                "user_id": ctx.actor_id,
                "work_role_id": role_id,
                "started_on": "2026-04-01",
            },
        ).json()
        resp = client.patch(
            f"/user_work_roles/{created['id']}", json={"ended_on": "2026-05-01"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ended_on"] == "2026-05-01"

    def test_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.patch("/user_work_roles/nope", json={"ended_on": "2026-05-01"})
        assert resp.status_code == 404


class TestDelete:
    def test_delete_returns_204(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        client = _owner_client(ctx, factory)
        created = client.post(
            "/user_work_roles",
            json={
                "user_id": ctx.actor_id,
                "work_role_id": role_id,
                "started_on": "2026-04-01",
            },
        ).json()
        resp = client.delete(f"/user_work_roles/{created['id']}")
        assert resp.status_code == 204
        # Row is soft-deleted — row lives in DB but is invisible.
        with factory() as s:
            row = s.get(UserWorkRole, created["id"])
            assert row is not None
            assert row.deleted_at is not None

    def test_delete_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.delete("/user_work_roles/nope")
        assert resp.status_code == 404


class TestOpenApiShape:
    def test_list_carries_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/users/{user_id}/user_work_roles"]["get"]
        assert "identity" in op["tags"]
        assert "user_work_roles" in op["tags"]
