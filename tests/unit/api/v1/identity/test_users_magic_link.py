"""HTTP-level tests for ``POST /users/{id}/magic_link`` (cd-y5z3).

Exercises the manager-tier "re-mail a recovery magic link" surface:

* manager / owner can issue a magic link to a workspace member;
* worker is rejected with 403 ``permission_denied``;
* cross-workspace target collapses to 404 ``employee_not_found``;
* writes a ``user.magic_link.issued`` audit row + the magic-link
  service's own ``magic_link.sent`` row;
* honours the ``email_to_use`` override for the destination.

See ``docs/specs/12-rest-api.md`` §"Users" and
``docs/specs/03-auth-and-tokens.md`` §"Magic links".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.workspace.models import UserWorkspace
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import _PINNED, build_client, ctx_for

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def settings() -> Settings:
    """Pin a fixed root_key + public_url so magic-link wires work."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-users-magic-link-root-key"),
        public_url="https://test.crew.day",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


def _client(
    ctx: WorkspaceContext,
    factory: sessionmaker[Session],
    *,
    mailer: InMemoryMailer,
    throttle: Throttle,
    settings: Settings,
) -> TestClient:
    """Mount ``build_users_router`` against pinned ctx + factory."""
    return build_client(
        [
            (
                "",
                build_users_router(
                    mailer=mailer,
                    throttle=throttle,
                    settings=settings,
                    base_url=settings.public_url,
                ),
            )
        ],
        factory,
        ctx,
    )


def _seed_target_user(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> str:
    """Seed a workspace member (``UserWorkspace`` + ``role_grant``)."""
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.add(
            UserWorkspace(
                user_id=user.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        s.commit()
        return user.id


def _audit_actions(factory: sessionmaker[Session]) -> list[str]:
    """Return every ``audit_log.action`` value in insertion order."""
    with factory() as s, tenant_agnostic():
        return [
            row.action
            for row in s.scalars(select(AuditLog).order_by(AuditLog.created_at)).all()
        ]


# ---------------------------------------------------------------------------
# AuthZ
# ---------------------------------------------------------------------------


class TestAuthZ:
    def test_owner_can_issue_magic_link(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_target_user(
            factory,
            workspace_id=ws_id,
            email="target@example.com",
            display_name="Target",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{target_id}/magic_link", json={})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["user_id"] == target_id
        assert body["status"] == "sent"

    def test_manager_can_issue_magic_link(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Managers (default-allow includes ``managers``) succeed too.

        Distinct from the owner-allowed case so a future spec change
        that demoted ``users.edit_profile_other`` to owners-only
        surfaces here as a regression rather than passing silently.
        """
        owner_full_ctx, factory, ws_id = owner_ctx
        del owner_full_ctx
        # Seed a manager (NOT in owners group) to act as the caller.
        with factory() as s:
            manager = bootstrap_user(
                s,
                email="ml-manager@example.com",
                display_name="ML Manager",
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=manager.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.add(
                UserWorkspace(
                    user_id=manager.id,
                    workspace_id=ws_id,
                    source="workspace_grant",
                    added_at=_PINNED,
                )
            )
            s.commit()
            manager_id = manager.id
        target_id = _seed_target_user(
            factory,
            workspace_id=ws_id,
            email="ml-target@example.com",
            display_name="ML Target",
        )
        manager_ctx = ctx_for(
            workspace_id=ws_id,
            workspace_slug="ws-identity",
            actor_id=manager_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(
            manager_ctx,
            factory,
            mailer=mailer,
            throttle=throttle,
            settings=settings,
        )
        r = client.post(f"/users/{target_id}/magic_link", json={})
        assert r.status_code == 202, r.text

    def test_worker_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Workers do not hold ``users.edit_profile_other``."""
        ctx, factory, _ws_id, worker_id = worker_ctx
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        # Workers acting on themselves still fail because the gate is
        # the FastAPI :class:`Permission` dep (no self-edit shortcut).
        r = client.post(f"/users/{worker_id}/magic_link", json={})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "permission_denied"


# ---------------------------------------------------------------------------
# Audit + side effects
# ---------------------------------------------------------------------------


class TestAudit:
    def test_writes_user_magic_link_issued_audit(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_target_user(
            factory,
            workspace_id=ws_id,
            email="audited@example.com",
            display_name="Audited",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{target_id}/magic_link", json={})
        assert r.status_code == 202, r.text

        actions = _audit_actions(factory)
        # Both the dedicated route audit row AND the magic-link
        # service's own ``magic_link.sent`` row land in the same UoW.
        assert "user.magic_link.issued" in actions
        assert "magic_link.sent" in actions

    def test_email_sent_to_user_default(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_target_user(
            factory,
            workspace_id=ws_id,
            email="onfile@example.com",
            display_name="On File",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{target_id}/magic_link", json={})
        assert r.status_code == 202, r.text
        # The route renders + sends one mail; the magic-link service
        # itself runs through a capturing mailer (so its generic
        # template is intercepted) and does NOT touch the recording
        # mailer.
        assert len(mailer.sent) == 1
        assert mailer.sent[0].to == ("onfile@example.com",)

    def test_email_to_use_override_redirects_destination(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_target_user(
            factory,
            workspace_id=ws_id,
            email="onfile@example.com",
            display_name="On File",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(
            f"/users/{target_id}/magic_link",
            json={"email_to_use": "alt@example.com"},
        )
        assert r.status_code == 202, r.text
        assert len(mailer.sent) == 1
        assert mailer.sent[0].to == ("alt@example.com",)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_cross_workspace_target_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """A user in workspace B is invisible from workspace A's caller."""
        ctx, factory, _ = owner_ctx
        with factory() as s:
            sibling_owner = bootstrap_user(
                s, email="sib-owner@example.com", display_name="Sib"
            )
            bootstrap_workspace(
                s,
                slug="ws-sibling-magic",
                name="Sibling",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            sibling_id = sibling_owner.id
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        # The caller is in workspace A; sibling_id only has rows in
        # workspace B. The membership probe must collapse to 404 to
        # avoid leaking the sibling's existence.
        r = client.post(f"/users/{sibling_id}/magic_link", json={})
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "employee_not_found"


class TestPostMagicLinkOutboxOrdering:
    """cd-9slq: SMTP send must run *after* the magic-link nonce +
    audit are durable.

    Mirrors :class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering`
    at the manager-mediated reissue surface. The router opens its own
    ``with make_uow() as session:`` block; if the commit fails (schema
    drift, FK violation, transient driver error) the deferred SMTP
    send is never invoked, so no working ``recover_passkey`` token
    reaches the inbox without the matching nonce + audit row durable
    on disk.
    """

    def test_commit_failure_before_deliver_does_not_send_email(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Inject a commit failure into the router's UoW; assert the
        recording mailer never fires.
        """
        from app.adapters.db import session as session_module
        from app.adapters.db.session import UnitOfWorkImpl

        ctx, factory, ws_id = owner_ctx
        target_id = _seed_target_user(
            factory,
            workspace_id=ws_id,
            email="outbox-target@example.com",
            display_name="Outbox Target",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )

        # Install a wrapper around UnitOfWorkImpl that raises on
        # commit. Mirrors the session-level commit override in
        # :class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering`
        # but applied at the ``make_uow`` factory layer because the
        # router owns the UoW lifetime here.
        original_uow_init = UnitOfWorkImpl.__init__

        class _CommitFailingUoW(UnitOfWorkImpl):
            def __exit__(
                self,
                exc_type: object,
                exc_val: object,
                exc_tb: object,
            ) -> bool:
                # Roll back to keep the session consistent (mirrors the
                # real ``UnitOfWorkImpl.__exit__`` rollback path) then
                # raise so the router's outer ``with`` propagates a
                # commit-time failure.
                if self._session is not None:
                    try:
                        self._session.rollback()
                    finally:
                        self._session.close()
                        self._session = None
                raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(session_module, "UnitOfWorkImpl", _CommitFailingUoW)
        del original_uow_init

        # The router will hit the failure inside its ``with
        # make_uow():`` block; FastAPI's TestClient was built with
        # ``raise_server_exceptions=False`` so the failure surfaces
        # as a 500 instead of propagating into the test.
        r = client.post(f"/users/{target_id}/magic_link", json={})
        assert r.status_code == 500
        # The mailer was never invoked — the queued reissue-template
        # send is post-commit and the failure short-circuits it
        # (cd-9slq invariant).
        assert mailer.sent == [], (
            f"mailer was invoked despite commit failure: {mailer.sent!r}"
        )
