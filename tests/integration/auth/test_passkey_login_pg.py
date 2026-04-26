"""Integration tests for the passkey login router.

Exercises ``/auth/passkey/login/start`` + ``/auth/passkey/login/finish``
against a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``). The WebAuthn verifier is the single seam
we stub — real authenticators aren't available inside the test
harness — everything else runs through the same domain service and UoW
the production app uses.

Three coverage slices:

* **Happy path.** Pre-seeded user + passkey credential → ``start`` →
  ``finish`` → ``Set-Cookie: __Host-crewday_session=...`` header,
  ``user_id`` in the body, ``session`` row persisted, challenge row
  consumed, ``sign_count`` bumped, ``audit.passkey.assertion_ok``
  written.

* **Clone detection.** A pre-seeded passkey at ``sign_count=10``; the
  authenticator returns a counter of ``3``. ``finish`` → 401 with the
  same envelope as "unknown credential" (no fingerprint leak), the
  ``audit.passkey.cloned_detected`` row lands, the session is NOT
  issued.

* **Unknown credential.** ``finish`` with an id that doesn't resolve
  to any ``passkey_credential`` row → 401 with the same envelope; the
  session is NOT issued.

See ``docs/specs/03-auth-and-tokens.md`` §"Login",
``docs/specs/15-security-privacy.md`` §"Passkey specifics".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    CredentialDeviceType,
)

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import (
    ApiToken,
    PasskeyCredential,
    User,
    WebAuthnChallenge,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth.passkey import build_login_router
from app.auth import passkey as passkey_module
from app.auth._throttle import Throttle
from app.auth.webauthn import RelyingParty, VerifiedAuthentication
from app.config import Settings
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-passkey-login-root-key"),
        public_url="http://localhost:8000",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def rp() -> RelyingParty:
    return RelyingParty(
        rp_id="localhost",
        rp_name="crew.day",
        origin="http://localhost:8000",
        allowed_origins=("http://localhost:8000",),
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    throttle: Throttle,
    rp: RelyingParty,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the login router.

    Each HTTP request opens its own Session against ``engine``, commits
    on clean exit, rolls back on exception — matching the production
    UoW shape. We deliberately do NOT thread the conftest's savepoint
    ``db_session`` fixture through the router, because the integration
    test's whole point is to let each step *commit* so the next step
    sees the row.

    Patches :mod:`app.adapters.db.session`'s module-level default
    engine + sessionmaker so refusal-path audit rows (written on a
    fresh :func:`make_uow`) land on the same DB the test reads from —
    mirrors the same shim in
    :mod:`tests.integration.auth.test_signup_abuse_wired`.
    """
    import app.adapters.db.session as _session_mod

    monkeypatch.setattr(
        passkey_module,
        "make_relying_party",
        lambda settings=None: rp,
    )
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    router = build_login_router(throttle=throttle, settings=settings)
    app = FastAPI()
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

    app.dependency_overrides[db_session_dep] = _session

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
    # table. Strictly scoped: only the families this flow touches.
    with factory() as s:
        s.execute(delete(PasskeyCredential))
        s.execute(delete(SessionRow))
        s.execute(delete(ApiToken))
        s.execute(delete(WebAuthnChallenge))
        s.execute(delete(AuditLog))
        s.execute(delete(User))
        s.commit()


def _verified(new_sign_count: int) -> VerifiedAuthentication:
    return VerifiedAuthentication(
        credential_id=b"\x77" * 32,
        new_sign_count=new_sign_count,
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
        user_verified=True,
    )


def _raw_assertion(credential_id: bytes) -> dict[str, Any]:
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


def _seed_credential(engine: Engine, sign_count: int = 1) -> tuple[str, bytes]:
    """Seed a user + passkey credential; return (user_id, credential_id)."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        user = bootstrap_user(
            s,
            email=f"int-{new_ulid().lower()}@example.com",
            display_name="Int",
        )
        credential_id = b"\x77" * 32
        s.add(
            PasskeyCredential(
                id=credential_id,
                user_id=user.id,
                public_key=b"\x88" * 64,
                sign_count=sign_count,
                backup_eligible=False,
                created_at=_PINNED,
            )
        )
        s.commit()
        return user.id, credential_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoginFullFlowIntegration:
    """start → finish over the real HTTP router + real DB."""

    def test_happy_path_sets_cookie_and_persists_session(
        self,
        client: TestClient,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, credential_id = _seed_credential(engine, sign_count=1)

        start = client.post("/api/v1/auth/passkey/login/start")
        assert start.status_code == 200, start.text
        challenge_id = start.json()["challenge_id"]

        # Stub the WebAuthn verifier — the rest is real.
        monkeypatch.setattr(
            passkey_module,
            "verify_authentication",
            lambda **_: _verified(new_sign_count=5),
        )
        finish = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert finish.status_code == 200, finish.text
        assert finish.json()["user_id"] == user_id

        set_cookie = finish.headers.get("set-cookie", "")
        assert "__Host-crewday_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Lax" in set_cookie

        # Verify the row landed end-to-end.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            sessions = s.scalars(
                select(SessionRow).where(SessionRow.user_id == user_id)
            ).all()
            assert len(sessions) == 1
            assert sessions[0].user_id == user_id

            pk = s.scalars(select(PasskeyCredential)).one()
            assert pk.sign_count == 5
            assert pk.last_used_at is not None

            # Challenge row consumed.
            assert s.scalars(select(WebAuthnChallenge)).first() is None

            # Assertion audit row lands.
            actions = {
                a.action
                for a in s.scalars(
                    select(AuditLog).where(
                        AuditLog.diff["user_id"].as_string() == user_id
                    )
                ).all()
            }
            assert "passkey.assertion_ok" in actions
            assert "session.created" in actions

    def test_clone_detection_refuses_login(
        self,
        client: TestClient,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, credential_id = _seed_credential(engine, sign_count=10)

        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        # Authenticator returns a lower counter → clone.
        monkeypatch.setattr(
            passkey_module,
            "verify_authentication",
            lambda **_: _verified(new_sign_count=3),
        )
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(credential_id),
            },
        )
        assert resp.status_code == 401
        # SAME envelope as unknown-credential — no fingerprint leak.
        assert resp.json()["detail"]["error"] == "invalid_credential"
        # No Set-Cookie header on refusal.
        assert "set-cookie" not in {k.lower() for k in resp.headers}

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            # No session issued.
            assert (
                s.scalars(
                    select(SessionRow).where(SessionRow.user_id == user_id)
                ).first()
                is None
            )
            # The clone-detected audit row landed.
            credential_id_b64 = bytes_to_base64url(credential_id)
            audit_rows = [
                row
                for row in s.scalars(select(AuditLog)).all()
                if row.entity_id == credential_id_b64
                or (isinstance(row.diff, dict) and row.diff.get("user_id") == user_id)
            ]
            actions = {a.action for a in audit_rows}
            assert "passkey.cloned_detected" in actions
            assert "session.created" not in actions
            # cd-qx1f: challenge row burned on failure via fresh UoW.
            assert s.get(WebAuthnChallenge, challenge_id) is None
            # cd-cx19: credential row hard-deleted via fresh UoW.
            assert s.get(PasskeyCredential, credential_id) is None
            # cd-cx19: passkey.auto_revoked audit row written with
            # reason "clone_detected" alongside cloned_detected.
            assert "passkey.auto_revoked" in actions
            # §"Session-invalidation causes": the clone_detected
            # session-invalidation audit row survives — the
            # per-credential invalidate is a sibling seam to the
            # credential revoke and must not regress.
            assert "session.invalidated" in actions
            revoked_row = s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "passkey.auto_revoked",
                    AuditLog.diff["user_id"].as_string() == user_id,
                )
            ).one()
            # diff carries the expected shape.
            assert revoked_row.diff["reason"] == "clone_detected"
            assert revoked_row.diff["user_id"] == user_id

    def test_clone_detected_credential_cannot_be_replayed(
        self,
        client: TestClient,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After auto-revoke, a second login with the same credential
        returns the unknown-credential 401 shape — not CloneDetected.

        Verifies the §"Passkey specifics" invariant: the revoked row is
        gone, so the domain's credential lookup misses and
        :class:`InvalidLoginAttempt` fires (which the router collapses
        into the same 401 envelope). No second cloned_detected audit
        row is written — there's no credential left to detect a clone
        against.
        """
        user_id, credential_id = _seed_credential(engine, sign_count=10)

        # First attempt — clone detected, credential hard-deleted.
        start1 = client.post("/api/v1/auth/passkey/login/start")
        monkeypatch.setattr(
            passkey_module,
            "verify_authentication",
            lambda **_: _verified(new_sign_count=3),
        )
        first = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": start1.json()["challenge_id"],
                "credential": _raw_assertion(credential_id),
            },
        )
        assert first.status_code == 401
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            assert s.get(PasskeyCredential, credential_id) is None

        # Second attempt — credential is gone, so domain raises
        # InvalidLoginAttempt (unknown credential) rather than
        # CloneDetected. Same 401 wire shape either way.
        start2 = client.post("/api/v1/auth/passkey/login/start")
        monkeypatch.setattr(
            passkey_module,
            "verify_authentication",
            lambda **_: _verified(new_sign_count=99),
        )
        second = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": start2.json()["challenge_id"],
                "credential": _raw_assertion(credential_id),
            },
        )
        assert second.status_code == 401
        assert second.json()["detail"]["error"] == "invalid_credential"

        with factory() as s:
            # Exactly one cloned_detected audit row across both attempts
            # — the second attempt did NOT re-enter the CloneDetected
            # branch because the credential row was gone.
            cloned = s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "passkey.cloned_detected",
                    AuditLog.entity_id == bytes_to_base64url(credential_id),
                )
            ).all()
            assert len(cloned) == 1
            revoked = s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "passkey.auto_revoked",
                    AuditLog.diff["user_id"].as_string() == user_id,
                )
            ).all()
            assert len(revoked) == 1

    def test_unknown_credential_returns_401_no_session(
        self,
        client: TestClient,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Seed a user but no passkey credential.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            user = bootstrap_user(
                s,
                email=f"u-{new_ulid().lower()}@example.com",
                display_name="U",
            )
            user_id = user.id
            s.commit()

        start = client.post("/api/v1/auth/passkey/login/start")
        challenge_id = start.json()["challenge_id"]

        monkeypatch.setattr(
            passkey_module,
            "verify_authentication",
            lambda **_: _verified(new_sign_count=1),
        )
        resp = client.post(
            "/api/v1/auth/passkey/login/finish",
            json={
                "challenge_id": challenge_id,
                "credential": _raw_assertion(b"\xde" * 32),
            },
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "invalid_credential"

        with factory() as s:
            assert (
                s.scalars(
                    select(SessionRow).where(SessionRow.user_id == user_id)
                ).first()
                is None
            )
            # cd-qx1f: challenge burned even though no credential matched.
            assert s.get(WebAuthnChallenge, challenge_id) is None
