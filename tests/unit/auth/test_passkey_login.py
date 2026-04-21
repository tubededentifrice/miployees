"""Unit tests for :mod:`app.auth.passkey` login + the login router.

Exercises :func:`login_start`, :func:`login_finish`, and the
:func:`build_login_router` HTTP surface against an in-memory SQLite
engine with the schema built from ``Base.metadata``. The py_webauthn
authenticator verifier is monkeypatched so we don't need a real
device; the cases that matter here:

* ``login_start`` returns options + persists a login-sentinel challenge;
* ``login_finish`` happy path verifies, bumps ``sign_count`` +
  ``last_used_at``, issues a session, writes ``passkey.assertion_ok``;
* Clone detection on a ``sign_count`` rollback raises
  :class:`CloneDetected` and emits ``passkey.cloned_detected``;
* Sign-count = 0 skips the clone check (authenticator without counter);
* Unknown credential id → :class:`InvalidLoginAttempt`;
* Malformed credential id → :class:`InvalidLoginAttempt`;
* Challenge replay (consumed) → :class:`ChallengeNotFound`;
* Challenge expired → :class:`ChallengeExpired`;
* Challenge subject mismatch (signup challenge on login path) →
  :class:`ChallengeSubjectMismatch`;
* Lockout after 3 failures on the same IP / credential →
  :class:`PasskeyLoginLockout` on the 4th attempt.
* Router 401 / 429 shape identical for unknown credential / clone /
  rate-limited (no fingerprint leak);
* Router stamps ``Set-Cookie: __Host-crewday_session=...`` on success.
* ``has_owner_grant`` → shorter session TTL when the authenticating
  user is in ``owners@*`` on any workspace.

See ``docs/specs/03-auth-and-tokens.md`` §"Login", §"Sessions",
``docs/specs/15-security-privacy.md`` §"Passkey specifics".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.helpers.structs import (
    CredentialDeviceType,
)

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    PasskeyCredential,
    WebAuthnChallenge,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth.passkey import build_login_router
from app.auth import passkey as passkey_module
from app.auth._throttle import PasskeyLoginLockout, Throttle
from app.auth.passkey import (
    AuthenticationOptions,
    ChallengeExpired,
    ChallengeNotFound,
    ChallengeSubjectMismatch,
    CloneDetected,
    InvalidLoginAttempt,
    LoginResult,
    login_finish,
    login_start,
)
from app.auth.webauthn import RelyingParty, VerifiedAuthentication
from app.config import Settings
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def rp() -> RelyingParty:
    return RelyingParty(
        rp_id="localhost",
        rp_name="crew.day",
        origin="http://localhost:8000",
        allowed_origins=("http://localhost:8000",),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-passkey-login-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def pepper() -> bytes:
    """Deterministic pepper for tests — 32 bytes."""
    return b"\x01" * 32


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def seeded_credential(session: Session) -> tuple[str, bytes, bytes]:
    """Seed a user + a passkey credential; return (user_id, cred_id, public_key).

    No workspace is seeded — the default path tests the non-owner
    (30-day TTL) branch. Tests that need owner membership call
    :func:`_seed_owners_membership` explicitly.
    """
    user = bootstrap_user(
        session,
        email="login@example.com",
        display_name="Login Tester",
        clock=FrozenClock(_PINNED),
    )
    credential_id = b"\x77" * 32
    public_key = b"\x88" * 64
    session.add(
        PasskeyCredential(
            id=credential_id,
            user_id=user.id,
            public_key=public_key,
            sign_count=5,
            backup_eligible=False,
            created_at=_PINNED,
        )
    )
    session.commit()
    return user.id, credential_id, public_key


def _seed_owners_membership(session: Session, user_id: str) -> str:
    """Seed a workspace + owners-group membership for ``user_id``.

    Returns the workspace id so a test can assert ``has_owner_grant``
    flipped the session TTL.
    """
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Owner WS",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    ctx = WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=f"ws-{workspace_id[-6:].lower()}",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    from app.tenancy import tenant_agnostic

    with tenant_agnostic():
        seed_owners_system_group(
            session,
            ctx,
            workspace_id=workspace_id,
            owner_user_id=user_id,
            clock=FrozenClock(_PINNED),
        )
    session.commit()
    return workspace_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verified_authentication(
    *,
    credential_id: bytes = b"\x77" * 32,
    new_sign_count: int = 6,
    user_verified: bool = True,
) -> VerifiedAuthentication:
    """Build a py_webauthn :class:`VerifiedAuthentication` stub."""
    del credential_id  # unused — py_webauthn keeps it as opaque bytes
    return VerifiedAuthentication(
        credential_id=b"\x77" * 32,
        new_sign_count=new_sign_count,
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
        user_verified=user_verified,
    )


def _raw_assertion(credential_id: bytes = b"\x77" * 32) -> dict[str, Any]:
    """Minimal browser assertion shape the service passes to py_webauthn.

    The verifier is stubbed, so the payload only needs the fields the
    service reads before verification — ``id`` (base64url) is the
    only required one. Everything else is opaque to our domain layer.
    """
    from webauthn.helpers import bytes_to_base64url

    return {
        "id": bytes_to_base64url(credential_id),
        "rawId": bytes_to_base64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": "mock",
            "authenticatorData": "mock",
            "signature": "mock",
        },
    }


def _stub_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: VerifiedAuthentication,
) -> None:
    def _fake(**_: Any) -> VerifiedAuthentication:
        return verified

    monkeypatch.setattr(passkey_module, "verify_authentication", _fake)


def _stub_verify_raises(
    monkeypatch: pytest.MonkeyPatch, *, message: str = "bad signature"
) -> None:
    def _fake(**_: Any) -> VerifiedAuthentication:
        raise InvalidAuthenticationResponse(message)

    monkeypatch.setattr(passkey_module, "verify_authentication", _fake)


# ---------------------------------------------------------------------------
# login_start
# ---------------------------------------------------------------------------


class TestLoginStart:
    """``login_start`` mints options + persists the challenge row."""

    def test_returns_options_and_challenge_id(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        opts = login_start(session, rp=rp, clock=clock)

        assert isinstance(opts, AuthenticationOptions)
        assert len(opts.challenge_id) == 26  # ULID
        assert opts.options["rpId"] == "localhost"
        # Empty allowCredentials for conditional UI.
        assert opts.options.get("allowCredentials", []) == []
        assert opts.options["userVerification"] == "required"
        assert "challenge" in opts.options

    def test_persists_challenge_row_with_login_sentinel(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        opts = login_start(session, rp=rp, clock=clock)
        row = session.get(WebAuthnChallenge, opts.challenge_id)
        assert row is not None
        # Login subject: no user_id, signup_session_id == sentinel.
        assert row.user_id is None
        assert row.signup_session_id == "__login__"
        # TTL is 10 minutes like register.
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        assert expires_at - created_at == timedelta(minutes=10)


# ---------------------------------------------------------------------------
# login_finish — happy path + edge cases
# ---------------------------------------------------------------------------


class TestLoginFinishHappyPath:
    def test_verifies_bumps_counter_and_issues_session(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        user_id, credential_id, _ = seeded_credential
        # settings is read by session_module via get_settings; patch it.
        monkeypatch.setattr(
            "app.auth.session.get_settings",
            lambda: settings,
        )
        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=7))
        result = login_finish(
            session,
            challenge_id=opts.challenge_id,
            credential=_raw_assertion(credential_id),
            ip="203.0.113.1",
            ua="UA/Test",
            ip_hash_pepper=pepper,
            throttle=throttle,
            clock=clock,
            rp=rp,
        )
        assert isinstance(result, LoginResult)
        assert result.user_id == user_id

        # credential's sign_count + last_used_at were updated
        row = session.get(PasskeyCredential, credential_id)
        assert row is not None
        assert row.sign_count == 7
        assert row.last_used_at is not None

        # Challenge row was consumed.
        assert session.get(WebAuthnChallenge, opts.challenge_id) is None

        # Session row landed.
        sessions = session.scalars(select(SessionRow)).all()
        assert len(sessions) == 1
        assert sessions[0].user_id == user_id

        # Audit row landed.
        actions = {a.action for a in session.scalars(select(AuditLog)).all()}
        assert "passkey.assertion_ok" in actions
        assert "session.created" in actions

    def test_owner_session_ttl_shorter(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        user_id, credential_id, _ = seeded_credential
        _seed_owners_membership(session, user_id)
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=7))
        result = login_finish(
            session,
            challenge_id=opts.challenge_id,
            credential=_raw_assertion(credential_id),
            ip="203.0.113.1",
            ua="UA/Test",
            ip_hash_pepper=pepper,
            throttle=throttle,
            clock=clock,
            rp=rp,
        )
        # Owner TTL is 7 days (configured in fixture).
        expected = _PINNED + timedelta(days=7)
        expires_at = result.session_issue.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        assert expires_at == expected

    def test_non_owner_session_ttl_30_days(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=7))
        result = login_finish(
            session,
            challenge_id=opts.challenge_id,
            credential=_raw_assertion(credential_id),
            ip="203.0.113.1",
            ua="UA/Test",
            ip_hash_pepper=pepper,
            throttle=throttle,
            clock=clock,
            rp=rp,
        )
        expected = _PINNED + timedelta(days=30)
        expires_at = result.session_issue.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        assert expires_at == expected


class TestLoginFinishCloneDetection:
    def test_sign_count_regression_raises_clone_detected(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential  # sign_count seeded at 5
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        opts = login_start(session, rp=rp, clock=clock)
        # authenticator returns a counter <= stored
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=3))
        with pytest.raises(CloneDetected) as exc_info:
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(credential_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )
        # Exception carries the detection payload so the router can
        # emit the fresh-UoW audit without re-reading the DB.
        assert exc_info.value.old_sign_count == 5
        assert exc_info.value.new_sign_count == 3
        # No session issued on refusal.
        actions = {a.action for a in session.scalars(select(AuditLog)).all()}
        assert "session.created" not in actions

    def test_sign_count_equal_raises_clone_detected(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential  # stored = 5
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        opts = login_start(session, rp=rp, clock=clock)
        # authenticator returns same counter — not strictly increasing.
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=5))
        with pytest.raises(CloneDetected):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(credential_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )

    def test_zero_stored_counter_skips_clone_check(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An authenticator that doesn't implement the counter is legal."""
        user = bootstrap_user(
            session, email="z@example.com", display_name="Z", clock=clock
        )
        credential_id = b"\x99" * 32
        session.add(
            PasskeyCredential(
                id=credential_id,
                user_id=user.id,
                public_key=b"\xbb" * 64,
                sign_count=0,
                backup_eligible=False,
                created_at=_PINNED,
            )
        )
        session.commit()
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        opts = login_start(session, rp=rp, clock=clock)
        # counter stays at 0 — the check should not fire.
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=0))
        result = login_finish(
            session,
            challenge_id=opts.challenge_id,
            credential=_raw_assertion(credential_id),
            ip="203.0.113.1",
            ua="UA/Test",
            ip_hash_pepper=pepper,
            throttle=throttle,
            clock=clock,
            rp=rp,
        )
        assert isinstance(result, LoginResult)


class TestLoginFinishErrors:
    def test_unknown_credential_raises_invalid(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opts = login_start(session, rp=rp, clock=clock)
        # No credential seeded → unknown
        missing_id = b"\xee" * 32
        # Verifier isn't reached but patch anyway for isolation.
        _stub_verify(monkeypatch, verified=_verified_authentication())
        with pytest.raises(InvalidLoginAttempt):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(missing_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )

    def test_verify_failure_raises_invalid(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential
        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify_raises(monkeypatch)
        with pytest.raises(InvalidLoginAttempt):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(credential_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )

    def test_malformed_credential_id_raises_invalid(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
    ) -> None:
        opts = login_start(session, rp=rp, clock=clock)
        # "!" is not a legal base64url character; pad to keep the
        # base64 decoder raising rather than silently accepting.
        payload = {"id": "not_base64!!!", "type": "public-key", "response": {}}
        with pytest.raises(InvalidLoginAttempt):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=payload,
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )

    def test_challenge_replay_raises_challenge_not_found(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=7))
        # First finish consumes the challenge.
        login_finish(
            session,
            challenge_id=opts.challenge_id,
            credential=_raw_assertion(credential_id),
            ip="203.0.113.1",
            ua="UA/Test",
            ip_hash_pepper=pepper,
            throttle=throttle,
            clock=clock,
            rp=rp,
        )
        # Second finish with the same id 409s.
        with pytest.raises(ChallengeNotFound):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(credential_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )

    def test_challenge_expired_raises(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential
        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify(monkeypatch, verified=_verified_authentication())
        # Advance the clock well past the 10-minute TTL.
        late = FrozenClock(_PINNED + timedelta(minutes=11))
        with pytest.raises(ChallengeExpired):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(credential_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=late,
                rp=rp,
            )

    def test_non_login_challenge_rejected(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        """A signup challenge smuggled into login_finish is rejected."""
        user_id, credential_id, _ = seeded_credential
        # Seed a signup-shaped challenge row directly.
        challenge_id = new_ulid()
        session.add(
            WebAuthnChallenge(
                id=challenge_id,
                user_id=None,
                signup_session_id="01HWA00000000000000000SGN9",
                challenge=b"\x00" * 32,
                exclude_credentials=[],
                created_at=_PINNED,
                expires_at=_PINNED + timedelta(minutes=10),
            )
        )
        session.commit()

        _stub_verify(monkeypatch, verified=_verified_authentication())
        with pytest.raises(ChallengeSubjectMismatch):
            login_finish(
                session,
                challenge_id=challenge_id,
                credential=_raw_assertion(credential_id),
                ip="203.0.113.1",
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )
        del user_id


class TestLoginFinishThrottle:
    """Throttle lockout gate fires once the bucket is full."""

    def test_third_failure_locks_out_next_attempt(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        pepper: bytes,
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
        seeded_credential: tuple[str, bytes, bytes],
    ) -> None:
        _, credential_id, _ = seeded_credential
        # Prime the throttle with 3 failures from the same IP +
        # credential hash. The finish call on the 4th attempt should
        # short-circuit with PasskeyLoginLockout before reaching the
        # DB.
        from webauthn.helpers import bytes_to_base64url

        from app.auth._hashing import hash_with_pepper

        ip = "203.0.113.7"
        cred_b64 = bytes_to_base64url(credential_id)
        ip_hash = hash_with_pepper(ip, pepper)
        cred_hash = hash_with_pepper(cred_b64, pepper)
        for _ in range(3):
            throttle.record_passkey_login_failure(
                credential_id_hash=cred_hash,
                ip_hash=ip_hash,
                now=_PINNED,
            )

        opts = login_start(session, rp=rp, clock=clock)
        _stub_verify(monkeypatch, verified=_verified_authentication())
        with pytest.raises(PasskeyLoginLockout):
            login_finish(
                session,
                challenge_id=opts.challenge_id,
                credential=_raw_assertion(credential_id),
                ip=ip,
                ua="UA/Test",
                ip_hash_pepper=pepper,
                throttle=throttle,
                clock=clock,
                rp=rp,
            )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def login_app(
    factory: sessionmaker[Session],
    settings: Settings,
    throttle: Throttle,
    monkeypatch: pytest.MonkeyPatch,
    rp: RelyingParty,
) -> FastAPI:
    # Pin the relying party for deterministic options.
    monkeypatch.setattr(
        passkey_module,
        "make_relying_party",
        lambda settings=None: rp,
    )
    # Session module reads settings via get_settings.
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    router = build_login_router(throttle=throttle, settings=settings)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _session() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[db_session_dep] = _session
    return app


class TestLoginRouter:
    def test_start_returns_options(self, login_app: FastAPI) -> None:
        client = TestClient(login_app)
        resp = client.post("/api/v1/auth/passkey/login/start")
        assert resp.status_code == 200
        body = resp.json()
        assert "challenge_id" in body
        assert body["options"]["rpId"] == "localhost"

    def test_finish_happy_path_sets_cookie_and_returns_user_id(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Seed a credential row outside the TestClient's UoW.
        with factory() as s:
            user = bootstrap_user(s, email="router@example.com", display_name="Router")
            credential_id = b"\x77" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\x88" * 64,
                    sign_count=1,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()
            user_id = user.id

        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=5))
        finish = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert finish.status_code == 200, finish.text
        body = finish.json()
        assert body["user_id"] == user_id

        set_cookie = finish.headers.get("set-cookie", "")
        assert "__Host-crewday_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Lax" in set_cookie

    def test_finish_unknown_credential_returns_401(
        self,
        login_app: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        # No credential seeded — use a random id.
        _stub_verify(monkeypatch, verified=_verified_authentication())
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(b"\xde" * 32),
            },
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "invalid_credential"

    def test_finish_clone_returns_401(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with factory() as s:
            user = bootstrap_user(s, email="clone@example.com", display_name="Clone")
            credential_id = b"\xab" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\xcd" * 64,
                    sign_count=10,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()

        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        # counter regresses 10 → 3 → clone
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=3))
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert resp.status_code == 401
        # SAME envelope as unknown credential — no fingerprint leak.
        assert resp.json()["detail"]["error"] == "invalid_credential"

    def test_finish_rate_limited_returns_429(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with factory() as s:
            user = bootstrap_user(s, email="rl@example.com", display_name="RL")
            credential_id = b"\xbb" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\xcc" * 64,
                    sign_count=1,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()

        # Prime the lockout — credential-scope bucket + IP 'testclient'.
        from webauthn.helpers import bytes_to_base64url

        from app.auth._hashing import hash_with_pepper
        from app.auth.keys import derive_subkey

        settings = Settings.model_construct(
            database_url="sqlite:///:memory:",
            root_key=SecretStr("unit-test-passkey-login-root-key"),
            session_owner_ttl_days=7,
            session_user_ttl_days=30,
        )
        login_pepper = derive_subkey(
            settings.root_key, purpose="passkey-login-throttle"
        )
        # TestClient's default client.host is 'testclient'.
        ip_hash = hash_with_pepper("testclient", login_pepper)
        cred_b64 = bytes_to_base64url(credential_id)
        cred_hash = hash_with_pepper(cred_b64, login_pepper)
        for _ in range(3):
            throttle.record_passkey_login_failure(
                credential_id_hash=cred_hash,
                ip_hash=ip_hash,
                now=datetime.now(tz=UTC),
            )

        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        _stub_verify(monkeypatch, verified=_verified_authentication())
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"


# ---------------------------------------------------------------------------
# cd-qx1f — challenge row is single-use even on login-finish failure
# ---------------------------------------------------------------------------


@pytest.fixture
def redirect_default_engine(
    engine: Engine,
    factory: sessionmaker[Session],
) -> Iterator[None]:
    """Point :func:`app.adapters.db.session.make_uow` at the test engine.

    cd-qx1f's fresh-UoW challenge delete reads the module-level default
    sessionmaker. Without this redirect the fresh UoW opens against the
    production default DB, the broad ``except Exception`` swallows the
    cross-DB failure, and the assertion "challenge gone" reads from the
    test DB where nothing was deleted. Mirrors the shim in
    :mod:`tests.integration.auth.test_passkey_login_pg`.
    """
    import app.adapters.db.session as _session_mod

    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


class TestLoginFinishChallengeSingleUse:
    """cd-qx1f: login finish burns the challenge on every failure path.

    Exercises every raise shape that :func:`login_finish` emits:
    InvalidLoginAttempt (unknown credential, malformed id, verifier
    rejection), CloneDetected, ChallengeExpired, ChallengeSubjectMismatch.
    ChallengeNotFound is naturally idempotent (row already gone).
    PasskeyLoginLockout is NOT covered here because it short-circuits
    before any DB read — the challenge row is untouched and deliberately
    left intact so the caller can retry inside the 10-min TTL once the
    throttle bucket drains.
    """

    def test_unknown_credential_burns_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]
        with factory() as s:
            assert s.get(WebAuthnChallenge, challenge_id) is not None

        # No credential seeded — the finish resolves to InvalidLoginAttempt.
        _stub_verify(monkeypatch, verified=_verified_authentication())
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(b"\xde" * 32),
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, challenge_id) is None

    def test_verifier_rejection_burns_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """py_webauthn raises :class:`InvalidAuthenticationResponse`; the
        domain rewraps as :class:`InvalidLoginAttempt`."""
        with factory() as s:
            user = bootstrap_user(s, email="bad@example.com", display_name="Bad")
            credential_id = b"\x55" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\x66" * 64,
                    sign_count=1,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()

        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        _stub_verify_raises(monkeypatch, message="bad signature")
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, challenge_id) is None

    def test_malformed_credential_id_burns_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
    ) -> None:
        """A malformed base64url id → InvalidLoginAttempt inside the
        domain, before any DB work. The challenge row is still burned.
        """
        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": {"id": "not_base64!!!", "type": "public-key"},
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, challenge_id) is None

    def test_clone_detected_burns_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with factory() as s:
            user = bootstrap_user(s, email="clone2@example.com", display_name="C2")
            credential_id = b"\x33" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\x44" * 64,
                    sign_count=10,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()

        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        # Counter regresses 10 → 3 → CloneDetected.
        _stub_verify(monkeypatch, verified=_verified_authentication(new_sign_count=3))
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, challenge_id) is None
            # cd-cx19: the clone-detected credential is hard-deleted on
            # a sibling fresh UoW, and passkey.auto_revoked audit is
            # written with reason "clone_detected".
            assert s.get(PasskeyCredential, credential_id) is None
            actions = {a.action for a in s.scalars(select(AuditLog)).all()}
            assert "passkey.auto_revoked" in actions
            assert "passkey.cloned_detected" in actions

    def test_challenge_expired_burns_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
    ) -> None:
        """Seed an already-expired login challenge and post finish; the
        domain raises :class:`ChallengeExpired` and the row is burned.
        """
        stale_id = new_ulid()
        with factory() as s:
            s.add(
                WebAuthnChallenge(
                    id=stale_id,
                    user_id=None,
                    signup_session_id="__login__",
                    challenge=b"\x00" * 32,
                    exclude_credentials=[],
                    created_at=datetime(2020, 1, 1, tzinfo=UTC),
                    expires_at=datetime(2020, 1, 1, 0, 10, tzinfo=UTC),
                )
            )
            s.commit()
            assert s.get(WebAuthnChallenge, stale_id) is not None

        client = TestClient(login_app)
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": stale_id,
                "credential": _raw_assertion(b"\x77" * 32),
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, stale_id) is None

    def test_subject_mismatch_burns_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
    ) -> None:
        """A signup challenge smuggled into login_finish is refused and
        the row is burned so the attacker can't keep pounding it.
        """
        cid = new_ulid()
        # Use a far-future expiry so the real wall-clock stays inside
        # the TTL; we want to land on the subject check, not expiry.
        now = datetime.now(tz=UTC)
        with factory() as s:
            s.add(
                WebAuthnChallenge(
                    id=cid,
                    user_id=None,
                    # Not the login sentinel → subject mismatch.
                    signup_session_id="01HWA00000000000000000SGN2",
                    challenge=b"\x00" * 32,
                    exclude_credentials=[],
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            s.commit()

        client = TestClient(login_app)
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": cid,
                "credential": _raw_assertion(b"\x77" * 32),
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, cid) is None

    def test_unknown_challenge_is_idempotent(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
    ) -> None:
        """Concurrent-finish race: if a sibling call already consumed
        the challenge, the fresh-UoW delete is a no-op and the handler
        still returns 401 without crashing.
        """
        client = TestClient(login_app)
        fake_id = new_ulid()
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": fake_id,
                "credential": _raw_assertion(b"\x77" * 32),
            },
        )
        assert resp.status_code == 401
        with factory() as s:
            assert s.get(WebAuthnChallenge, fake_id) is None

    def test_rate_limit_preserves_challenge(
        self,
        login_app: FastAPI,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
        throttle: Throttle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PasskeyLoginLockout fires before any DB read — the challenge
        row stays intact so the caller can finish once the bucket drains.
        """
        with factory() as s:
            user = bootstrap_user(s, email="rlc@example.com", display_name="RLC")
            credential_id = b"\x22" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\x11" * 64,
                    sign_count=1,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()

        # Prime the lockout — same pepper/IP arithmetic as
        # test_finish_rate_limited_returns_429.
        from webauthn.helpers import bytes_to_base64url

        from app.auth._hashing import hash_with_pepper
        from app.auth.keys import derive_subkey

        settings = Settings.model_construct(
            database_url="sqlite:///:memory:",
            root_key=SecretStr("unit-test-passkey-login-root-key"),
            session_owner_ttl_days=7,
            session_user_ttl_days=30,
        )
        login_pepper = derive_subkey(
            settings.root_key, purpose="passkey-login-throttle"
        )
        ip_hash = hash_with_pepper("testclient", login_pepper)
        cred_b64 = bytes_to_base64url(credential_id)
        cred_hash = hash_with_pepper(cred_b64, login_pepper)
        for _ in range(3):
            throttle.record_passkey_login_failure(
                credential_id_hash=cred_hash,
                ip_hash=ip_hash,
                now=datetime.now(tz=UTC),
            )

        client = TestClient(login_app)
        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        _stub_verify(monkeypatch, verified=_verified_authentication())
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert resp.status_code == 429
        # Row stays — lockout is not a "verification failure" for
        # the purposes of §03 "single-use on failure".
        with factory() as s:
            assert s.get(WebAuthnChallenge, challenge_id) is not None


# ---------------------------------------------------------------------------
# cd-cx19 — clone-detected credential auto-revoke on a fresh UoW
# ---------------------------------------------------------------------------


class TestAutoRevokeCredentialFreshUow:
    """Exercises the auto-revoke helper's edge cases.

    The happy path is covered by
    :meth:`TestLoginFinishChallengeSingleUse.test_clone_detected_burns_challenge`
    (credential hard-deleted, ``passkey.auto_revoked`` audit lands).
    These cases cover the paths that test doesn't:

    * Concurrent auto-revoke — the credential is already gone when
      the helper opens its fresh UoW. It must return cleanly and must
      NOT emit a ``passkey.auto_revoked`` audit row (a no-op writing
      an audit row would falsely claim this call did the work).
    * DB failure — the fresh UoW raises. The helper swallows the
      error via ``except Exception`` so the 401 still lands; the
      absent audit row is fine (the operator-facing forensic trail
      still carries ``passkey.cloned_detected``).
    """

    def test_no_op_when_credential_already_gone_skips_audit(
        self,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
    ) -> None:
        """Direct unit test of :func:`_auto_revoke_credential_fresh_uow`
        when the credential row is already absent.

        The helper opens its own fresh UoW, reads the credential id,
        finds ``None``, logs at INFO, and returns — crucially WITHOUT
        emitting a ``passkey.auto_revoked`` audit row. Emitting an
        audit for a no-op would falsely claim this call did the work
        that the sibling caller (concurrent auto-revoke, race with
        :func:`revoke_passkey`, prior revocation) actually did.

        This goes straight at the helper rather than driving a full
        CloneDetected flow: the integration-level "credential gone"
        race is hard to trigger deterministically, and the unit-level
        question the review asks ("no-op does not emit audit") is
        answered more cleanly by the direct call.
        """
        from webauthn.helpers import bytes_to_base64url

        import app.api.v1.auth.passkey as passkey_api_module

        # An id that is well-formed base64url but has no row seeded —
        # the helper's ``session.get`` must return None and the helper
        # must short-circuit without an audit write.
        absent_credential_id = b"\xcc" * 32
        absent_b64 = bytes_to_base64url(absent_credential_id)

        # Pre-condition: the row really is absent.
        with factory() as s:
            assert s.get(PasskeyCredential, absent_credential_id) is None

        # The helper is a module-private function — we call it directly
        # (underscored-name is an explicit "this is implementation, the
        # test is intentionally coupled to it").
        passkey_api_module._auto_revoke_credential_fresh_uow(
            credential_id_b64=absent_b64,
            reason="clone_detected",
        )

        with factory() as s:
            actions = [a.action for a in s.scalars(select(AuditLog)).all()]
            # No audit row was written — the helper's no-op path is
            # correctly silent. Any ``passkey.auto_revoked`` here
            # would be a false claim that the helper revoked a
            # credential when the row was never present.
            assert "passkey.auto_revoked" not in actions
            # And no other rows landed either — the helper only
            # writes one kind of audit.
            assert actions == []

    def test_db_failure_is_swallowed_without_raising(
        self,
        factory: sessionmaker[Session],
        redirect_default_engine: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """A ``make_uow`` failure inside the helper is logged and
        swallowed — the helper returns without propagating the error.

        A raise here would shadow the router's intended 401 with a
        500, defeating the entire fresh-UoW-rescue pattern §15 asks
        for. The broad ``except Exception`` is the point; this test
        pins that it stays broad.
        """
        import logging

        from webauthn.helpers import bytes_to_base64url

        import app.api.v1.auth.passkey as passkey_api_module

        with factory() as s:
            user = bootstrap_user(s, email="boom@example.com", display_name="Boom")
            credential_id = b"\xee" * 32
            s.add(
                PasskeyCredential(
                    id=credential_id,
                    user_id=user.id,
                    public_key=b"\xff" * 64,
                    sign_count=10,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
            s.commit()

        cred_b64 = bytes_to_base64url(credential_id)

        def _raiser() -> Any:
            raise RuntimeError("simulated DB failure")

        monkeypatch.setattr(passkey_api_module, "make_uow", _raiser)

        # alembic's fileConfig in migrations/env.py disables propagation
        # on every non-listed logger; re-enable via the shared fixture
        # so caplog actually sees the ERROR record.
        allow_propagated_log_capture(passkey_api_module.__name__)

        # The helper must NOT propagate — any raise would collapse the
        # 401 into a 500 at the router layer.
        with caplog.at_level(logging.ERROR, logger=passkey_api_module.__name__):
            passkey_api_module._auto_revoke_credential_fresh_uow(
                credential_id_b64=cred_b64,
                reason="clone_detected",
            )

        # The failure was logged with the credential id so an operator
        # investigating a missing ``passkey.auto_revoked`` audit row
        # can correlate back to the source event.
        assert any(cred_b64 in rec.getMessage() for rec in caplog.records)

        with factory() as s:
            # No audit row was written (the fresh UoW never opened),
            # and the credential row stays put (the helper never got
            # past the make_uow call).
            actions = [a.action for a in s.scalars(select(AuditLog)).all()]
            assert "passkey.auto_revoked" not in actions
            assert s.get(PasskeyCredential, credential_id) is not None
