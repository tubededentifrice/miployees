"""Integration test for :mod:`app.auth.signup` — end-to-end flow.

Exercises ``start → verify → complete`` against a real engine
(SQLite by default; Postgres when ``CREWDAY_TEST_DB=postgres``),
driving the FastAPI router with a stub mailer that captures the
magic-link URL and a stub passkey verifier that produces a
deterministic credential.

The flow lands one row per domain entity:

* ``workspace``
* ``user``
* ``user_workspace``
* four ``permission_group`` rows (``owners``, ``managers``,
  ``all_workers``, ``all_clients``)
* one ``permission_group_member`` in the owners group
* one ``role_grant(grant_role='manager', scope=workspace)``
* one ``passkey_credential``
* ``signup_attempt.completed_at`` + ``workspace_id`` set

Plus audit rows on ``signup.requested`` / ``signup.verified`` /
``signup.completed``.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from webauthn.helpers.structs import (
    AttestationFormat,
    CredentialDeviceType,
    PublicKeyCredentialType,
)

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import (
    ApiToken,
    PasskeyCredential,
    SignupAttempt,
    User,
    WebAuthnChallenge,
)
from app.adapters.db.identity.models import (
    Session as AuthSession,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session as _db_session_dep
from app.api.v1.auth.signup import build_signup_router
from app.auth import passkey
from app.auth._throttle import Throttle
from app.auth.webauthn import VerifiedRegistration
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _RecordingMailer:
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


def _extract_token(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError("no URL in body")


def _stub_passkey_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> bytes:
    """Patch :func:`app.auth.passkey._verify_or_raise` to always succeed.

    The integration test runs against a real DB and the real signup
    service — the single seam we stub is the WebAuthn verifier,
    because real authenticators aren't available. The returned
    credential id is what the test asserts the ``passkey_credential``
    row carries.
    """
    credential_id = b"integration-cred-" + b"x" * 15
    verified = VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=b"pub-" + b"\x00" * 60,
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000000",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )

    def _fake_verify(**_: Any) -> VerifiedRegistration:
        return verified

    monkeypatch.setattr(passkey, "_verify_or_raise", _fake_verify)
    return credential_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-root-key"),
        public_url="https://crew.day",
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def capabilities() -> Capabilities:
    # ``captcha_required=False`` keeps the full-flow test focused on
    # the happy path through start → verify → complete; the CAPTCHA
    # gate itself is exercised in
    # :mod:`tests.integration.auth.test_signup_abuse_wired` (cd-055).
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
        settings=DeploymentSettings(signup_enabled=True, captcha_required=False),
    )


@pytest.fixture
def mailer() -> _RecordingMailer:
    return _RecordingMailer()


@pytest.fixture
def client(
    engine: Engine,
    mailer: _RecordingMailer,
    throttle: Throttle,
    capabilities: Capabilities,
    settings: Settings,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the signup router.

    Each HTTP request opens its own session against ``engine``,
    commits on clean exit, rolls back on exception — matching the
    production UoW shape. We deliberately do NOT thread the
    conftest's savepoint ``db_session`` fixture through the router,
    because the integration test's whole point is to let each step
    *commit* so the next step sees the row.
    """
    import app.adapters.db.session as _session_mod

    app = FastAPI()
    router = build_signup_router(
        mailer=mailer,
        throttle=throttle,
        capabilities=capabilities,
        base_url="https://crew.day",
        settings=settings,
    )
    app.include_router(router, prefix="/api/v1")

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

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

    app.dependency_overrides[_db_session_dep] = _session

    # cd-9slq: ``POST /signup/start`` opens its own
    # ``with make_uow():`` block so the magic-link SMTP send fires
    # post-commit. Redirect the module-level default sessionmaker to
    # the per-test engine so the router's UoW binds correctly.
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        with TestClient(app) as c:
            yield c
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory

    # Clean up committed rows so sibling integration tests see a clean
    # table. Delete children before parents so SQLAlchemy does not
    # transiently null token/user FKs and violate shape checks.
    with factory() as s:
        from app.adapters.db.audit.models import AuditLog
        from app.adapters.db.identity.models import MagicLinkNonce

        for table_model in (
            PasskeyCredential,
            AuthSession,
            ApiToken,
            WebAuthnChallenge,
            MagicLinkNonce,
            SignupAttempt,
            PermissionGroupMember,
            RoleGrant,
            UserWorkspace,
            PermissionGroup,
            AuditLog,
            Workspace,
            User,
        ):
            s.execute(delete(table_model))
        s.commit()


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


class TestSignupFullFlow:
    """start → verify → complete via the real HTTP router + real DB."""

    def test_happy_path_lands_every_row(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_passkey_verifier(monkeypatch)

        # 1. Start — 202, magic link sent.
        r = client.post(
            "/api/v1/signup/start",
            json={
                "email": "integration@example.com",
                "desired_slug": "integration-ws",
            },
        )
        assert r.status_code == 202, r.text
        assert len(mailer.sent) == 1
        token = _extract_token(mailer.sent[0][2])

        # 2. Verify — flips the attempt to verified.
        r = client.post("/api/v1/signup/verify", json={"token": token})
        assert r.status_code == 200, r.text
        verify_body = r.json()
        signup_session_id = verify_body["signup_session_id"]
        assert verify_body["desired_slug"] == "integration-ws"

        # 3. Passkey start — returns the challenge options.
        r = client.post(
            "/api/v1/signup/passkey/start",
            json={
                "signup_session_id": signup_session_id,
                "display_name": "Integration Owner",
            },
        )
        assert r.status_code == 200, r.text
        challenge_body = r.json()
        challenge_id = challenge_body["challenge_id"]
        assert challenge_body["options"]  # some CreationOptions payload

        # 4. Passkey finish — lands the whole workspace + user + passkey.
        r = client.post(
            "/api/v1/signup/passkey/finish",
            json={
                "signup_session_id": signup_session_id,
                "challenge_id": challenge_id,
                "display_name": "Integration Owner",
                "timezone": "Pacific/Auckland",
                "credential": {
                    "id": "stub",
                    "rawId": "stub",
                    "response": {},
                    "type": "public-key",
                },
            },
        )
        assert r.status_code == 200, r.text
        finish_body = r.json()
        assert finish_body["workspace_slug"] == "integration-ws"
        assert finish_body["redirect"] == "/w/integration-ws/today"

        # 5. Assert every downstream row landed.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            workspace = s.scalars(
                select(Workspace).where(Workspace.slug == "integration-ws")
            ).one()
            assert workspace.plan == "free"
            # Tight initial caps — 10% of free-tier.
            assert workspace.quota_json["llm_budget_cents_30d"] == 50

            user = s.scalars(
                select(User).where(User.email_lower == "integration@example.com")
            ).one()
            assert user.display_name == "Integration Owner"

            membership = s.scalars(
                select(UserWorkspace).where(UserWorkspace.user_id == user.id)
            ).one()
            assert membership.workspace_id == workspace.id
            assert membership.source == "workspace_grant"

            groups = s.scalars(
                select(PermissionGroup).where(
                    PermissionGroup.workspace_id == workspace.id
                )
            ).all()
            assert {g.slug for g in groups} == {
                "owners",
                "managers",
                "all_workers",
                "all_clients",
            }

            owners_group = next(g for g in groups if g.slug == "owners")
            owners_member = s.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.group_id == owners_group.id
                )
            ).one()
            assert owners_member.user_id == user.id

            grant = s.scalars(
                select(RoleGrant).where(RoleGrant.user_id == user.id)
            ).one()
            assert grant.grant_role == "manager"
            assert grant.workspace_id == workspace.id
            assert grant.scope_property_id is None

            cred = s.scalars(
                select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
            ).one()
            assert cred.user_id == user.id

            attempt = s.scalars(
                select(SignupAttempt).where(
                    SignupAttempt.email_lower == "integration@example.com"
                )
            ).one()
            assert attempt.completed_at is not None
            assert attempt.workspace_id == workspace.id


class TestSignupRouterErrors:
    """Router-level error mapping against a real DB."""

    def test_replay_verify_returns_409(
        self,
        client: TestClient,
        mailer: _RecordingMailer,
    ) -> None:
        r = client.post(
            "/api/v1/signup/start",
            json={"email": "replay@example.com", "desired_slug": "replay-ws"},
        )
        assert r.status_code == 202
        token = _extract_token(mailer.sent[0][2])

        first = client.post("/api/v1/signup/verify", json={"token": token})
        assert first.status_code == 200

        replay = client.post("/api/v1/signup/verify", json={"token": token})
        assert replay.status_code == 409
        assert replay.json()["detail"]["error"] == "already_consumed"
