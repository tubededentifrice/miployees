"""Unit tests for :mod:`app.auth.passkey` — registration ceremony.

The tests exercise the domain service against an in-memory SQLite
engine with the schema created from ``Base.metadata`` (no Alembic
— migration-level integration coverage lives in
``tests/integration/identity/test_passkey_register_pg.py``). The
py_webauthn attestation verifier is monkeypatched so we don't need a
real authenticator; the cases that matter here are:

* the shape of the options the service hands back on start,
* the audit + credential row it writes on finish,
* per-user cap (5), replay (409), expired challenge, subject
  mismatch, and py_webauthn error rewrapping.

See ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics" and
cd-8m4 acceptance criteria.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    PasskeyCredential,
    WebAuthnChallenge,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.auth import passkey as passkey_module
from app.auth.passkey import (
    ChallengeAlreadyConsumed,
    ChallengeExpired,
    ChallengeNotFound,
    ChallengeSubjectMismatch,
    InvalidRegistration,
    TooManyPasskeys,
    register_finish,
    register_finish_signup,
    register_start,
    register_start_signup,
)
from app.auth.webauthn import (
    InvalidRegistrationResponse,
    RelyingParty,
    VerifiedRegistration,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite, schema from ``Base.metadata``.

    No Alembic — the service is exercised against the ORM models. The
    parity harness in ``tests/integration/test_schema_parity.py``
    keeps model + migration in sync.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session; no tenant filter installed so the service's
    own ``tenant_agnostic`` gates are the only mechanism under test."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def workspace(session: Session) -> Workspace:
    """Minimal :class:`Workspace` row so the ``audit_log.workspace_id``
    FK on a successful finish has a valid parent."""
    ws = Workspace(
        id=new_ulid(),
        slug="pk-test",
        name="Passkey Test",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


@pytest.fixture
def ctx(workspace: Workspace) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id="placeholder-replaced-per-test",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


@pytest.fixture
def user_ctx(session: Session, ctx: WorkspaceContext) -> WorkspaceContext:
    """Bootstrap a user and bind it into ``ctx.actor_id``."""
    user = bootstrap_user(
        session,
        email="passkey@example.com",
        display_name="Passkey Tester",
        clock=FrozenClock(_PINNED),
    )
    # WorkspaceContext is frozen; recreate with the user's id.
    return WorkspaceContext(
        workspace_id=ctx.workspace_id,
        workspace_slug=ctx.workspace_slug,
        actor_id=user.id,
        actor_kind=ctx.actor_kind,
        actor_grant_role=ctx.actor_grant_role,
        actor_was_owner_member=ctx.actor_was_owner_member,
        audit_correlation_id=ctx.audit_correlation_id,
    )


@pytest.fixture
def rp() -> RelyingParty:
    """Fixed relying party for the tests — no env lookup, no boot path."""
    return RelyingParty(
        rp_id="localhost",
        rp_name="crew.day",
        origin="http://localhost:8000",
        allowed_origins=("http://localhost:8000",),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _verified_response(
    *,
    credential_id: bytes = b"\xaa" * 32,
    public_key: bytes = b"\xbb" * 64,
    sign_count: int = 0,
    aaguid: str = "00000000-0000-0000-0000-000000000000",
    backed_up: bool = False,
) -> VerifiedRegistration:
    """Build a py_webauthn :class:`VerifiedRegistration` stub.

    All fields the service reads have real values; the rest get
    library-shaped placeholders so the dataclass construction
    succeeds on any py_webauthn version matching our pin.
    """
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
        credential_backed_up=backed_up,
    )


def _raw_credential() -> dict[str, Any]:
    """Minimal raw credential body the service passes to py_webauthn.

    Transport hints come straight off ``response.transports`` per
    WebAuthn Level 3; the verifier itself is stubbed so the payload
    need only contain the fields the service reads.
    """
    return {
        "id": "mock",
        "type": "public-key",
        "response": {
            "clientDataJSON": "mock",
            "attestationObject": "mock",
            "transports": ["internal", "hybrid"],
        },
    }


def _stub_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: VerifiedRegistration,
) -> list[dict[str, Any]]:
    """Patch :func:`app.auth.passkey.verify_registration` with a stub.

    Returns a list that accumulates each invocation's kwargs so tests
    can assert the service passed the right challenge / rp through.
    """
    calls: list[dict[str, Any]] = []

    def _fake(
        *,
        rp: RelyingParty,
        credential: dict[str, Any],
        expected_challenge: bytes,
    ) -> VerifiedRegistration:
        calls.append(
            {
                "rp": rp,
                "credential": credential,
                "expected_challenge": expected_challenge,
            }
        )
        return verified

    monkeypatch.setattr(passkey_module, "verify_registration", _fake)
    return calls


def _stub_verify_raises(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: str = "challenge mismatch",
) -> None:
    """Patch verify to raise py_webauthn's concrete rejection type."""

    def _fake(**_: Any) -> VerifiedRegistration:
        raise InvalidRegistrationResponse(message)

    monkeypatch.setattr(passkey_module, "verify_registration", _fake)


# ---------------------------------------------------------------------------
# register_start
# ---------------------------------------------------------------------------


class TestRegisterStart:
    """``register_start`` mints options + persists the challenge row."""

    def test_returns_options_and_challenge_id(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )

        # ``challenge_id`` is a 26-char ULID.
        assert len(opts.challenge_id) == 26
        # The options dict is parsed JSON with the spec fields.
        assert opts.options["rp"]["id"] == "localhost"
        assert opts.options["rp"]["name"] == "crew.day"
        assert opts.options["user"]["name"] == "passkey@example.com"
        assert opts.options["user"]["displayName"] == "Passkey Tester"
        assert "challenge" in opts.options
        assert opts.options["attestation"] == "none"
        # ES256 (-7) + RS256 (-257) are in pubKeyCredParams.
        algs = [p["alg"] for p in opts.options["pubKeyCredParams"]]
        assert -7 in algs
        assert -257 in algs

    def test_persists_challenge_row(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        row = session.get(WebAuthnChallenge, opts.challenge_id)
        assert row is not None
        assert row.user_id == user_ctx.actor_id
        assert row.signup_session_id is None
        # ``expires_at`` is 10 minutes after ``created_at``.
        assert row.expires_at - row.created_at == timedelta(minutes=10)
        # ``exclude_credentials`` empty for a fresh user.
        assert row.exclude_credentials == []

    def test_exclude_credentials_populated_from_existing(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """Existing passkeys flow into ``excludeCredentials``."""
        # Seed one existing passkey for the user.
        session.add(
            PasskeyCredential(
                id=b"\xde\xad\xbe\xef" + b"\x00" * 28,
                user_id=user_ctx.actor_id,
                public_key=b"\xaa" * 32,
                sign_count=1,
                backup_eligible=False,
                created_at=_PINNED,
            )
        )
        session.flush()

        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        row = session.get(WebAuthnChallenge, opts.challenge_id)
        assert row is not None
        assert len(row.exclude_credentials) == 1
        # ``excludeCredentials`` in the browser payload mirrors the
        # server-side list.
        assert len(opts.options["excludeCredentials"]) == 1

    def test_fifth_passkey_is_allowed(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """4 existing passkeys — a 5th start is still allowed."""
        for i in range(4):
            session.add(
                PasskeyCredential(
                    id=bytes([i]) * 32,
                    user_id=user_ctx.actor_id,
                    public_key=b"\x00" * 32,
                    sign_count=0,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
        session.flush()

        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        assert len(opts.options["excludeCredentials"]) == 4

    def test_too_many_passkeys_rejected_on_start(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """5 existing → a 6th ``start`` raises :class:`TooManyPasskeys`.

        Failing early on start saves the user a tap on their
        authenticator — the same cap is re-checked on finish to
        close the concurrent-enrolment race.
        """
        for i in range(5):
            session.add(
                PasskeyCredential(
                    id=bytes([i]) * 32,
                    user_id=user_ctx.actor_id,
                    public_key=b"\x00" * 32,
                    sign_count=0,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
        session.flush()

        with pytest.raises(TooManyPasskeys):
            register_start(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                clock=clock,
                rp=rp,
            )

    def test_unknown_user_raises_lookup(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """An unknown user id raises ``LookupError`` (not a DB error)."""
        with pytest.raises(LookupError):
            register_start(
                user_ctx,
                session,
                user_id="01HWA00000000000000000NONE",
                clock=clock,
                rp=rp,
            )


# ---------------------------------------------------------------------------
# register_finish — happy path + edge cases
# ---------------------------------------------------------------------------


class TestRegisterFinishHappyPath:
    """A verified finish writes one credential + one audit row, then
    deletes the challenge."""

    def test_writes_credential_row(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        verified = _verified_response()
        _stub_verify(monkeypatch, verified=verified)

        ref = register_finish(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )

        assert ref.user_id == user_ctx.actor_id
        assert ref.transports == "internal,hybrid"
        assert ref.sign_count == 0
        # Row landed with the verified bytes.
        row = session.get(PasskeyCredential, verified.credential_id)
        assert row is not None
        assert row.public_key == verified.credential_public_key
        assert row.transports == "internal,hybrid"
        assert row.backup_eligible is False

    def test_writes_audit_row(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        verified = _verified_response()
        _stub_verify(monkeypatch, verified=verified)

        ref = register_finish(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )

        rows = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == ref.credential_id_b64url)
        ).all()
        assert len(rows) == 1
        audit = rows[0]
        assert audit.entity_kind == "passkey_credential"
        assert audit.action == "passkey.registered"
        assert audit.actor_id == user_ctx.actor_id
        assert audit.workspace_id == user_ctx.workspace_id
        assert audit.diff == {
            "user_id": user_ctx.actor_id,
            "aaguid": verified.aaguid,
            "transports": "internal,hybrid",
            "backup_eligible": False,
        }

    def test_challenge_consumed_on_success(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        _stub_verify(monkeypatch, verified=_verified_response())
        register_finish(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        # Challenge row gone.
        assert session.get(WebAuthnChallenge, opts.challenge_id) is None

    def test_expected_challenge_passed_to_verifier(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The service hands the stashed bytes to py_webauthn verbatim."""
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        stashed = session.get(WebAuthnChallenge, opts.challenge_id)
        assert stashed is not None
        challenge_bytes = stashed.challenge

        calls = _stub_verify(monkeypatch, verified=_verified_response())
        register_finish(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        assert len(calls) == 1
        assert calls[0]["expected_challenge"] == challenge_bytes
        assert calls[0]["rp"] is rp


# ---------------------------------------------------------------------------
# register_finish — error paths
# ---------------------------------------------------------------------------


class TestRegisterFinishErrors:
    """Every error branch the spec calls out maps to the right exception."""

    def test_replay_raises_not_found(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC #5: a replayed finish returns 409 (ChallengeNotFound)."""
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        _stub_verify(monkeypatch, verified=_verified_response())
        register_finish(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        # Second call with the same challenge_id.
        with pytest.raises(ChallengeNotFound):
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )

    def test_unknown_challenge_raises_not_found(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        with pytest.raises(ChallengeNotFound):
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id="01HWA00000000000000000NONE",
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )

    def test_expired_challenge_raises(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """Past ``expires_at`` on the challenge row → 400."""
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        later = FrozenClock(_PINNED + timedelta(minutes=11))
        with pytest.raises(ChallengeExpired):
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=later,
                rp=rp,
            )

    def test_mismatched_challenge_wraps_pywebauthn_error(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC #2: mismatched challenge / origin / rp_id → 400."""
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        _stub_verify_raises(monkeypatch, message="challenge mismatch")
        with pytest.raises(InvalidRegistration) as exc:
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )
        assert "challenge mismatch" in str(exc.value)
        # Challenge row MUST remain so the caller can retry
        # (consumption is reserved for successful finishes).
        assert session.get(WebAuthnChallenge, opts.challenge_id) is not None

    def test_origin_mismatch_wraps_pywebauthn_error(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        _stub_verify_raises(monkeypatch, message="origin not allowed")
        with pytest.raises(InvalidRegistration) as exc:
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )
        assert "origin" in str(exc.value).lower()

    def test_signup_challenge_rejected_in_user_finish(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """A signup-bound challenge MUST NOT redeem via ``register_finish``."""
        opts = register_start_signup(
            session,
            signup_session_id="01HWA00000000000000000SGN1",
            email="sig@example.com",
            display_name="Signup",
            clock=clock,
            rp=rp,
        )
        with pytest.raises(ChallengeSubjectMismatch):
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )

    def test_wrong_user_id_rejected(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """A challenge minted for user A must not redeem for user B."""
        # Mint the challenge for user A.
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        # Finish asserting user B's id — even though the session
        # layer would normally prevent this, the service layer must
        # refuse it.
        with pytest.raises(ChallengeSubjectMismatch):
            register_finish(
                user_ctx,
                session,
                user_id="01HWA0000000000000000OTHR",
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )

    def test_cap_recheck_blocks_race(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a sibling enrolment landed before finish, the 6th attempt
        raises :class:`TooManyPasskeys`.

        Simulates the race by minting a valid challenge, then seeding
        5 credential rows, then calling finish — the cap recheck
        inside finish fires even though the start passed.
        """
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        # Concurrent enrolment landed 5 rows between start and finish.
        for i in range(5):
            session.add(
                PasskeyCredential(
                    id=bytes([i + 0x10]) * 32,
                    user_id=user_ctx.actor_id,
                    public_key=b"\x00" * 32,
                    sign_count=0,
                    backup_eligible=False,
                    created_at=_PINNED,
                )
            )
        session.flush()

        _stub_verify(monkeypatch, verified=_verified_response())
        with pytest.raises(TooManyPasskeys):
            register_finish(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )

    def test_no_commit_in_service(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Caller-owned transaction: rollback discards credential + audit."""
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        verified = _verified_response()
        _stub_verify(monkeypatch, verified=verified)
        register_finish(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        session.rollback()
        # After rollback the fresh transaction sees no rows.
        assert session.get(PasskeyCredential, verified.credential_id) is None
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.actor_id == user_ctx.actor_id)
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# Signup flow — no user, no workspace, no audit
# ---------------------------------------------------------------------------


class TestRegisterSignupFlow:
    """Signup path: tenant-agnostic, no audit (owned by cd-3i5)."""

    def test_signup_start_persists_challenge(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        opts = register_start_signup(
            session,
            signup_session_id="01HWA00000000000000000SGNX",
            email="new@example.com",
            display_name="New User",
            clock=clock,
            rp=rp,
        )
        row = session.get(WebAuthnChallenge, opts.challenge_id)
        assert row is not None
        assert row.user_id is None
        assert row.signup_session_id == "01HWA00000000000000000SGNX"

    def test_signup_finish_writes_credential_no_audit(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No audit row is written — the signup service owns that audit."""
        # Seed a user that will be the signup's freshly minted row.
        user = bootstrap_user(
            session,
            email="brandnew@example.com",
            display_name="Brand New",
            clock=clock,
        )
        opts = register_start_signup(
            session,
            signup_session_id="01HWA00000000000000000SGN2",
            email="brandnew@example.com",
            display_name="Brand New",
            clock=clock,
            rp=rp,
        )
        verified = _verified_response(credential_id=b"\xcc" * 32)
        _stub_verify(monkeypatch, verified=verified)

        ref = register_finish_signup(
            session,
            signup_session_id="01HWA00000000000000000SGN2",
            user_id=user.id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            clock=clock,
            rp=rp,
        )
        assert ref.user_id == user.id
        # Credential persisted.
        assert session.get(PasskeyCredential, verified.credential_id) is not None
        # No audit row exists — the signup service emits it.
        rows = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == ref.credential_id_b64url)
        ).all()
        assert rows == []

    def test_signup_finish_rejects_user_flow_challenge(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """A user-flow challenge must not redeem via the signup finish."""
        opts = register_start(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            clock=clock,
            rp=rp,
        )
        with pytest.raises(ChallengeSubjectMismatch):
            register_finish_signup(
                session,
                signup_session_id="01HWA00000000000000000SGN3",
                user_id=user_ctx.actor_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )

    def test_signup_finish_rejects_wrong_signup_session(
        self,
        session: Session,
        rp: RelyingParty,
        clock: FrozenClock,
    ) -> None:
        """Cross-signup replay is refused."""
        opts = register_start_signup(
            session,
            signup_session_id="01HWA00000000000000000SGNA",
            email="a@example.com",
            display_name="A",
            clock=clock,
            rp=rp,
        )
        with pytest.raises(ChallengeSubjectMismatch):
            register_finish_signup(
                session,
                signup_session_id="01HWA00000000000000000SGNB",
                user_id="01HWA00000000000000000USRX",
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                clock=clock,
                rp=rp,
            )


# ---------------------------------------------------------------------------
# Alias types
# ---------------------------------------------------------------------------


class TestErrorAliases:
    """``ChallengeAlreadyConsumed`` is a distinct type from
    ``ChallengeNotFound`` so tests can pin the spec mapping, even
    though the router collapses both to 409."""

    def test_distinct_types(self) -> None:
        # Both subclass ``LookupError`` but are declared in separate
        # ``class`` statements; mypy sees them as non-overlapping, which
        # is the contract we want the suite to lock in.
        assert ChallengeAlreadyConsumed.__qualname__ != ChallengeNotFound.__qualname__
        assert issubclass(ChallengeAlreadyConsumed, LookupError)
        assert issubclass(ChallengeNotFound, LookupError)
