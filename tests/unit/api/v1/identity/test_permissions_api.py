"""HTTP-level tests for ``/permissions`` + ``/permission_rules`` (cd-jinb).

Two routers in one file because they share fixtures and trade in the
same authz seam:

* ``/permissions/action_catalog`` — read-only static catalog.
* ``/permissions/resolved`` — non-raising resolver.
* ``/permissions/resolved/self`` — current-actor resolver for route guards.
* ``/permission_rules`` — root-only rule CRUD; v1 reality is the
  table doesn't exist yet so the GET surfaces empty + cursor scaffold,
  POST/DELETE 503 with the action gate firing first.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.api.v1.permission_rules import build_permission_rules_router
from app.api.v1.permissions import build_permissions_router
from app.domain.identity._action_catalog import ACTION_CATALOG
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.unit.api.v1.identity.conftest import build_client, ctx_for


def _permissions_client(
    ctx: WorkspaceContext, factory: sessionmaker[Session]
) -> TestClient:
    return build_client(
        [("", build_permissions_router())],
        factory,
        ctx,
    )


def _rules_client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [("", build_permission_rules_router())],
        factory,
        ctx,
    )


# ---------------------------------------------------------------------------
# /permissions/action_catalog
# ---------------------------------------------------------------------------


class TestActionCatalog:
    def test_owner_can_read(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get("/permissions/action_catalog")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == len(ACTION_CATALOG)
        keys = {e["key"] for e in body["entries"]}
        # Spot-check a few well-known entries from §05.
        assert "permissions.edit_rules" in keys
        assert "scope.view" in keys
        assert "tasks.create" in keys

    def test_worker_can_read(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``scope.view`` is the gate; default-allow includes all_workers."""
        ctx, factory, _, _ = worker_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get("/permissions/action_catalog")
        assert resp.status_code == 200

    def test_entry_shape_matches_spec(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get("/permissions/action_catalog")
        body = resp.json()
        sample = next(
            e for e in body["entries"] if e["key"] == "permissions.edit_rules"
        )
        assert sample["root_only"] is True
        assert "workspace" in sample["valid_scope_kinds"]
        assert sample["default_allow"] == []


# ---------------------------------------------------------------------------
# /permissions/resolved
# ---------------------------------------------------------------------------


class TestPermissionsResolved:
    def test_owner_resolves_root_only_action_to_allow(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": ctx.actor_id,
                "action_key": "permissions.edit_rules",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["effect"] == "allow"
        assert body["source_layer"] == "root_only"
        assert body["matched_groups"] == ["owners"]
        assert body["source_rule_id"] is None

    def test_non_owner_resolves_root_only_to_deny(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A user who is not in the owners group is denied root-only actions."""
        ctx, factory, ws_id = owner_ctx
        client = _permissions_client(ctx, factory)
        # ``some_random_user_id`` has no rows whatsoever — the resolver
        # short-circuits on the root-only gate to deny.
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": "01HWASTRANGER00000000000XX",
                "action_key": "permissions.edit_rules",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["effect"] == "deny"
        assert body["source_layer"] == "root_only"
        assert body["matched_groups"] == []

    def test_default_allow_path(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner of the workspace falls through to default_allow."""
        ctx, factory, ws_id = owner_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": ctx.actor_id,
                "action_key": "scope.view",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        body = resp.json()
        assert body["effect"] == "allow"
        assert body["source_layer"] == "default_allow"
        # ``owners`` is in default_allow for ``scope.view``.
        assert "owners" in body["matched_groups"]

    def test_unknown_action_key_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": ctx.actor_id,
                "action_key": "totally.fake",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "unknown_action_key"

    def test_invalid_scope_kind_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _permissions_client(ctx, factory)
        # ``payroll.lock_period`` only accepts ``scope_kind=workspace``.
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": ctx.actor_id,
                "action_key": "payroll.lock_period",
                "scope_kind": "property",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "invalid_scope_kind"

    def test_no_match_path(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A stranger with no grants on a non-owners-default action denies."""
        ctx, factory, ws_id = owner_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": "01HWASTRANGER00000000000XX",
                "action_key": "tasks.create",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        body = resp.json()
        assert body["effect"] == "deny"
        assert body["source_layer"] == "no_match"
        assert body["matched_groups"] == []

    def test_derived_managers_match(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A user with a manager role_grant matches the derived ``managers``
        group on default_allow."""
        ctx, factory, ws_id = owner_ctx
        # Seed a separate manager (no owners-group membership). Two
        # statements separated by a flush so the user FK is visible to
        # the RoleGrant insert; SQLite's FK enforcement is per-statement.
        from app.adapters.db.identity.models import User
        from tests.factories.identity import bootstrap_user

        with factory() as s:
            user = bootstrap_user(s, email="mgr@example.com", display_name="Mgr")
            manager_id = user.id
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=manager_id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
        del User  # silence the unused-import lint check
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": manager_id,
                "action_key": "tasks.create",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        body = resp.json()
        assert body["effect"] == "allow"
        assert "managers" in body["matched_groups"]

    def test_worker_cannot_read_resolved(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``audit_log.view`` is the gate; workers fail by default."""
        ctx, factory, ws_id, _ = worker_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved",
            params={
                "user_id": ctx.actor_id,
                "action_key": "scope.view",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 403

    def test_worker_can_resolve_own_scope_view_without_audit_gate(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Route guards can resolve the current actor without ``audit_log.view``."""
        ctx, factory, ws_id, _ = worker_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved/self",
            params={
                "action_key": "scope.view",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["effect"] == "allow"
        assert "all_workers" in body["matched_groups"]

    def test_worker_self_resolves_approvals_read_to_deny(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, ws_id, _ = worker_ctx
        client = _permissions_client(ctx, factory)
        resp = client.get(
            "/permissions/resolved/self",
            params={
                "action_key": "approvals.read",
                "scope_kind": "workspace",
                "scope_id": ws_id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["effect"] == "deny"
        assert body["source_layer"] == "no_match"
        assert body["matched_groups"] == []


# ---------------------------------------------------------------------------
# /permission_rules
# ---------------------------------------------------------------------------


class TestPermissionRules:
    def test_owner_list_returns_empty_page(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """v1: the table doesn't exist yet, GET always returns empty."""
        ctx, factory, _ = owner_ctx
        client = _rules_client(ctx, factory)
        resp = client.get("/permission_rules")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    def test_owner_post_returns_503(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _rules_client(ctx, factory)
        resp = client.post(
            "/permission_rules",
            json={
                "scope_kind": "workspace",
                "scope_id": ws_id,
                "action_key": "tasks.create",
                "subject_kind": "user",
                "subject_id": ctx.actor_id,
                "effect": "allow",
            },
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "permission_rule_table_unavailable"

    def test_owner_delete_returns_503(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _rules_client(ctx, factory)
        resp = client.delete("/permission_rules/01HWAFAKERULE0000000000000")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "permission_rule_table_unavailable"

    def test_non_owner_get_blocked_by_root_only_gate(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``permissions.edit_rules`` is root-only; a non-owner manager fails."""
        ctx, factory, ws_id = owner_ctx
        # A manager who is NOT in the owners permission group.
        from tests.factories.identity import bootstrap_user

        with factory() as s:
            user = bootstrap_user(s, email="rules@example.com", display_name="Rules")
            non_owner_id = user.id
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=non_owner_id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
        non_owner_ctx_obj = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=non_owner_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _rules_client(non_owner_ctx_obj, factory)
        resp = client.get("/permission_rules")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "permission_denied"

    def test_invalid_cursor_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _rules_client(ctx, factory)
        resp = client.get("/permission_rules?cursor=!!!bad!!!")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


class TestOpenApiShape:
    def test_permissions_routes_carry_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _permissions_client(ctx, factory)
        schema = client.get("/openapi.json").json()
        for path in (
            "/permissions/action_catalog",
            "/permissions/resolved",
            "/permissions/resolved/self",
        ):
            for op in schema["paths"][path].values():
                assert "identity" in op["tags"]
                assert "permissions" in op["tags"]

    def test_permission_rules_routes_carry_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _rules_client(ctx, factory)
        schema = client.get("/openapi.json").json()
        for path in ("/permission_rules", "/permission_rules/{rule_id}"):
            for op in schema["paths"][path].values():
                assert "identity" in op["tags"]
                assert "permission_rules" in op["tags"]
