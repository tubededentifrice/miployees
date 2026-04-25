"""HTTP-level tests for ``/user_leaves`` (cd-oydd).

Covers the CRUD + approval-state-machine contract per spec §12
"Users / work roles / settings" and §06 "user_leave":

* Self-submit by a worker lands pending (``approved_at IS NULL``).
* Self-submit by a manager / owner auto-approves at insert.
* Manager retroactive create on someone else lands auto-approved.
* PATCH is pending-only; approved leaves reject with 409.
* Approve stamps ``approved_at`` + ``approved_by``; second approve = 409.
* Reject soft-deletes the row, folds ``reason_md`` into ``note_md``,
  and writes a ``user_leave.rejected`` audit row.
* DELETE is soft and idempotent surface-wise (404 on second call).
* Workers cannot approve / reject / view-others / edit-others.
* Cross-workspace probes collapse to 404.
* Listing is cursor-paginated with the §12 envelope shape.
* OpenAPI tags carry ``identity`` + ``user_leaves``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.availability.models import UserLeave
from app.api.v1.user_leaves import build_user_leaves_router
from app.tenancy import WorkspaceContext
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_user_leaves_router())], factory, ctx)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_owner_self_create_auto_approves(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner creating their own leave lands ``approved_at`` populated."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-05",
                "category": "vacation",
                "note_md": "Annual break",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == ctx.actor_id
        assert body["category"] == "vacation"
        assert body["starts_on"] == "2026-05-01"
        assert body["ends_on"] == "2026-05-05"
        assert body["approved_at"] is not None
        assert body["approved_by"] == ctx.actor_id
        assert body["deleted_at"] is None

    def test_manager_self_create_auto_approves(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Manager grant (no owner-membership) still auto-approves their own.

        ``actor_grant_role='manager'`` qualifies even when the caller
        is not in the owners group — covers the workspace-manager
        path explicitly so the behaviour is not accidentally tied to
        owner-bootstrap shape.
        """
        ctx, factory, ws_id = owner_ctx
        # Mint a fresh manager who is NOT in the owners group.
        with factory() as s:
            mgr = bootstrap_user(s, email="mgr@example.com", display_name="Mgr")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=mgr.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            mgr_id = mgr.id
        mgr_ctx = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=mgr_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(mgr_ctx, factory)

        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-06-01",
                "ends_on": "2026-06-03",
                "category": "personal",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approved_at"] is not None
        assert body["approved_by"] == mgr_id

    def test_worker_self_create_lands_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker self-submit lands ``approved_at = null``."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "sick",
                "note_md": "Flu",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == ctx.actor_id
        assert body["approved_at"] is None
        assert body["approved_by"] is None

    def test_manager_create_for_other_auto_approves(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Manager retroactive create on a worker's behalf auto-approves."""
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            worker = bootstrap_user(
                s, email="worker-leave@example.com", display_name="Worker"
            )
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id

        client = _client(ctx, factory)
        resp = client.post(
            "/user_leaves",
            json={
                "user_id": worker_id,
                "starts_on": "2026-08-01",
                "ends_on": "2026-08-05",
                "category": "vacation",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == worker_id
        assert body["approved_at"] is not None
        assert body["approved_by"] == ctx.actor_id

    def test_worker_cannot_create_for_other(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker creating a leave for someone else collapses to 403."""
        ctx, factory, _ws_id, _ = worker_ctx
        with factory() as s:
            other = bootstrap_user(s, email="other@example.com", display_name="Other")
            s.commit()
            other_id = other.id
        client = _client(ctx, factory)

        resp = client.post(
            "/user_leaves",
            json={
                "user_id": other_id,
                "starts_on": "2026-09-01",
                "ends_on": "2026-09-02",
                "category": "personal",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "permission_denied"

    def test_invalid_window_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``ends_on < starts_on`` is rejected at the DTO boundary."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-10",
                "ends_on": "2026-05-01",
                "category": "vacation",
            },
        )
        # Pydantic validation collapses to 422 at the FastAPI layer.
        assert resp.status_code == 422

    def test_invalid_category_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Unknown category strings reject at the DTO layer."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "marriage",
            },
        )
        assert resp.status_code == 422

    def test_create_writes_audit(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``user_leave.created`` audit row lands in the same transaction."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        )
        assert resp.status_code == 201
        leave_id = resp.json()["id"]
        with factory() as s:
            rows = list(
                s.scalars(select(AuditLog).where(AuditLog.entity_id == leave_id)).all()
            )
        assert len(rows) == 1
        assert rows[0].action == "user_leave.created"
        assert rows[0].entity_kind == "user_leave"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_list_self_no_capability_required(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker can list their own leaves without ``leaves.view_others``."""
        ctx, factory, _, worker_id = worker_ctx
        client = _client(ctx, factory)
        client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        )

        resp = client.get(f"/user_leaves?user_id={worker_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["user_id"] == worker_id

    def test_worker_cannot_list_others(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker listing ``?user_id=<other>`` collapses to 403."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/user_leaves?user_id=01HWOTHER000000000000000")
        assert resp.status_code == 403

    def test_worker_cannot_list_inbox(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Bare GET (no ``user_id``) is the manager inbox — 403 for workers."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/user_leaves")
        assert resp.status_code == 403

    def test_owner_lists_inbox(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner sees every workspace leave on the bare GET."""
        ctx, factory, ws_id = owner_ctx
        # Seed two leaves: one by the owner, one by a fresh worker.
        client = _client(ctx, factory)
        client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        )
        with factory() as s:
            worker = bootstrap_user(s, email="w@example.com", display_name="Worker")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id
        client.post(
            "/user_leaves",
            json={
                "user_id": worker_id,
                "starts_on": "2026-06-01",
                "ends_on": "2026-06-02",
                "category": "sick",
            },
        )

        resp = client.get("/user_leaves")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2
        assert {r["user_id"] for r in body["data"]} == {ctx.actor_id, worker_id}

    def test_list_paginated(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Cursor envelope walks forward across multiple pages."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        for i in range(3):
            client.post(
                "/user_leaves",
                json={
                    "starts_on": f"2026-05-{i + 1:02d}",
                    "ends_on": f"2026-05-{i + 1:02d}",
                    "category": "personal",
                },
            )

        resp = client.get("/user_leaves?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        assert len(body["data"]) == 2

        resp2 = client.get(f"/user_leaves?cursor={body['next_cursor']}&limit=2")
        body2 = resp2.json()
        assert resp2.status_code == 200
        assert body2["has_more"] is False
        assert len(body2["data"]) == 1

    def test_list_filter_approved(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``?approved=true`` narrows to approved rows; ``=false`` to pending."""
        ctx, factory, ws_id = owner_ctx
        owner_client = _client(ctx, factory)
        # Owner self-create is auto-approved.
        owner_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-01",
                "category": "vacation",
            },
        )
        # Worker self-create is pending.
        with factory() as s:
            worker = bootstrap_user(s, email="ww@example.com", display_name="W")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        worker_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-10",
                "ends_on": "2026-05-10",
                "category": "sick",
            },
        )

        approved = owner_client.get("/user_leaves?approved=true").json()
        pending = owner_client.get("/user_leaves?approved=false").json()
        assert len(approved["data"]) == 1
        assert approved["data"][0]["approved_at"] is not None
        assert len(pending["data"]) == 1
        assert pending["data"][0]["approved_at"] is None

    def test_list_bad_cursor_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A malformed cursor surfaces 422 ``invalid_cursor`` per §12.

        Mirrors :func:`app.api.pagination.decode_cursor`'s contract
        (a tampered cursor gives a loud error rather than a silent
        "reset to first page"). Locks the wire envelope so a UI bug
        sending the next_cursor with extra whitespace is debuggable.
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/user_leaves?cursor=!!!not-base64")
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "invalid_cursor"

    def test_list_filter_date_window(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``?from=`` / ``?to=`` slice the date range."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        for d in ("2026-05-01", "2026-06-01", "2026-07-01"):
            client.post(
                "/user_leaves",
                json={"starts_on": d, "ends_on": d, "category": "personal"},
            )

        resp = client.get("/user_leaves?from=2026-06-01&to=2026-06-30")
        body = resp.json()
        assert resp.status_code == 200
        assert len(body["data"]) == 1
        assert body["data"][0]["starts_on"] == "2026-06-01"


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


class TestPatch:
    def test_worker_can_edit_own_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker editing their own pending leave succeeds."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()

        resp = client.patch(
            f"/user_leaves/{created['id']}",
            json={"ends_on": "2026-05-04", "note_md": "Extended"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ends_on"] == "2026-05-04"
        assert body["note_md"] == "Extended"

    def test_patch_approved_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """An approved row rejects PATCH with 409."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        # Owner self-create lands approved.
        created = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        ).json()
        resp = client.patch(
            f"/user_leaves/{created['id']}", json={"ends_on": "2026-05-05"}
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "user_leave_transition_forbidden"

    def test_patch_invalid_window_422(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """PATCHing one edge so ``ends_on < starts_on`` is rejected as 422."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-10",
                "ends_on": "2026-05-15",
                "category": "personal",
            },
        ).json()

        # Send only ``ends_on`` — the row's ``starts_on=2026-05-10``
        # then makes ``2026-05-01 < 2026-05-10`` and the service
        # rejects with the invariant error.
        resp = client.patch(
            f"/user_leaves/{created['id']}", json={"ends_on": "2026-05-01"}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "user_leave_invariant"

    def test_patch_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.patch("/user_leaves/nope", json={"note_md": "x"})
        assert resp.status_code == 404

    def test_worker_cannot_patch_others_pending(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Worker editing another user's pending leave collapses to 403.

        Authorisation regression: PATCH must route the cross-user case
        through ``leaves.edit_others``. A worker without that
        capability cannot mutate a peer's pending row even though the
        row itself is in the editable state.
        """
        ctx, factory, ws_id = owner_ctx
        # Mint two workers; the first owns a pending leave, the second
        # is the unauthorised editor.
        with factory() as s:
            owner_w = bootstrap_user(s, email="ow@example.com", display_name="OW")
            attacker = bootstrap_user(s, email="atk@example.com", display_name="ATK")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            for u in (owner_w, attacker):
                s.add(
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=ws_id,
                        user_id=u.id,
                        grant_role="worker",
                        scope_property_id=None,
                        created_at=datetime.now(tz=UTC),
                        created_by_user_id=None,
                    )
                )
            s.commit()
            owner_w_id = owner_w.id
            attacker_id = attacker.id

        owner_w_ctx = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=owner_w_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        owner_w_client = _client(owner_w_ctx, factory)
        leave = owner_w_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()

        attacker_ctx = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=attacker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        attacker_client = _client(attacker_ctx, factory)
        resp = attacker_client.patch(
            f"/user_leaves/{leave['id']}",
            json={"note_md": "tampering"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "permission_denied"


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_owner_approves_worker_pending(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner approves a worker's pending leave; second approve = 409."""
        ctx, factory, ws_id = owner_ctx
        # Seed worker + their pending leave.
        with factory() as s:
            worker = bootstrap_user(s, email="wapp@example.com", display_name="W")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        leave = worker_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()
        assert leave["approved_at"] is None

        owner_client = _client(ctx, factory)
        resp = owner_client.post(f"/user_leaves/{leave['id']}/approve")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["approved_at"] is not None
        assert body["approved_by"] == ctx.actor_id

        # Second approve = 409.
        resp2 = owner_client.post(f"/user_leaves/{leave['id']}/approve")
        assert resp2.status_code == 409

        # Audit chain: created + approved.
        with factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(AuditLog.entity_id == leave["id"])
                ).all()
            )
        actions = sorted(r.action for r in rows)
        assert actions == ["user_leave.approved", "user_leave.created"]

    def test_worker_cannot_approve(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker hitting approve on their own pending leave is 403."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        leave = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()
        resp = client.post(f"/user_leaves/{leave['id']}/approve")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


class TestReject:
    def test_owner_rejects_with_reason(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Reject soft-deletes, folds reason into note_md, audits transition."""
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            worker = bootstrap_user(s, email="wrej@example.com", display_name="W")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        leave = worker_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
                "note_md": "Family trip",
            },
        ).json()

        owner_client = _client(ctx, factory)
        resp = owner_client.post(
            f"/user_leaves/{leave['id']}/reject",
            json={"reason_md": "Coverage gap that week"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_at"] is not None
        assert "Family trip" in (body["note_md"] or "")
        assert "Rejected: Coverage gap that week" in (body["note_md"] or "")

        # Row hidden from default listing (tombstone filter).
        listing = owner_client.get(f"/user_leaves?user_id={worker_id}").json()
        assert listing["data"] == []

        # Audit row carries the rejected action.
        with factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(AuditLog.entity_id == leave["id"])
                ).all()
            )
        assert any(r.action == "user_leave.rejected" for r in rows)

    def test_reject_without_body(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Reject without a body still soft-deletes the row."""
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            worker = bootstrap_user(s, email="wnobody@example.com", display_name="W")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        leave = worker_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()

        owner_client = _client(ctx, factory)
        resp = owner_client.post(f"/user_leaves/{leave['id']}/reject")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_at"] is not None

    def test_reject_approved_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Rejecting an already-approved row collapses to 409."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        leave = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        ).json()
        # Owner self-create is approved.
        resp = client.post(f"/user_leaves/{leave['id']}/reject")
        assert resp.status_code == 409

    def test_worker_cannot_reject(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        leave = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()
        resp = client.post(f"/user_leaves/{leave['id']}/reject")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_worker_withdraws_own_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker DELETEs their own pending leave; row is tombstoned."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        leave = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        ).json()

        resp = client.delete(f"/user_leaves/{leave['id']}")
        assert resp.status_code == 204

        with factory() as s:
            row = s.get(UserLeave, leave["id"])
            assert row is not None
            assert row.deleted_at is not None

        # Second DELETE = 404 (tombstone hidden from _load_row).
        resp2 = client.delete(f"/user_leaves/{leave['id']}")
        assert resp2.status_code == 404

    def test_owner_revokes_approved(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner DELETEs an approved row (the only path to revoke)."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        leave = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        ).json()
        resp = client.delete(f"/user_leaves/{leave['id']}")
        assert resp.status_code == 204

    def test_worker_cannot_delete_other(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A worker DELETing someone else's row is 403."""
        ctx, factory, ws_id = owner_ctx
        # Seed a worker and have them request a leave.
        with factory() as s:
            worker = bootstrap_user(s, email="wdel@example.com", display_name="W")
            from app.adapters.db.authz.models import RoleGrant
            from app.util.ulid import new_ulid

            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            worker_id = worker.id
        # Owner creates a leave for themselves (auto-approved).
        owner_client = _client(ctx, factory)
        leave = owner_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        ).json()

        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        resp = worker_client.delete(f"/user_leaves/{leave['id']}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cross-workspace
# ---------------------------------------------------------------------------


class TestCrossWorkspace:
    def test_cross_workspace_blocked(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A row in workspace A is invisible from workspace B's caller.

        Seeds two workspaces in the same DB. The owner of WS-A creates
        a leave; the WS-B owner cannot read, patch, approve, reject,
        or delete it. The 404 surface is identical to "never existed"
        per §01 "tenant surface is not enumerable".
        """
        ctx_a, factory, _ = owner_ctx
        client_a = _client(ctx_a, factory)
        leave = client_a.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "vacation",
            },
        ).json()

        with factory() as s:
            owner_b = bootstrap_user(
                s, email="owner-b-ul@example.com", display_name="Owner B"
            )
            ws_b = bootstrap_workspace(
                s, slug="ws-leaves-b", name="WS B", owner_user_id=owner_b.id
            )
            s.commit()
            ctx_b = ctx_for(
                workspace_id=ws_b.id,
                workspace_slug=ws_b.slug,
                actor_id=owner_b.id,
                grant_role="manager",
                actor_was_owner_member=True,
            )
        client_b = _client(ctx_b, factory)

        # GET inbox: empty for B.
        listing = client_b.get("/user_leaves").json()
        assert listing["data"] == []

        # Sub-resource verbs: 404 for B.
        for path in (
            f"/user_leaves/{leave['id']}/approve",
            f"/user_leaves/{leave['id']}/reject",
        ):
            r = client_b.post(path)
            assert r.status_code == 404, path

        r_patch = client_b.patch(f"/user_leaves/{leave['id']}", json={"note_md": "x"})
        assert r_patch.status_code == 404

        r_del = client_b.delete(f"/user_leaves/{leave['id']}")
        assert r_del.status_code == 404


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


class TestOpenApiShape:
    def test_routes_carry_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/user_leaves"]["get"]
        assert "identity" in op["tags"]
        assert "user_leaves" in op["tags"]


# ---------------------------------------------------------------------------
# Auto-approve regression: a worker self-submit must NOT auto-approve
# (covers the negative path through `_can_edit_others`).
# ---------------------------------------------------------------------------


class TestAutoApproveRegression:
    def test_pending_row_is_visible_via_approved_false(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker self-submit shows up under ``?approved=false``.

        Regression check that the auto-approve code path doesn't
        accidentally stamp ``approved_at`` for workers.
        """
        ctx, factory, _, worker_id = worker_ctx
        client = _client(ctx, factory)
        client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
            },
        )
        body = client.get(f"/user_leaves?user_id={worker_id}&approved=false").json()
        assert len(body["data"]) == 1
        body_approved = client.get(
            f"/user_leaves?user_id={worker_id}&approved=true"
        ).json()
        assert body_approved["data"] == []
