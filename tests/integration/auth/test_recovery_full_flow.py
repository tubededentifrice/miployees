"""Integration test for :mod:`app.auth.recovery` — end-to-end HTTP flow.

Exercises ``request → verify → passkey/start → passkey/finish``
against a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``) driving the FastAPI router with a stub
mailer that captures the magic-link URL and a stub passkey verifier
that produces a deterministic credential.

The flow must, in one happy path:

* issue a magic link whose URL lands at
  ``/recover/enroll?token=...``;
* consume it and return a recovery session handle that is distinct
  from any :class:`AuthSession` row;
* revoke every prior passkey + web session for the user when the
  finish route lands;
* insert one fresh passkey row bound to the user.

Plus error-path coverage:

* unknown recovery session → 404;
* replayed finish → 404 (session was consumed);
* rate-limit trip → 429 with ``Retry-After``.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service lost-device
recovery", §"Recovery paths" and ``docs/specs/15-security-privacy.md``
§"Self-service lost-device & email-change abuse mitigations".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
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

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import (
    ApiToken,
    BreakGlassCode,
    MagicLinkNonce,
    PasskeyCredential,
    User,
)
from app.adapters.db.identity.models import (
    Session as AuthSession,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session as _db_session_dep
from app.api.v1.auth.recovery import build_recovery_router
from app.auth import passkey
from app.auth import recovery as recovery_module
from app.auth._throttle import Throttle
from app.auth.webauthn import VerifiedRegistration
from app.config import Settings
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

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


def _extract_recovery_token(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if "recover/enroll?token=" in stripped:
            return stripped.rsplit("=", 1)[-1]
    raise AssertionError(f"no recovery URL in body: {body!r}")


def _stub_passkey_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> bytes:
    """Patch :func:`app.auth.passkey._verify_or_raise` with a stub.

    Returns the deterministic credential id the stub produces, so
    tests can assert the persisted passkey row carries it.
    """
    credential_id = b"recovery-cred-" + b"x" * 18
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


def _workspace_slug(workspace_id: str) -> str:
    """Return a slug that remains unique under parallel test execution."""
    return f"ws-{workspace_id.lower()}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-recovery-root-key"),
        public_url="https://crew.day",
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def mailer() -> _RecordingMailer:
    return _RecordingMailer()


@pytest.fixture(autouse=True)
def reset_recovery_store() -> Iterator[None]:
    """Clear the in-memory recovery-session store between tests."""
    recovery_module._RECOVERY_SESSIONS.clear()
    yield
    recovery_module._RECOVERY_SESSIONS.clear()


@pytest.fixture
def client(
    engine: Engine,
    mailer: _RecordingMailer,
    throttle: Throttle,
    settings: Settings,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the recovery router."""
    import app.adapters.db.session as _session_mod

    app = FastAPI()
    router = build_recovery_router(
        mailer=mailer,
        throttle=throttle,
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

    # cd-9slq: the router opens its own ``with make_uow():`` block to
    # commit before firing the deferred SMTP send. ``make_uow`` reads
    # the module-level default sessionmaker — without this redirect
    # the router's UoW would bind to whatever DB the default factory
    # was last built for instead of this test's per-test engine.
    # Mirrors :mod:`tests.tenant.conftest` and the autouse fixture
    # in :mod:`tests.unit.api.v1.identity.conftest`.
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

    # Sweep committed rows so sibling tests see a clean table.
    with factory() as s:
        # ``RoleGrant`` sweeps before ``Workspace`` because the FK
        # cascade would otherwise hard-delete grants under the
        # workspace-delete, and SQLAlchemy's per-row ``delete()`` in
        # the loop below would fail to find them the second pass.
        # Ordering deletes FK-children first also mirrors how a real
        # tenant hard-delete would run.
        for table_model in (
            BreakGlassCode,
            PasskeyCredential,
            AuthSession,
            ApiToken,
            MagicLinkNonce,
            RoleGrant,
            UserWorkspace,
            Workspace,
            User,
            AuditLog,
        ):
            s.execute(delete(table_model))
        s.commit()


def _seed_user_with_state(
    engine: Engine,
    *,
    email: str,
    display_name: str,
    passkey_count: int,
    session_count: int,
) -> tuple[str, str]:
    """Seed a user with pre-existing passkeys + auth sessions.

    Returns ``(user_id, workspace_id)``. The workspace is needed as
    the FK target for the web sessions.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        workspace_id = new_ulid()
        s.add(
            Workspace(
                id=workspace_id,
                slug=_workspace_slug(workspace_id),
                name="Placeholder",
                plan="free",
                quota_json={},
                created_at=user.created_at,
            )
        )
        s.flush()
        for i in range(passkey_count):
            s.add(
                PasskeyCredential(
                    id=bytes([0xB0 + i]) * 32,
                    user_id=user.id,
                    public_key=b"\x00" * 32,
                    sign_count=0,
                    backup_eligible=False,
                    created_at=user.created_at,
                )
            )
        for _ in range(session_count):
            # Live sessions — expires in the future so the cd-geqp
            # "active only" filter in :func:`invalidate_for_user`
            # picks them up. An already-expired row is filtered out
            # (no point re-flagging a dead session); using
            # ``created_at`` as the expiry would produce a fake "zero
            # sessions to invalidate" result on a real recovery.
            s.add(
                AuthSession(
                    id=new_ulid(),
                    user_id=user.id,
                    workspace_id=workspace_id,
                    expires_at=user.created_at + timedelta(days=30),
                    last_seen_at=user.created_at,
                    created_at=user.created_at,
                )
            )
        s.commit()
        return user.id, workspace_id


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


class TestRecoveryFullFlow:
    """request → verify → passkey/start → passkey/finish via HTTP."""

    def test_happy_path_revokes_old_and_lands_new(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, _workspace_id = _seed_user_with_state(
            engine,
            email="happy@example.com",
            display_name="Happy User",
            passkey_count=2,
            session_count=3,
        )
        cred_id = _stub_passkey_verifier(monkeypatch)

        # 1. request — 202, magic link mailed.
        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "happy@example.com"},
        )
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "accepted"
        assert len(mailer.sent) == 1
        token = _extract_recovery_token(mailer.sent[0][2])

        # 2. verify — returns a recovery session handle.
        r = client.get(f"/api/v1/recover/passkey/verify?token={token}")
        assert r.status_code == 200, r.text
        recovery_session_id = r.json()["recovery_session_id"]
        assert len(recovery_session_id) == 26  # ULID

        # 3. passkey/start — returns the WebAuthn creation options.
        r = client.post(
            "/api/v1/recover/passkey/start",
            json={"recovery_session_id": recovery_session_id},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        challenge_id = body["challenge_id"]
        assert body["options"]
        # Recovery start skips excludeCredentials — the prior
        # credentials are about to be revoked.
        assert body["options"].get("excludeCredentials", []) == []

        # 4. passkey/finish — revokes old + lands new.
        r = client.post(
            "/api/v1/recover/passkey/finish",
            json={
                "recovery_session_id": recovery_session_id,
                "challenge_id": challenge_id,
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
        assert finish_body["user_id"] == user_id
        assert finish_body["revoked_credential_count"] == 2
        assert finish_body["revoked_session_count"] == 3
        assert finish_body["credential_id"]  # base64url of cred_id

        # 5. Assert final DB state.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            creds = s.scalars(
                select(PasskeyCredential).where(PasskeyCredential.user_id == user_id)
            ).all()
            assert len(creds) == 1
            assert creds[0].id == cred_id

            # Every prior web session INVALIDATED (cd-geqp) — rows
            # survive for forensics but carry ``invalidated_at`` and
            # ``invalidation_cause = "recovery_consumed"``.
            prior_sessions = s.scalars(
                select(AuthSession).where(AuthSession.user_id == user_id)
            ).all()
            assert len(prior_sessions) == 3
            for row in prior_sessions:
                assert row.invalidated_at is not None
                assert row.invalidation_cause == "recovery_consumed"

            # Audit trail: requested + verified + completed + passkey.registered.
            actions = {
                row.action
                for row in s.scalars(
                    select(AuditLog).where(
                        AuditLog.action.like("recovery.%")
                        | (AuditLog.action == "passkey.registered")
                    )
                ).all()
            }
            assert {
                "recovery.requested",
                "recovery.verified",
                "recovery.completed",
                "passkey.registered",
            } <= actions


class TestRecoveryEnumerationGuard:
    """Unknown-email request still returns 202 and writes audit."""

    def test_unknown_email_request_returns_202(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "noone@example.com"},
        )
        assert r.status_code == 202, r.text
        # Unknown-email template still sent.
        assert len(mailer.sent) == 1
        assert mailer.sent[0][0] == ("noone@example.com",)
        assert "https://" not in mailer.sent[0][2]  # no link

        # Audit row carries hit=False.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            row = s.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).one()
            assert isinstance(row.diff, dict)
            assert row.diff["hit"] is False


class TestRecoveryFinishReplay:
    """A replayed finish sees the consumed recovery session — 404."""

    def test_second_finish_returns_404(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_user_with_state(
            engine,
            email="replay@example.com",
            display_name="Replay",
            passkey_count=1,
            session_count=1,
        )
        _stub_passkey_verifier(monkeypatch)
        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "replay@example.com"},
        )
        assert r.status_code == 202
        token = _extract_recovery_token(mailer.sent[0][2])
        r = client.get(f"/api/v1/recover/passkey/verify?token={token}")
        recovery_session_id = r.json()["recovery_session_id"]
        r = client.post(
            "/api/v1/recover/passkey/start",
            json={"recovery_session_id": recovery_session_id},
        )
        challenge_id = r.json()["challenge_id"]
        r = client.post(
            "/api/v1/recover/passkey/finish",
            json={
                "recovery_session_id": recovery_session_id,
                "challenge_id": challenge_id,
                "credential": {
                    "id": "s",
                    "rawId": "s",
                    "response": {},
                    "type": "public-key",
                },
            },
        )
        assert r.status_code == 200
        # Second finish with same session → 404.
        replay = client.post(
            "/api/v1/recover/passkey/finish",
            json={
                "recovery_session_id": recovery_session_id,
                "challenge_id": challenge_id,
                "credential": {
                    "id": "s",
                    "rawId": "s",
                    "response": {},
                    "type": "public-key",
                },
            },
        )
        assert replay.status_code == 404
        assert replay.json()["detail"]["error"] == "recovery_session_not_found"


class TestRecoveryRateLimit:
    """Rate-limit trip returns 429 with ``Retry-After``."""

    def test_per_email_cap_returns_429(
        self,
        client: TestClient,
        engine: Engine,
    ) -> None:
        _seed_user_with_state(
            engine,
            email="rate@example.com",
            display_name="Rate",
            passkey_count=0,
            session_count=0,
        )
        for _ in range(3):
            r = client.post(
                "/api/v1/recover/passkey/request",
                json={"email": "rate@example.com"},
            )
            assert r.status_code == 202
        # 4th trips the 3/email/hour cap.
        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "rate@example.com"},
        )
        assert r.status_code == 429
        body = r.json()["detail"]
        assert body["error"] == "rate_limited"
        assert body["retry_after_seconds"] >= 1
        assert "Retry-After" in r.headers


class TestRecoveryInvalidToken:
    """Verify with a garbage token → 400 invalid_token."""

    def test_garbage_token_returns_400(
        self,
        client: TestClient,
    ) -> None:
        r = client.get("/api/v1/recover/passkey/verify?token=not-a-real-token")
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# Workspace kill-switch (auth.self_service_recovery_enabled)
# ---------------------------------------------------------------------------


def _seed_user_with_killswitch(
    engine: Engine,
    *,
    email: str,
    display_name: str,
    kill_switch: bool,
) -> str:
    """Seed a user with one grant in a workspace whose kill-switch state
    is pinned.

    Returns the user id. ``kill_switch=True`` writes the flag
    ``False`` into the workspace's ``settings_json`` (i.e. self-service
    recovery is *disabled* for anyone with a grant here). The dual
    meaning ("kill_switch=True → recovery disabled") is ugly but
    matches the test's narrative intent; the model column stays the
    spec's ``auth.self_service_recovery_enabled`` boolean.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        workspace_id = new_ulid()
        s.add(
            Workspace(
                id=workspace_id,
                slug=_workspace_slug(workspace_id),
                name="KillSwitch",
                plan="free",
                quota_json={},
                settings_json=(
                    {"auth.self_service_recovery_enabled": False} if kill_switch else {}
                ),
                created_at=user.created_at,
            )
        )
        s.flush()
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=user.created_at,
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


class TestRecoveryStepUp:
    """Spec §03 "Self-service lost-device recovery" step 3 + §15
    "Step-up bypass is not a fallback" — break-glass code gate over
    the HTTP surface.

    Three branches mirroring the unit tests but driven through the
    real router so the Pydantic schema, dispatch ordering, and audit
    persistence on a fresh UoW are exercised end-to-end.
    """

    def test_manager_no_code_returns_202_no_mail(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """A manager who omits the code → 202 (enumeration guard) +
        no mail + ``recovery.stepup_missing`` audit.
        """
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            user = bootstrap_user(s, email="m@example.com", display_name="M")
            workspace_id = new_ulid()
            s.add(
                Workspace(
                    id=workspace_id,
                    slug=_workspace_slug(workspace_id),
                    name="W",
                    plan="free",
                    quota_json={},
                    created_at=user.created_at,
                )
            )
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=user.created_at,
                    created_by_user_id=None,
                )
            )
            s.commit()
            user_id = user.id

        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "m@example.com"},
        )
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"
        # No mail leaves the host — step-up gate refused.
        assert mailer.sent == []

        with factory() as s:
            assert s.scalars(select(MagicLinkNonce)).all() == []
            stepup = s.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_missing")
            ).one()
            assert stepup.entity_id == user_id
            # No ``recovery.requested`` row — the step-up gate
            # short-circuits before the primary UoW audit.
            assert (
                s.scalars(
                    select(AuditLog).where(AuditLog.action == "recovery.requested")
                ).all()
                == []
            )

    def test_manager_valid_code_burns_and_mails(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """A manager with a valid code → mail + code burnt +
        ``consumed_magic_link_id`` stamped + audit ``stepup=True``.
        """
        from app.adapters.db.identity.models import BreakGlassCode
        from app.auth import break_glass as bg_module

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        plaintext = "valid-int-code"
        with factory() as s:
            user = bootstrap_user(s, email="mv@example.com", display_name="MV")
            workspace_id = new_ulid()
            s.add(
                Workspace(
                    id=workspace_id,
                    slug=_workspace_slug(workspace_id),
                    name="MV",
                    plan="free",
                    quota_json={},
                    created_at=user.created_at,
                )
            )
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=user.created_at,
                    created_by_user_id=None,
                )
            )
            code_id = new_ulid()
            s.add(
                BreakGlassCode(
                    id=code_id,
                    workspace_id=workspace_id,
                    user_id=user.id,
                    hash=bg_module._HASHER.hash(plaintext),
                    hash_params={
                        "time_cost": 3,
                        "memory_cost": 65536,
                        "parallelism": 4,
                    },
                    created_at=user.created_at,
                    used_at=None,
                    consumed_magic_link_id=None,
                )
            )
            s.commit()

        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "mv@example.com", "break_glass_code": plaintext},
        )
        assert r.status_code == 202
        # Mail sent (step-up gate cleared, normal hit branch).
        assert len(mailer.sent) == 1

        with factory() as s:
            nonce = s.scalars(select(MagicLinkNonce)).one()
            burnt = s.get(BreakGlassCode, code_id)
            assert burnt is not None
            assert burnt.used_at is not None
            assert burnt.consumed_magic_link_id == nonce.jti
            audit = s.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).one()
            assert isinstance(audit.diff, dict)
            assert audit.diff["hit"] is True
            assert audit.diff["stepup"] is True

    def test_worker_with_code_does_not_burn(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """Non-step-up worker with a code → code IGNORED, mail still sent.

        §03 step 4 — the code field is silently dropped for workers /
        clients / guests.
        """
        from app.adapters.db.identity.models import BreakGlassCode
        from app.auth import break_glass as bg_module

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        plaintext = "untouched-code"
        with factory() as s:
            user = bootstrap_user(s, email="wc@example.com", display_name="WC")
            workspace_id = new_ulid()
            s.add(
                Workspace(
                    id=workspace_id,
                    slug=_workspace_slug(workspace_id),
                    name="WC",
                    plan="free",
                    quota_json={},
                    created_at=user.created_at,
                )
            )
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=user.created_at,
                    created_by_user_id=None,
                )
            )
            code_id = new_ulid()
            s.add(
                BreakGlassCode(
                    id=code_id,
                    workspace_id=workspace_id,
                    user_id=user.id,
                    hash=bg_module._HASHER.hash(plaintext),
                    hash_params={
                        "time_cost": 3,
                        "memory_cost": 65536,
                        "parallelism": 4,
                    },
                    created_at=user.created_at,
                    used_at=None,
                    consumed_magic_link_id=None,
                )
            )
            s.commit()

        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "wc@example.com", "break_glass_code": plaintext},
        )
        assert r.status_code == 202
        # Mail sent — normal hit branch for non-step-up users.
        assert len(mailer.sent) == 1

        with factory() as s:
            row = s.get(BreakGlassCode, code_id)
            assert row is not None
            # Code untouched — non-step-up flow ignores it.
            assert row.used_at is None
            assert row.consumed_magic_link_id is None


class TestRecoveryKillSwitch:
    """Spec §03 "Workspace kill-switch" end-to-end."""

    def test_kill_switched_user_no_mail_and_audit_lands(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """A user with a grant in a kill-switched workspace sees 202,
        receives no mail, and an ``audit.recovery.disabled_by_workspace``
        row is written on a fresh UoW.
        """
        user_id = _seed_user_with_killswitch(
            engine,
            email="ks@example.com",
            display_name="KS",
            kill_switch=True,
        )

        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "ks@example.com"},
        )
        # Wire-identical to every other 202 — the enumeration guard
        # survives the kill-switch case.
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"
        # No mail, no magic-link nonce — the hit branch was skipped.
        assert mailer.sent == []
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            assert s.scalars(select(MagicLinkNonce)).all() == []
            # The disabled-by-workspace audit row landed.
            disabled = s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "recovery.disabled_by_workspace"
                )
            ).all()
            assert len(disabled) == 1
            row = disabled[0]
            assert row.entity_id == user_id
            assert isinstance(row.diff, dict)
            assert row.diff["reason"] == "workspace_kill_switch"
            # No ``recovery.requested`` row — the domain branch
            # short-circuits before the primary UoW audit.
            assert (
                s.scalars(
                    select(AuditLog).where(AuditLog.action == "recovery.requested")
                ).all()
                == []
            )

    def test_flag_true_everywhere_flows_through_happy_path(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """The explicit ``True`` case matches the catalog default — the
        mail lands, the nonce is written, the ``recovery.requested``
        audit fires."""
        _seed_user_with_killswitch(
            engine,
            email="on@example.com",
            display_name="On",
            kill_switch=False,
        )
        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "on@example.com"},
        )
        assert r.status_code == 202
        assert len(mailer.sent) == 1

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            assert len(s.scalars(select(MagicLinkNonce)).all()) == 1
            assert s.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).all()
            assert (
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "recovery.disabled_by_workspace"
                    )
                ).all()
                == []
            )

    def test_archived_grants_do_not_block(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """§03 "Workspace kill-switch": only non-archived grants feed
        the decision. v1's archival == hard-delete, so we seed the
        kill-switched grant and then delete it — the user must still
        reach the happy path via their surviving non-kill-switched
        grant. When cd-x1xh lands ``role_grant.revoked_at``, this
        test extends to soft-revoke semantics without changing the
        behavioural assertion.
        """
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            user = bootstrap_user(s, email="arch@example.com", display_name="Arch")
            ok_ws_id = new_ulid()
            s.add(
                Workspace(
                    id=ok_ws_id,
                    slug=f"ok-{ok_ws_id[:8].lower()}",
                    name="OK",
                    plan="free",
                    quota_json={},
                    settings_json={},
                    created_at=user.created_at,
                )
            )
            killed_ws_id = new_ulid()
            s.add(
                Workspace(
                    id=killed_ws_id,
                    slug=f"ko-{killed_ws_id[:8].lower()}",
                    name="Killed",
                    plan="free",
                    quota_json={},
                    settings_json={"auth.self_service_recovery_enabled": False},
                    created_at=user.created_at,
                )
            )
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ok_ws_id,
                    user_id=user.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=user.created_at,
                    created_by_user_id=None,
                )
            )
            killed_grant_id = new_ulid()
            s.add(
                RoleGrant(
                    id=killed_grant_id,
                    workspace_id=killed_ws_id,
                    user_id=user.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=user.created_at,
                    created_by_user_id=None,
                )
            )
            s.flush()
            # Archive == delete in v1.
            s.execute(delete(RoleGrant).where(RoleGrant.id == killed_grant_id))
            s.commit()

        r = client.post(
            "/api/v1/recover/passkey/request",
            json={"email": "arch@example.com"},
        )
        assert r.status_code == 202
        # Happy path — mail sent, no disabled audit.
        assert len(mailer.sent) == 1
        with factory() as s:
            assert len(s.scalars(select(MagicLinkNonce)).all()) == 1
            assert (
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "recovery.disabled_by_workspace"
                    )
                ).all()
                == []
            )

    def test_kill_switched_step_up_user_does_not_burn_code(
        self,
        client: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        """A step-up user who lands in a kill-switched workspace must
        NOT have their break-glass code burnt.

        Kill-switch fires *before* the step-up gate (recovery refused
        for an operator-disabled workspace), so the redeem walk
        never runs. Burning a captured code on a refused request
        would let an attacker grind a step-up user's code-set down
        to zero by repeatedly submitting against a kill-switched
        workspace — the §03 redemption ordering is what protects
        against it.
        """
        from app.adapters.db.identity.models import BreakGlassCode
        from app.auth import break_glass as bg_module

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        plaintext = "captured-code"
        with factory() as s:
            user = bootstrap_user(s, email="ksm@example.com", display_name="KSM")
            workspace_id = new_ulid()
            s.add(
                Workspace(
                    id=workspace_id,
                    slug=_workspace_slug(workspace_id),
                    name="KSManager",
                    plan="free",
                    quota_json={},
                    settings_json={
                        "auth.self_service_recovery_enabled": False,
                    },
                    created_at=user.created_at,
                )
            )
            s.flush()
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=user.created_at,
                    created_by_user_id=None,
                )
            )
            code_id = new_ulid()
            s.add(
                BreakGlassCode(
                    id=code_id,
                    workspace_id=workspace_id,
                    user_id=user.id,
                    hash=bg_module._HASHER.hash(plaintext),
                    hash_params={
                        "time_cost": 3,
                        "memory_cost": 65536,
                        "parallelism": 4,
                    },
                    created_at=user.created_at,
                    used_at=None,
                    consumed_magic_link_id=None,
                )
            )
            s.commit()

        r = client.post(
            "/api/v1/recover/passkey/request",
            json={
                "email": "ksm@example.com",
                "break_glass_code": plaintext,
            },
        )
        # Wire-identical 202; the kill-switch refused before the
        # step-up gate even classified the user.
        assert r.status_code == 202
        assert mailer.sent == []

        with factory() as s:
            row = s.get(BreakGlassCode, code_id)
            assert row is not None
            # Code untouched — the kill-switch branch returned before
            # the redeem walk.
            assert row.used_at is None
            assert row.consumed_magic_link_id is None
            # The kill-switch audit landed; no step-up audit.
            assert s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "recovery.disabled_by_workspace"
                )
            ).all()
            assert (
                s.scalars(
                    select(AuditLog).where(AuditLog.action.like("recovery.stepup_%"))
                ).all()
                == []
            )
