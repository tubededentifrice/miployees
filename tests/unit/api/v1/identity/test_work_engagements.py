"""HTTP-level tests for ``/work_engagements`` (cd-dcfw)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.workspace.models import WorkEngagement
from app.api.v1.work_engagements import build_work_engagements_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.unit.api.v1.identity.conftest import build_client


def _owner_client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_work_engagements_router())], factory, ctx)


def _seed_engagement(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    kind: str = "payroll",
    supplier_org_id: str | None = None,
    archived_on: date | None = None,
) -> str:
    with factory() as s:
        row = WorkEngagement(
            id=new_ulid(),
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind=kind,
            supplier_org_id=supplier_org_id,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=date(2026, 4, 1),
            archived_on=archived_on,
            notes_md="",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(row)
        s.commit()
        return row.id


class TestList:
    def test_owner_lists_engagements(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        _seed_engagement(factory, user_id=ctx.actor_id, workspace_id=ws_id)
        client = _owner_client(ctx, factory)
        resp = client.get("/work_engagements")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["data"]) == 1

    def test_user_id_filter(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        _seed_engagement(factory, user_id=ctx.actor_id, workspace_id=ws_id)
        _seed_engagement(factory, user_id="other-user", workspace_id=ws_id)
        client = _owner_client(ctx, factory)
        resp = client.get(f"/work_engagements?user_id={ctx.actor_id}")
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["user_id"] == ctx.actor_id

    def test_active_filter(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        _seed_engagement(factory, user_id=ctx.actor_id, workspace_id=ws_id)
        _seed_engagement(
            factory,
            user_id="other-user",
            workspace_id=ws_id,
            archived_on=date(2026, 3, 1),
        )
        client = _owner_client(ctx, factory)
        resp = client.get("/work_engagements?active=true")
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["archived_on"] is None


class TestRead:
    def test_read_happy_path(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        engagement_id = _seed_engagement(
            factory, user_id=ctx.actor_id, workspace_id=ws_id
        )
        client = _owner_client(ctx, factory)
        resp = client.get(f"/work_engagements/{engagement_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == engagement_id

    def test_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.get("/work_engagements/unknown")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "work_engagement_not_found"


class TestPatch:
    def test_patch_notes_and_destination(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        engagement_id = _seed_engagement(
            factory, user_id=ctx.actor_id, workspace_id=ws_id
        )
        client = _owner_client(ctx, factory)
        resp = client.patch(
            f"/work_engagements/{engagement_id}",
            json={"notes_md": "Added note", "pay_destination_id": "dest_123"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["notes_md"] == "Added note"
        assert body["pay_destination_id"] == "dest_123"

    def test_switch_to_agency_without_supplier_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        engagement_id = _seed_engagement(
            factory, user_id=ctx.actor_id, workspace_id=ws_id
        )
        client = _owner_client(ctx, factory)
        resp = client.patch(
            f"/work_engagements/{engagement_id}",
            json={"engagement_kind": "agency_supplied"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "work_engagement_invariant"

    def test_worker_patch_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, ws_id, worker_id = worker_ctx
        engagement_id = _seed_engagement(factory, user_id=worker_id, workspace_id=ws_id)
        client = build_client([("", build_work_engagements_router())], factory, ctx)
        resp = client.patch(
            f"/work_engagements/{engagement_id}", json={"notes_md": "x"}
        )
        assert resp.status_code == 403


class TestArchiveReinstate:
    def test_archive_sets_archived_on(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        engagement_id = _seed_engagement(
            factory, user_id=ctx.actor_id, workspace_id=ws_id
        )
        client = _owner_client(ctx, factory)
        resp = client.post(f"/work_engagements/{engagement_id}/archive")
        assert resp.status_code == 200
        assert resp.json()["archived_on"] is not None

    def test_archive_is_idempotent(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        engagement_id = _seed_engagement(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            archived_on=date(2026, 3, 1),
        )
        client = _owner_client(ctx, factory)
        resp = client.post(f"/work_engagements/{engagement_id}/archive")
        assert resp.status_code == 200
        # Still archived — no reset.
        assert resp.json()["archived_on"] == "2026-03-01"

    def test_reinstate_clears_archived_on(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        engagement_id = _seed_engagement(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            archived_on=date(2026, 3, 1),
        )
        client = _owner_client(ctx, factory)
        resp = client.post(f"/work_engagements/{engagement_id}/reinstate")
        assert resp.status_code == 200
        assert resp.json()["archived_on"] is None

    def test_archive_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.post("/work_engagements/unknown/archive")
        assert resp.status_code == 404

    def test_reinstate_conflicting_active_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Second active engagement on reinstate must 422, not 500.

        Exercises the pre-flush guard that replaces the former
        partial-UNIQUE ``IntegrityError`` surface.
        """
        ctx, factory, ws_id = owner_ctx
        # Archived row we will try to reinstate.
        archived_id = _seed_engagement(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            archived_on=date(2026, 3, 1),
        )
        # Active row that blocks the reinstate via the partial UNIQUE.
        _seed_engagement(factory, user_id=ctx.actor_id, workspace_id=ws_id)
        client = _owner_client(ctx, factory)
        resp = client.post(f"/work_engagements/{archived_id}/reinstate")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "work_engagement_invariant"


class TestOpenApiShape:
    def test_has_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        schema = client.get("/openapi.json").json()
        list_op = schema["paths"]["/work_engagements"]["get"]
        assert "identity" in list_op["tags"]
        assert "work_engagements" in list_op["tags"]
