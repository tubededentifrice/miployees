"""Integration tests for the cd-9slq outbox boundary across the 5 callers.

Each test injects a commit-time failure into the production HTTP
router's :class:`~app.adapters.db.session.UnitOfWorkImpl` and asserts
that no SMTP send leaves the host. Mirrors
:class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering
.test_commit_failure_before_deliver_does_not_send_email` at the HTTP
layer for every flow that mints a magic link:

* ``POST /api/v1/signup/start`` — :func:`app.auth.signup.start_signup`
* ``POST /api/v1/recover/passkey/request`` —
  :func:`app.auth.recovery.request_recovery`
* ``POST /api/v1/users/invite`` —
  :func:`app.domain.identity.membership.invite`
* ``POST /api/v1/users/{id}/magic_link`` —
  :func:`app.api.v1.users._issue_passkey_recovery_link`
* ``POST /api/v1/me/email/change_request`` —
  :func:`app.domain.identity.email_change.request_change`

The §15 invariant: "no working magic-link token leaves the host
without a matching nonce + audit_log row that has been committed."

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format",
``docs/specs/15-security-privacy.md`` §"Self-service lost-device &
email-change abuse mitigations", and the ``cd-9slq`` Beads task.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db import session as session_module
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    EmailChangePending,
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    SignupAttempt,
    User,
)
from app.adapters.db.session import UnitOfWorkImpl
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import (
    current_workspace_context,
)
from app.api.deps import (
    db_session as _db_session_dep,
)
from app.api.v1.auth.email_change import build_email_change_router
from app.api.v1.auth.recovery import build_recovery_router
from app.api.v1.auth.signup import build_signup_router
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings
from app.tenancy import WorkspaceContext

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _RecordingMailer:
    """Captures every :meth:`Mailer.send` call so tests can assert
    on cadence without touching SMTP.
    """

    sent: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        del body_html, headers, reply_to
        self.sent.append((tuple(to), subject, body_text))
        return "test-message-id"


# ---------------------------------------------------------------------------
# UoW commit-failure injector
# ---------------------------------------------------------------------------


@pytest.fixture
def force_commit_failure() -> Iterator[None]:
    """Replace :class:`UnitOfWorkImpl` with a commit-failing variant.

    Mirrors the unit-level shape (overriding ``session.commit`` on a
    per-session basis) but operates at the factory level — the
    cd-9slq routers each build their own UoW via ``make_uow()`` so
    we can't reach into the per-request session from outside. The
    replacement raises on ``__exit__`` after rolling back, simulating
    the cd-t2jz reproducer (schema drift on ``audit_log``, FK
    violation, transient driver error — anything that flips the
    caller's UoW to fail-closed at commit time).
    """

    class _CommitFailingUoW(UnitOfWorkImpl):
        def __exit__(
            self,
            exc_type: object,
            exc_val: object,
            exc_tb: object,
        ) -> bool:
            if self._session is not None:
                try:
                    self._session.rollback()
                finally:
                    self._session.close()
                    self._session = None
            raise RuntimeError("simulated commit failure")

    original = session_module.UnitOfWorkImpl
    session_module.UnitOfWorkImpl = _CommitFailingUoW  # type: ignore[misc]
    try:
        yield
    finally:
        session_module.UnitOfWorkImpl = original  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-cd-9slq-outbox-root-key"),
        public_url=_BASE_URL,
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Build a sessionmaker bound to the session-scoped integration
    engine + sweep every domain table on teardown so sibling tests
    see a clean DB.

    The integration engine is shared across the session (see
    ``tests/integration/conftest.py``); per-test cleanup keeps the
    cd-9slq integration suite hermetic.
    """
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        yield f
    finally:
        with f() as s:
            from app.adapters.db.identity.models import (
                EmailChangePending as _ECP,
            )
            from app.adapters.db.identity.models import (
                MagicLinkNonce as _MLN,
            )
            from app.adapters.db.identity.models import (
                PasskeyCredential as _PK,
            )
            from app.adapters.db.identity.models import (
                Session as _Sess,
            )
            from app.adapters.db.identity.models import (
                SignupAttempt as _SA,
            )

            for model in (
                _ECP,
                _MLN,
                _SA,
                _PK,
                _Sess,
                Invite,
                AuditLog,
                PermissionGroupMember,
                RoleGrant,
                PermissionGroup,
            ):
                for row in s.scalars(select(model)).all():
                    s.delete(row)
            for ws_link in s.scalars(select(UserWorkspace)).all():
                s.delete(ws_link)
            for u in s.scalars(select(User)).all():
                s.delete(u)
            for w in s.scalars(select(Workspace)).all():
                s.delete(w)
            s.commit()


@pytest.fixture
def mailer() -> _RecordingMailer:
    return _RecordingMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def capabilities() -> Capabilities:
    return Capabilities(
        features=Features(
            rls=False,
            fulltext_search=False,
            concurrent_writers=False,
            object_storage=False,
            wildcard_subdomains=False,
            email_bounce_webhooks=False,
            llm_voice_input=False,
        ),
        # Captcha disabled so the test can drive ``/signup/start``
        # without supplying a Turnstile token; the abuse gates we
        # want to keep (rate + disposable) still fire.
        settings=DeploymentSettings(signup_enabled=True, captcha_required=False),
    )


@pytest.fixture(autouse=True)
def _redirect_default_uow(
    engine: Engine,
    factory: sessionmaker[Session],
) -> Iterator[None]:
    """Bind ``make_uow`` to the per-test engine so the routers'
    ``with make_uow():`` blocks hit the seeded DB.
    """
    original_engine = session_module._default_engine
    original_factory = session_module._default_sessionmaker_
    session_module._default_engine = engine
    session_module._default_sessionmaker_ = factory
    try:
        yield
    finally:
        session_module._default_engine = original_engine
        session_module._default_sessionmaker_ = original_factory


def _build_session_dep(
    factory: sessionmaker[Session],
) -> Any:
    def _session() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return _session


# ---------------------------------------------------------------------------
# Fixture: workspace + owner seed for the workspace-scoped routers
# ---------------------------------------------------------------------------


@dataclass
class _Seed:
    workspace_id: str
    workspace_slug: str
    owner_id: str
    target_id: str
    cookie_value: str


@pytest.fixture
def seed(
    factory: sessionmaker[Session],
    settings: Settings,
) -> _Seed:
    """Seed a workspace + owner + target user with a live session.

    The owner gets one passkey credential so the email-change cool-
    off check (which gates on "newest passkey > 15 minutes old")
    passes — the seeded credential is created at ``_PINNED - 1
    hour`` so the cool-off does not fire.
    """
    from datetime import timedelta

    from app.util.ulid import new_ulid
    from tests.factories.identity import bootstrap_user, bootstrap_workspace

    with factory() as s:
        owner = bootstrap_user(s, email="owner@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-outbox",
            name="Outbox WS",
            owner_user_id=owner.id,
        )
        target = bootstrap_user(s, email="target@example.com", display_name="Target")
        s.add(
            UserWorkspace(
                user_id=target.id,
                workspace_id=ws.id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        # Worker grant for the target so the route can find them.
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ws.id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        # Seeded passkey on the owner so the email-change cool-off
        # check passes (newest credential must be older than 15 min).
        s.add(
            PasskeyCredential(
                id=b"pk-owner-cd-9slq",
                user_id=owner.id,
                public_key=b"test-pk",
                sign_count=0,
                transports=None,
                backup_eligible=False,
                label="seeded",
                created_at=_PINNED - timedelta(hours=1),
                last_used_at=None,
            )
        )
        s.commit()
        owner_id = owner.id
        target_id = target.id
        ws_id = ws.id
        ws_slug = ws.slug

    # Issue a real session cookie for the email-change route.
    from app.util.clock import FrozenClock

    clock = FrozenClock(_PINNED)
    with factory() as s:
        result = issue(
            s,
            user_id=owner_id,
            workspace_id=None,
            ip="127.0.0.1",
            ua="pytest-cd-9slq",
            accept_language="en",
            clock=clock,
            settings=settings,
            has_owner_grant=True,
        )
        cookie_value = result.cookie_value
        s.commit()

    return _Seed(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        owner_id=owner_id,
        target_id=target_id,
        cookie_value=cookie_value,
    )


# ---------------------------------------------------------------------------
# Tests — one per caller
# ---------------------------------------------------------------------------


class TestSignupCommitFailureNoEmailLeak:
    """``POST /signup/start`` — commit fails → no SMTP send."""

    def test_commit_failure_does_not_send_email(
        self,
        engine: Engine,
        factory: sessionmaker[Session],
        mailer: _RecordingMailer,
        throttle: Throttle,
        capabilities: Capabilities,
        settings: Settings,
        force_commit_failure: None,
    ) -> None:
        del force_commit_failure
        app = FastAPI()
        app.include_router(
            build_signup_router(
                mailer=mailer,
                throttle=throttle,
                capabilities=capabilities,
                base_url=_BASE_URL,
                settings=settings,
            ),
            prefix="/api/v1",
        )
        app.dependency_overrides[_db_session_dep] = _build_session_dep(factory)

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(
                "/api/v1/signup/start",
                json={"email": "newuser@example.com", "desired_slug": "villa-9slq"},
            )

        assert r.status_code == 500, r.text
        assert mailer.sent == [], (
            f"signup mailer fired despite commit failure: {mailer.sent!r}"
        )
        # Rolled-back rows are gone on the engine.
        with factory() as s:
            assert s.scalars(select(SignupAttempt)).all() == []
            assert s.scalars(select(MagicLinkNonce)).all() == []
            assert (
                s.scalars(
                    select(AuditLog).where(AuditLog.action == "signup.requested")
                ).all()
                == []
            )


class TestRecoveryCommitFailureNoEmailLeak:
    """``POST /recover/passkey/request`` — commit fails → no SMTP send."""

    def test_commit_failure_does_not_send_email(
        self,
        engine: Engine,
        factory: sessionmaker[Session],
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        seed: _Seed,
        force_commit_failure: None,
    ) -> None:
        del force_commit_failure, seed
        # The hit branch needs a user row; the seed fixture already
        # created ``owner@example.com``.
        app = FastAPI()
        app.include_router(
            build_recovery_router(
                mailer=mailer,
                throttle=throttle,
                base_url=_BASE_URL,
                settings=settings,
            ),
            prefix="/api/v1",
        )
        app.dependency_overrides[_db_session_dep] = _build_session_dep(factory)

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(
                "/api/v1/recover/passkey/request",
                json={"email": "owner@example.com"},
            )

        assert r.status_code == 500, r.text
        assert mailer.sent == [], (
            f"recovery mailer fired despite commit failure: {mailer.sent!r}"
        )
        with factory() as s:
            assert s.scalars(select(MagicLinkNonce)).all() == []
            assert (
                s.scalars(
                    select(AuditLog).where(AuditLog.action == "recovery.requested")
                ).all()
                == []
            )


class TestInviteCommitFailureNoEmailLeak:
    """``POST /users/invite`` — commit fails → no SMTP send."""

    def test_commit_failure_does_not_send_email(
        self,
        factory: sessionmaker[Session],
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        seed: _Seed,
        force_commit_failure: None,
    ) -> None:
        del force_commit_failure
        ctx = WorkspaceContext(
            workspace_id=seed.workspace_id,
            workspace_slug=seed.workspace_slug,
            actor_id=seed.owner_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000CRL9",
        )
        app = FastAPI()
        app.include_router(
            build_users_router(
                mailer=mailer,
                throttle=throttle,
                settings=settings,
                base_url=_BASE_URL,
            ),
        )
        app.dependency_overrides[_db_session_dep] = _build_session_dep(factory)
        app.dependency_overrides[current_workspace_context] = lambda: ctx

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(
                "/users/invite",
                json={
                    "email": "invitee@example.com",
                    "display_name": "Invitee",
                    "grants": [
                        {
                            "scope_kind": "workspace",
                            "scope_id": seed.workspace_id,
                            "grant_role": "worker",
                        }
                    ],
                },
            )

        assert r.status_code == 500, r.text
        assert mailer.sent == [], (
            f"invite mailer fired despite commit failure: {mailer.sent!r}"
        )
        # Rolled-back invite + nonce + audit are gone.
        with factory() as s:
            assert s.scalars(select(Invite)).all() == []
            invite_audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "user.invited")
            ).all()
            assert invite_audits == []


class TestUsersMagicLinkCommitFailureNoEmailLeak:
    """``POST /users/{id}/magic_link`` — commit fails → no SMTP send."""

    def test_commit_failure_does_not_send_email(
        self,
        factory: sessionmaker[Session],
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        seed: _Seed,
        force_commit_failure: None,
    ) -> None:
        del force_commit_failure
        ctx = WorkspaceContext(
            workspace_id=seed.workspace_id,
            workspace_slug=seed.workspace_slug,
            actor_id=seed.owner_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000CRL8",
        )
        app = FastAPI()
        app.include_router(
            build_users_router(
                mailer=mailer,
                throttle=throttle,
                settings=settings,
                base_url=_BASE_URL,
            ),
        )
        app.dependency_overrides[_db_session_dep] = _build_session_dep(factory)
        app.dependency_overrides[current_workspace_context] = lambda: ctx

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post(f"/users/{seed.target_id}/magic_link", json={})

        assert r.status_code == 500, r.text
        assert mailer.sent == [], (
            f"users magic_link mailer fired despite commit failure: {mailer.sent!r}"
        )
        with factory() as s:
            assert s.scalars(select(MagicLinkNonce)).all() == []


class TestEmailChangeRequestCommitFailureNoEmailLeak:
    """``POST /me/email/change_request`` — commit fails → no SMTP send."""

    def test_commit_failure_does_not_send_email(
        self,
        factory: sessionmaker[Session],
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        seed: _Seed,
        force_commit_failure: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del force_commit_failure
        # Pin :func:`get_settings` everywhere the email-change layer
        # reads it so peppers line up with the seeded session cookie.
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        monkeypatch.setattr("app.auth.magic_link.get_settings", lambda: settings)
        monkeypatch.setattr(
            "app.domain.identity.email_change.get_settings", lambda: settings
        )

        app = FastAPI()
        app.include_router(
            build_email_change_router(
                mailer=mailer,
                throttle=throttle,
                settings=settings,
            ),
            prefix="/api/v1",
        )
        app.dependency_overrides[_db_session_dep] = _build_session_dep(factory)

        with TestClient(
            app,
            base_url="https://testserver",
            headers={
                "User-Agent": "pytest-cd-9slq",
                "Accept-Language": "en",
            },
            raise_server_exceptions=False,
        ) as c:
            r = c.post(
                "/api/v1/me/email/change_request",
                json={"new_email": "alice.new@example.com"},
                cookies={SESSION_COOKIE_NAME: seed.cookie_value},
            )

        assert r.status_code == 500, r.text
        assert mailer.sent == [], (
            f"email-change request mailer fired despite commit failure: {mailer.sent!r}"
        )
        # Rolled-back pending row + nonce + audit are gone.
        with factory() as s:
            assert s.scalars(select(EmailChangePending)).all() == []
            change_audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "email.change_requested")
            ).all()
            assert change_audits == []
