"""Integration test for :mod:`app.api.v1.auth.tokens` — end-to-end flow.

Exercises ``POST → GET → DELETE`` + a gated-route Bearer verify against
a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``), driving the FastAPI router with a
pinned :class:`WorkspaceContext` via the standard permission stack.

The flow lands:

* One :class:`ApiToken` row per mint.
* A :class:`~app.adapters.db.audit.models.AuditLog` trail:
  ``api_token.minted`` → ``api_token.revoked`` (and ``revoked_noop``
  on the idempotent retry).
* An HTTP 204 on revoke; 404 on an unknown ``token_id``.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/12-rest-api.md`` §"Auth / tokens".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

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
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.auth.tokens import build_tokens_router
from app.auth.tokens import verify as verify_token
from app.tenancy import WorkspaceContext, tenant_agnostic
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Per-test session factory that commits on clean exit."""
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seeded_ctx(
    session_factory: sessionmaker[Session],
) -> Iterator[WorkspaceContext]:
    """Seed a user + workspace + owners membership, yield a matching ctx.

    The permission gate on ``api_tokens.manage`` walks the user's
    owners-group membership to decide; seeding via
    :func:`bootstrap_workspace` lands the group + member rows so the
    default-allow (owners, managers) branch fires.

    Each test run gets a uniquely-named slug + email (derived from a
    fresh ULID) so sibling integration tests don't collide on the
    case-insensitive email unique index when they share the same
    session-scoped engine. Teardown drops every row we touched so
    the next test starts from a clean slate.
    """
    from app.util.ulid import new_ulid as _new_ulid

    tag = _new_ulid()[-8:].lower()
    email = f"mgr-{tag}@example.com"
    slug = f"ws-tok-{tag}"

    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="Manager")
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Tokens",
            owner_user_id=user.id,
        )
        s.commit()
        user_id, ws_id, ws_slug = user.id, ws.id, ws.slug

    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    yield ctx

    # Scoped cleanup — delete every row we seeded so concurrent /
    # sibling integration tests see a clean state. We walk the
    # workspace children first (the FK cascades would handle most of
    # this, but ``user_workspace`` / ``role_grant`` rows are
    # workspace-scoped and need the tenant filter bypassed).
    with session_factory() as s:
        with tenant_agnostic():
            for tok in s.scalars(
                select(ApiToken).where(ApiToken.workspace_id == ws_id)
            ).all():
                s.delete(tok)
            for audit in s.scalars(
                select(AuditLog).where(AuditLog.workspace_id == ws_id)
            ).all():
                s.delete(audit)
            for grant in s.scalars(
                select(RoleGrant).where(RoleGrant.workspace_id == ws_id)
            ).all():
                s.delete(grant)
            for member in s.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.workspace_id == ws_id
                )
            ).all():
                s.delete(member)
            for group in s.scalars(
                select(PermissionGroup).where(PermissionGroup.workspace_id == ws_id)
            ).all():
                s.delete(group)
            for uw in s.scalars(
                select(UserWorkspace).where(UserWorkspace.workspace_id == ws_id)
            ).all():
                s.delete(uw)
            ws_row = s.get(Workspace, ws_id)
            if ws_row is not None:
                s.delete(ws_row)
            user_row = s.get(User, user_id)
            if user_row is not None:
                s.delete(user_row)
        s.commit()


@pytest.fixture
def client(
    engine: Engine,
    session_factory: sessionmaker[Session],
    seeded_ctx: WorkspaceContext,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the tokens router.

    The permission gate reads :class:`WorkspaceContext` via
    :func:`current_workspace_context`; we override the dep so every
    request sees ``seeded_ctx``. The UoW dep yields a session on the
    shared engine and commits on clean exit — matching the production
    shape.
    """
    app = FastAPI()
    app.include_router(build_tokens_router(), prefix="/api/v1")

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
        return seeded_ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


class TestTokensHttpFlow:
    """POST → GET → DELETE via the real HTTP router + real DB."""

    def test_mint_then_list_then_revoke(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        # 1. Mint — 201, plaintext returned once.
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "hermes-scheduler",
                "scopes": {"tasks:read": True, "stays:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["token"].startswith("mip_")
        key_id = body["key_id"]
        assert len(key_id) == 26
        assert body["prefix"]
        assert body["expires_at"] is not None

        # 2. List — returns the row we just inserted.
        r = client.get("/api/v1/auth/tokens")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["key_id"] == key_id
        assert rows[0]["label"] == "hermes-scheduler"
        assert rows[0]["prefix"] == body["prefix"]
        assert rows[0]["scopes"] == {"tasks:read": True, "stays:read": True}
        # The hash is never in the list response.
        assert "hash" not in rows[0]
        # §03 "API tokens": plaintext `token` is returned ONLY on the
        # 201 mint response — never on subsequent list reads. cd-rpxd
        # acceptance criterion #3 — regression-pinned here so a future
        # schema edit that re-surfaces the secret fails loudly.
        assert "token" not in rows[0]

        # 3. Verify the plaintext token against the DB directly — this
        # mirrors what the future Bearer-auth middleware will do.
        with session_factory() as s:
            verified = verify_token(s, token=body["token"])
            assert verified.user_id == seeded_ctx.actor_id
            assert verified.workspace_id == seeded_ctx.workspace_id
            assert verified.key_id == key_id

        # 4. Revoke — 204.
        r = client.delete(f"/api/v1/auth/tokens/{key_id}")
        assert r.status_code == 204, r.text

        # 5. Verify fails post-revoke.
        from app.auth.tokens import TokenRevoked

        with session_factory() as s, pytest.raises(TokenRevoked):
            verify_token(s, token=body["token"])

    def test_revoke_unknown_token_is_404(self, client: TestClient) -> None:
        r = client.delete("/api/v1/auth/tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "token_not_found"

    def test_double_revoke_is_idempotent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "idem", "scopes": {}, "expires_at_days": 7},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]

        # First delete — 204.
        r1 = client.delete(f"/api/v1/auth/tokens/{key_id}")
        assert r1.status_code == 204
        # Second delete — still 204 (idempotent).
        r2 = client.delete(f"/api/v1/auth/tokens/{key_id}")
        assert r2.status_code == 204

        # Audit trail has one revoke + one revoked_noop.
        with session_factory() as s:
            audits = s.scalars(
                select(AuditLog).where(
                    AuditLog.workspace_id == seeded_ctx.workspace_id,
                    AuditLog.entity_id == key_id,
                )
            ).all()
            actions = [a.action for a in audits]
            assert "api_token.minted" in actions
            assert "api_token.revoked" in actions
            assert "api_token.revoked_noop" in actions

    def test_sixth_mint_is_422_too_many(
        self,
        client: TestClient,
    ) -> None:
        for i in range(5):
            r = client.post(
                "/api/v1/auth/tokens",
                json={
                    "label": f"t-{i}",
                    "scopes": {},
                    "expires_at_days": 30,
                },
            )
            assert r.status_code == 201, r.text
        r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "6th", "scopes": {}, "expires_at_days": 30},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "too_many_tokens"


# ---------------------------------------------------------------------------
# cd-i1qe — delegated tokens through POST /auth/tokens
# ---------------------------------------------------------------------------


class TestDelegatedTokensHttp:
    """``delegate: true`` mints a delegated row and the response echoes kind."""

    def test_delegated_mint_returns_kind_and_delegate_fk(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "chat-agent",
                "delegate": True,
                "scopes": {},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "delegated"
        key_id = body["key_id"]
        # The row carries the FK back to the caller.
        with session_factory() as s:
            row = s.get(ApiToken, key_id)
            assert row is not None
            assert row.kind == "delegated"
            assert row.delegate_for_user_id == seeded_ctx.actor_id
            assert row.workspace_id == seeded_ctx.workspace_id
        # GET /auth/tokens surfaces the row with the discriminator.
        r_list = client.get("/api/v1/auth/tokens")
        assert r_list.status_code == 200
        rows = r_list.json()
        match = next(row for row in rows if row["key_id"] == key_id)
        assert match["kind"] == "delegated"
        assert match["delegate_for_user_id"] == seeded_ctx.actor_id

    def test_delegated_with_nonempty_scopes_is_422(
        self,
        client: TestClient,
    ) -> None:
        """§03: delegated tokens reject non-empty scopes."""
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "bad",
                "delegate": True,
                "scopes": {"tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "delegated_requires_empty_scopes"

    def test_scoped_with_me_scope_is_422_conflict(
        self,
        client: TestClient,
    ) -> None:
        """Mixing me:* with a scoped token body is ``me_scope_conflict``."""
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "bad",
                "scopes": {"tasks:read": True, "me.tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "me_scope_conflict"

    def test_delegated_default_ttl_is_30_days(
        self,
        client: TestClient,
    ) -> None:
        """§03 "Guardrails": delegated tokens default to 30 days."""
        r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "agent", "delegate": True, "scopes": {}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # The response shape is ISO-8601; parse and compare deltas.
        expires = datetime.fromisoformat(body["expires_at"])
        now = datetime.now(tz=expires.tzinfo or UTC)
        # 30 days ±10s — comfortably inside a window that survives
        # test-runner clock drift.
        delta = expires - now
        assert timedelta(days=29, hours=23) <= delta <= timedelta(days=30, hours=1)

    def test_scoped_default_ttl_is_90_days(
        self,
        client: TestClient,
    ) -> None:
        r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "agent", "scopes": {}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        expires = datetime.fromisoformat(body["expires_at"])
        now = datetime.now(tz=expires.tzinfo or UTC)
        delta = expires - now
        assert timedelta(days=89, hours=23) <= delta <= timedelta(days=90, hours=1)
