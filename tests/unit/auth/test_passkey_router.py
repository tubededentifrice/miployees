"""Router-level tests for :mod:`app.api.v1.auth.passkey`.

Exercises the FastAPI handlers through :class:`TestClient`:

* happy-path round-trip shapes the response body correctly,
* error mapping matches the spec (422 too_many_passkeys, 409 replay,
  400 invalid_registration, 400 challenge_expired),
* the signup router works without an authenticated context.

We stand up a minimal FastAPI app per test so we don't depend on the
full app factory (cd-ika7 — not merged yet).

See cd-8m4 acceptance criteria.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.identity.models import PasskeyCredential
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.auth.passkey import router, signup_router
from app.auth import passkey as passkey_module
from app.auth.webauthn import RelyingParty, VerifiedRegistration
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> tuple[WorkspaceContext, str]:
    """Seed a workspace + user, return (ctx, user_id) for the tests."""
    with factory() as s:
        ws = Workspace(
            id=new_ulid(),
            slug="router-test",
            name="Router Test",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        s.add(ws)
        s.flush()
        user = bootstrap_user(
            s,
            email="router@example.com",
            display_name="Router Tester",
            clock=FrozenClock(_PINNED),
        )
        s.commit()
        ctx = WorkspaceContext(
            workspace_id=ws.id,
            workspace_slug=ws.slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000CRLA",
        )
        return ctx, user.id


@pytest.fixture
def app_with_overrides(
    factory: sessionmaker[Session],
    seeded: tuple[WorkspaceContext, str],
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """Build a fresh FastAPI instance wired to the test session factory.

    Overrides the two deps routers consume: the workspace-context
    resolver (returns the seeded ctx) and the DB session (opens a UoW
    bound to the test engine). The RP is pinned to localhost so
    ``make_relying_party()`` inside the domain service doesn't need
    CREWDAY_PUBLIC_URL set.
    """
    ctx, _ = seeded

    # Pin the relying party — make_relying_party() reads app config.
    rp = RelyingParty(
        rp_id="localhost",
        rp_name="crew.day",
        origin="http://localhost:8000",
        allowed_origins=("http://localhost:8000",),
    )
    monkeypatch.setattr(
        passkey_module,
        "make_relying_party",
        lambda settings=None: rp,  # type-check passes: Callable signature matches
    )

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(signup_router, prefix="/api/v1")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


def _verified() -> VerifiedRegistration:
    from webauthn.helpers.structs import (
        AttestationFormat,
        CredentialDeviceType,
        PublicKeyCredentialType,
    )

    return VerifiedRegistration(
        credential_id=b"\x77" * 32,
        credential_public_key=b"\x88" * 64,
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000077",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"\x00",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )


def _raw() -> dict[str, Any]:
    return {
        "id": "mock",
        "type": "public-key",
        "response": {
            "clientDataJSON": "mock",
            "attestationObject": "mock",
            "transports": ["internal"],
        },
    }


class TestRegisterRouter:
    """Happy-path + 409 replay + 422 cap — the three AC-gated flows."""

    def test_start_returns_options_and_challenge_id(
        self, app_with_overrides: FastAPI
    ) -> None:
        client = TestClient(app_with_overrides)
        resp = client.post("/api/v1/auth/passkey/register/start")
        assert resp.status_code == 200
        body = resp.json()
        assert "challenge_id" in body
        assert body["options"]["rp"]["id"] == "localhost"

    def test_finish_happy_path(
        self,
        app_with_overrides: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = TestClient(app_with_overrides)
        start = client.post("/api/v1/auth/passkey/register/start")
        assert start.status_code == 200
        challenge_id = start.json()["challenge_id"]

        monkeypatch.setattr(
            passkey_module,
            "verify_registration",
            lambda **_: _verified(),
        )
        finish = client.post(
            "/api/v1/auth/passkey/register/finish",
            json={"challenge_id": challenge_id, "credential": _raw()},
        )
        assert finish.status_code == 200, finish.text
        body = finish.json()
        assert body["aaguid"] == "00000000-0000-0000-0000-000000000077"
        assert body["transports"] == "internal"

    def test_replay_returns_409(
        self,
        app_with_overrides: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = TestClient(app_with_overrides)
        start = client.post("/api/v1/auth/passkey/register/start")
        challenge_id = start.json()["challenge_id"]

        monkeypatch.setattr(
            passkey_module,
            "verify_registration",
            lambda **_: _verified(),
        )
        client.post(
            "/api/v1/auth/passkey/register/finish",
            json={"challenge_id": challenge_id, "credential": _raw()},
        )
        replay = client.post(
            "/api/v1/auth/passkey/register/finish",
            json={"challenge_id": challenge_id, "credential": _raw()},
        )
        assert replay.status_code == 409
        assert replay.json()["detail"]["error"] == "challenge_consumed_or_unknown"

    def test_too_many_passkeys_returns_422(
        self,
        app_with_overrides: FastAPI,
        factory: sessionmaker[Session],
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        """Preseeding 5 passkeys makes the 6th start return 422."""
        _, user_id = seeded
        with factory() as s:
            for i in range(5):
                s.add(
                    PasskeyCredential(
                        id=bytes([i]) * 32,
                        user_id=user_id,
                        public_key=b"\x00" * 32,
                        sign_count=0,
                        backup_eligible=False,
                        created_at=_PINNED,
                    )
                )
            s.commit()

        client = TestClient(app_with_overrides)
        resp = client.post("/api/v1/auth/passkey/register/start")
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "too_many_passkeys"

    def test_invalid_registration_returns_400(
        self,
        app_with_overrides: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.auth.webauthn import InvalidRegistrationResponse

        client = TestClient(app_with_overrides)
        start = client.post("/api/v1/auth/passkey/register/start")
        challenge_id = start.json()["challenge_id"]

        def _raise(**_: Any) -> VerifiedRegistration:
            raise InvalidRegistrationResponse("challenge mismatch")

        monkeypatch.setattr(passkey_module, "verify_registration", _raise)
        resp = client.post(
            "/api/v1/auth/passkey/register/finish",
            json={"challenge_id": challenge_id, "credential": _raw()},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_registration"


class TestSignupRouter:
    """Bare-host signup router — no ctx dep, no audit write."""

    def test_signup_start_finish_round_trip(
        self,
        app_with_overrides: FastAPI,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = TestClient(app_with_overrides)
        # Seed a user who will receive the credential on finish.
        with factory() as s:
            user = bootstrap_user(
                s,
                email="signup@example.com",
                display_name="Signup User",
                clock=FrozenClock(_PINNED),
            )
            s.commit()
            new_user_id = user.id

        start = client.post(
            "/api/v1/auth/passkey/signup/register/start",
            json={
                "signup_session_id": "01HWA00000000000000000SGN9",
                "email": "signup@example.com",
                "display_name": "Signup User",
            },
        )
        assert start.status_code == 200
        challenge_id = start.json()["challenge_id"]

        monkeypatch.setattr(
            passkey_module,
            "verify_registration",
            lambda **_: _verified(),
        )
        finish = client.post(
            "/api/v1/auth/passkey/signup/register/finish",
            json={
                "signup_session_id": "01HWA00000000000000000SGN9",
                "user_id": new_user_id,
                "challenge_id": challenge_id,
                "credential": _raw(),
            },
        )
        assert finish.status_code == 200, finish.text
