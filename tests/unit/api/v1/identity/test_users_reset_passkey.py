"""HTTP-level tests for ``POST /users/{id}/reset_passkey`` (cd-y5z3).

Exercises the owner-only "reset a worker's passkey" surface:

* owner can trigger a reset (default-allow includes ``owners`` only);
* manager (without owners-group membership) is rejected with 403;
* worker is rejected with 403;
* sends TWO emails — one to the worker (real magic link), one to the
  owner (non-consumable notification copy);
* writes a ``user.reset_passkey.initiated`` audit row;
* cross-workspace target collapses to 404 ``employee_not_found``;
* the owner's notification copy carries the worker's email masked
  (no plaintext address in the notice body).

See ``docs/specs/03-auth-and-tokens.md`` §"Owner-initiated worker
passkey reset" and ``docs/specs/12-rest-api.md`` §"Users".
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
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-users-reset-passkey-root-key"),
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


def _seed_worker_member(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> str:
    """Seed a workspace member (``worker`` role + ``UserWorkspace``)."""
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


def _seed_manager_user(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> str:
    """Seed a manager (``manager`` role grant + workspace membership).

    Importantly, this user is NOT in the ``owners`` permission group
    — only the ``bootstrap_workspace`` owner is. This is the persona
    the ``reset_passkey`` AuthZ test needs to confirm "managers fall
    through to 403".
    """
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role="manager",
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
    with factory() as s, tenant_agnostic():
        return [
            row.action
            for row in s.scalars(select(AuditLog).order_by(AuditLog.created_at)).all()
        ]


# ---------------------------------------------------------------------------
# AuthZ
# ---------------------------------------------------------------------------


class TestAuthZ:
    def test_owner_allowed(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker_member(
            factory,
            workspace_id=ws_id,
            email="rpworker@example.com",
            display_name="RP Worker",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{worker_id}/reset_passkey")
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["user_id"] == worker_id
        assert body["status"] == "sent"

    def test_manager_rejected_403(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """A manager (not in owners group) cannot trigger reset_passkey.

        The action key ``users.reset_passkey`` has
        ``default_allow=("owners",)`` — managers do NOT inherit it
        even though they hold the manager surface. They can still
        re-issue a magic link via the lighter ``/users/{id}/magic_link``
        surface; ``reset_passkey`` is the owners-only break-glass door.
        """
        owner_full_ctx, factory, ws_id = owner_ctx
        del owner_full_ctx  # only the workspace bootstrap is needed

        manager_id = _seed_manager_user(
            factory,
            workspace_id=ws_id,
            email="rpmanager@example.com",
            display_name="RP Manager",
        )
        worker_id = _seed_worker_member(
            factory,
            workspace_id=ws_id,
            email="rpworker2@example.com",
            display_name="RP Worker 2",
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
        r = client.post(f"/users/{worker_id}/reset_passkey")
        assert r.status_code == 403, r.text
        body = r.json()
        assert body["detail"]["error"] == "permission_denied"
        assert body["detail"]["action_key"] == "users.reset_passkey"

    def test_worker_rejected_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, _ws_id, worker_id = worker_ctx
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{worker_id}/reset_passkey")
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "permission_denied"


# ---------------------------------------------------------------------------
# Audit + dual-mail side effects
# ---------------------------------------------------------------------------


class TestAuditAndMail:
    def test_writes_initiated_audit(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker_member(
            factory,
            workspace_id=ws_id,
            email="rpaudit@example.com",
            display_name="RP Audit",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{worker_id}/reset_passkey")
        assert r.status_code == 202, r.text
        actions = _audit_actions(factory)
        assert "user.reset_passkey.initiated" in actions
        # Magic-link service writes its own row in the same UoW.
        assert "magic_link.sent" in actions

    def test_sends_two_mails(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker_member(
            factory,
            workspace_id=ws_id,
            email="rpdual@example.com",
            display_name="RP Dual",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{worker_id}/reset_passkey")
        assert r.status_code == 202, r.text
        # Two emails: one to the worker (consumable link), one to the
        # owner (non-consumable notification copy).
        assert len(mailer.sent) == 2

        recipients = {msg.to[0] for msg in mailer.sent}
        # Worker's plaintext email is in the recipient set; owner's is
        # ``owner@example.com`` from :func:`bootstrap_user` in conftest.
        assert "rpdual@example.com" in recipients
        assert "owner@example.com" in recipients

    def test_owner_notice_masks_worker_email(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """The owner-side notification body masks the worker's address.

        Spec §03 "Owner-initiated worker passkey reset" pins the
        ``m***@example.com`` shape so a forwarded copy doesn't leak
        the worker's plaintext address. The worker-bound email DOES
        carry the plaintext (it's their own address), but the
        owner-bound notice MUST not.
        """
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker_member(
            factory,
            workspace_id=ws_id,
            email="marie.dupont@example.com",
            display_name="Marie Dupont",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{worker_id}/reset_passkey")
        assert r.status_code == 202, r.text

        # Find the message bound for the owner (NOT the worker).
        owner_msgs = [msg for msg in mailer.sent if msg.to == ("owner@example.com",)]
        assert len(owner_msgs) == 1
        body = owner_msgs[0].body_text
        # Plaintext address must NOT appear in the owner's notice.
        assert "marie.dupont@example.com" not in body
        # Masked form ``m***@example.com`` MUST appear.
        assert "m***@example.com" in body


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
        ctx, factory, _ = owner_ctx
        with factory() as s:
            sibling_owner = bootstrap_user(
                s,
                email="sib-owner-rp@example.com",
                display_name="Sib RP",
            )
            bootstrap_workspace(
                s,
                slug="ws-sibling-rp",
                name="Sibling RP",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            sibling_id = sibling_owner.id
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        r = client.post(f"/users/{sibling_id}/reset_passkey")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "employee_not_found"
