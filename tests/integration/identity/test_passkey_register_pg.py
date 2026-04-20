"""Integration tests for :mod:`app.auth.passkey` against a real DB.

Exercises the happy path + replay 409 on both backends (SQLite by
default; Postgres when ``CREWDAY_TEST_DB=postgres``). The goal is to
make sure the ``webauthn_challenge`` migration lands portably and
that the service's ``tenant_agnostic`` gates work under the ORM
tenant filter installed on the session.

The py_webauthn attestation verifier is monkeypatched — we test the
service seam, not the cryptographic layer (covered by unit tests on
the RP module and, in the future, Playwright end-to-end runs).

See cd-8m4 acceptance criteria and
``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import PasskeyCredential, WebAuthnChallenge
from app.auth import passkey as passkey_module
from app.auth.passkey import (
    ChallengeNotFound,
    register_finish,
    register_start,
)
from app.auth.webauthn import RelyingParty, VerifiedRegistration
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register the workspace-scoped tables this test module depends on.

    Same concern as the other identity integration modules: a sibling
    unit test resets the process-wide tenancy registry; we re-register
    to guarantee the tenant filter engages for the ``audit_log``
    writes in this module's finish flow.
    """
    registry.register("audit_log")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("role_grant")


_SLUG_COUNTER = 0


def _next_slug() -> str:
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"pk-int-{_SLUG_COUNTER:05d}"


@pytest.fixture
def rp() -> RelyingParty:
    return RelyingParty(
        rp_id="localhost",
        rp_name="crew.day",
        origin="http://localhost:8000",
        allowed_origins=("http://localhost:8000",),
    )


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext]]:
    """Yield ``(session, ctx)`` with a bootstrapped user + workspace.

    Mirrors the ``env`` fixture in the sibling identity integration
    tests: install the tenant filter on the session, bootstrap a
    user / workspace, pin the context.
    """
    install_tenant_filter(db_session)

    clock = FrozenClock(_PINNED)
    slug = _next_slug()
    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"User {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = WorkspaceContext(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )
    token = set_current(ctx)
    try:
        yield db_session, ctx
    finally:
        reset_current(token)


def _verified_response(
    *,
    credential_id: bytes = b"\x11" * 32,
    public_key: bytes = b"\x22" * 64,
    sign_count: int = 0,
    aaguid: str = "00000000-0000-0000-0000-000000000001",
) -> VerifiedRegistration:
    from webauthn.helpers.structs import (
        AttestationFormat,
        CredentialDeviceType,
        PublicKeyCredentialType,
    )

    return VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=public_key,
        sign_count=sign_count,
        aaguid=aaguid,
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"\x00",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )


def _raw_credential() -> dict[str, Any]:
    return {
        "id": "mock",
        "type": "public-key",
        "response": {
            "clientDataJSON": "mock",
            "attestationObject": "mock",
            "transports": ["internal"],
        },
    }


def _stub_verify(
    monkeypatch: pytest.MonkeyPatch, *, verified: VerifiedRegistration
) -> None:
    def _fake(
        *,
        rp: RelyingParty,
        credential: dict[str, Any],
        expected_challenge: bytes,
    ) -> VerifiedRegistration:
        del rp, credential, expected_challenge
        return verified

    monkeypatch.setattr(passkey_module, "verify_registration", _fake)


class TestHappyPathOnRealBackend:
    """Start → finish writes credential + audit, consumes challenge."""

    def test_end_to_end(
        self,
        env: tuple[Session, WorkspaceContext],
        rp: RelyingParty,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        opts = register_start(
            ctx,
            session,
            user_id=ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        # Challenge persisted at the backend's precision.
        stashed = session.get(WebAuthnChallenge, opts.challenge_id)
        assert stashed is not None
        assert stashed.user_id == ctx.actor_id

        verified = _verified_response()
        _stub_verify(monkeypatch, verified=verified)

        ref = register_finish(
            ctx,
            session,
            user_id=ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        # Credential row lives on ``passkey_credential`` and carries
        # the verified bytes.
        row = session.get(PasskeyCredential, verified.credential_id)
        assert row is not None
        assert row.public_key == verified.credential_public_key

        # Audit row in the same transaction — workspace + actor fields
        # copied from ctx.
        audit_rows = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == ref.credential_id_b64url,
                AuditLog.workspace_id == ctx.workspace_id,
            )
        ).all()
        assert len(audit_rows) == 1
        assert audit_rows[0].action == "passkey.registered"
        assert audit_rows[0].actor_id == ctx.actor_id

        # Challenge consumed.
        assert session.get(WebAuthnChallenge, opts.challenge_id) is None


class TestReplayReturnsConflict:
    """AC #5: a replayed finish returns 409 (ChallengeNotFound)."""

    def test_replay_raises_not_found(
        self,
        env: tuple[Session, WorkspaceContext],
        rp: RelyingParty,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        opts = register_start(
            ctx,
            session,
            user_id=ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        _stub_verify(
            monkeypatch,
            verified=_verified_response(credential_id=b"\xab" * 32),
        )
        register_finish(
            ctx,
            session,
            user_id=ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        # Second call with the same challenge_id — the row is gone.
        with pytest.raises(ChallengeNotFound):
            register_finish(
                ctx,
                session,
                user_id=ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )


class TestMigrationShape:
    """The ``webauthn_challenge`` migration landed with the expected shape."""

    def test_table_exists_after_migration(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Simple smoke: the alembic upgrade landed the table so the
        happy-path can insert. If the migration isn't applied this
        whole module would ImportError earlier, but pinning it here
        means a future drop of the migration bit fails loudly."""
        session, _ = env
        # A query against the model class works iff the table exists.
        session.scalars(select(WebAuthnChallenge)).all()
