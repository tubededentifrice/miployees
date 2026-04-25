"""Unit tests for :mod:`app.auth.signup`.

Covers the spec (§03 "Self-serve signup") end-to-end at the domain-
service level — start, verify, complete, and the orphan GC callable
— plus the HTTP router's ``signup_enabled=false → 404`` gate.

The mailer is a recording double; the passkey verifier is stubbed so
the tests don't need a real authenticator. The stub lives in
:class:`_StubRegistrationVerifier` and produces a deterministic
:class:`VerifiedRegistration` so we can assert row-level writes
without WebAuthn noise.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from webauthn.helpers.structs import (
    AttestationFormat,
    CredentialDeviceType,
    PublicKeyCredentialType,
)

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    MagicLinkNonce,
    PasskeyCredential,
    SignupAttempt,
    User,
    WebAuthnChallenge,
)
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session
from app.api.v1.auth.signup import build_signup_router
from app.auth import magic_link, passkey, signup
from app.auth._throttle import Throttle
from app.auth.webauthn import VerifiedRegistration
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _SentMessage:
    to: list[str]
    subject: str
    body_text: str


@dataclass
class _RecordingMailer:
    sent: list[_SentMessage] = field(default_factory=list)

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
        self.sent.append(
            _SentMessage(to=list(to), subject=subject, body_text=body_text)
        )
        return "test-message-id"


@dataclass
class _ExplodingMailer:
    """:class:`Mailer` double that raises a pre-canned exception on send.

    Drives the §15 enumeration-guard coverage in
    :class:`TestStartSignupEnumerationGuard` — mirrors the fixture of
    the same name in :mod:`tests.unit.auth.test_recovery` so the shape
    stays consistent across the three auth surfaces that swallow
    :class:`MailDeliveryError` uniformly.
    """

    exc: BaseException

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
        del to, subject, body_text, body_html, headers, reply_to
        raise self.exc


def _stub_verify_registration(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> bytes:
    """Monkeypatch :func:`app.auth.passkey._verify_or_raise`.

    Returns a deterministic credential id so tests can assert the
    :class:`PasskeyCredential` row landed. When ``fail=True`` the
    stub raises :class:`~app.auth.passkey.InvalidRegistration` to
    exercise the atomicity / rollback path (cd-3i5 AC #1).
    """
    credential_id = b"cred-" + b"x" * 27  # 32 bytes total, bytes-typed
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
        if fail:
            raise passkey.InvalidRegistration("stubbed registration failure")
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
        root_key=SecretStr("unit-test-root-key-do-not-ship"),
        public_url="https://crew.day",
    )


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
def mailer() -> _RecordingMailer:
    return _RecordingMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def capabilities_enabled() -> Capabilities:
    """Capabilities with signup enabled (default).

    ``captcha_required=False`` because these tests don't exercise the
    CAPTCHA gate added in cd-055; the abuse gate + its refusal
    shapes are covered in :mod:`tests.unit.auth.test_signup_abuse`.
    """
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
def capabilities_disabled() -> Capabilities:
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
        settings=DeploymentSettings(signup_enabled=False),
    )


def _extract_token(message: _SentMessage) -> str:
    for line in message.body_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"no magic-link URL in body: {message.body_text!r}")


def _make_signup_challenge(
    session: Session,
    *,
    signup_attempt_id: str,
    now: datetime,
) -> str:
    """Seed a :class:`WebAuthnChallenge` row for the signup path.

    Mirrors the real :func:`passkey.register_start_signup` side effect
    so the complete_signup test can call the verify half in isolation.
    """
    challenge_id = "01HWACHLNG0000000000000000"
    row = WebAuthnChallenge(
        id=challenge_id,
        user_id=None,
        signup_session_id=signup_attempt_id,
        challenge=b"\x00" * 32,
        exclude_credentials=[],
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    session.add(row)
    session.flush()
    return challenge_id


def _dummy_credential() -> dict[str, Any]:
    """Return a placeholder WebAuthn attestation payload.

    The real verifier is stubbed in these tests, so the shape only
    needs to parse as a dict; the contents are never inspected.
    """
    return {"id": "dummy", "rawId": "dummy", "response": {}, "type": "public-key"}


# ---------------------------------------------------------------------------
# start_signup
# ---------------------------------------------------------------------------


class TestStartSignupHappy:
    def test_inserts_signup_attempt_and_nonce(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        signup.start_signup(
            session,
            email="NEW@example.com",
            desired_slug="villa-sud",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        attempts = session.scalars(select(SignupAttempt)).all()
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.email_lower == "new@example.com"
        assert attempt.desired_slug == "villa-sud"
        assert attempt.verified_at is None
        assert attempt.completed_at is None
        assert attempt.expires_at - attempt.created_at == timedelta(minutes=15)

        # The magic-link nonce was minted with the attempt id as subject.
        nonce = session.scalars(select(MagicLinkNonce)).one()
        assert nonce.subject_id == attempt.id
        assert nonce.purpose == "signup_verify"

        # Mail was sent.
        assert len(mailer.sent) == 1
        assert mailer.sent[0].to == ["new@example.com"]


class TestStartSignupGates:
    def test_signup_disabled_raises(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_disabled: Capabilities,
    ) -> None:
        with pytest.raises(signup.SignupDisabled):
            signup.start_signup(
                session,
                email="x@example.com",
                desired_slug="villa-sud",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_disabled,
                now=_PINNED,
                settings=settings,
            )

    def test_invalid_slug_pattern_raises_invalid_slug(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        from app.tenancy import InvalidSlug

        with pytest.raises(InvalidSlug):
            signup.start_signup(
                session,
                email="x@example.com",
                desired_slug="_invalid",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED,
                settings=settings,
            )

    def test_reserved_slug_raises(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        with pytest.raises(signup.SlugReserved):
            signup.start_signup(
                session,
                email="x@example.com",
                desired_slug="admin",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED,
                settings=settings,
            )

    def test_slug_taken_raises(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        # Pre-seed a workspace using the slug.
        session.add(
            Workspace(
                id="01HWAWSPREEXIST00000000000",
                slug="villa-sud",
                name="Villa Sud",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.flush()

        with pytest.raises(signup.SlugTaken) as excinfo:
            signup.start_signup(
                session,
                email="x@example.com",
                desired_slug="villa-sud",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED,
                settings=settings,
            )
        # Spec §03 step 1: the exception carries a suggested alternative
        # the router serialises onto the 409 body.
        assert excinfo.value.slug == "villa-sud"
        assert excinfo.value.suggested_alternative == "villa-sud-2"

    def test_homoglyph_collision_raises_with_colliding_slug(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        session.add(
            Workspace(
                id="01HWAWSPEXIST000000000ABCD",
                slug="micasa",
                name="Mi Casa",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.flush()

        with pytest.raises(signup.SlugHomoglyphError) as excinfo:
            signup.start_signup(
                session,
                email="x@example.com",
                desired_slug="rnicasa",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED,
                settings=settings,
            )
        assert excinfo.value.colliding_slug == "micasa"
        assert excinfo.value.candidate == "rnicasa"


class TestSlugTakenSuggestion:
    """Parametrised checks on :class:`SlugTaken.suggested_alternative`.

    Spec §03 step 1 requires the 409 body to carry a
    ``suggested_alternative`` slug. The suggestion must itself be valid
    per :func:`validate_slug`, not reserved, and not a homoglyph
    collision against any live workspace slug. These two cases cover:

    * **Clean suffix** — ``villa-sud`` taken → ``villa-sud-2`` wins the
      first probe.
    * **Reserved suffix bump** — ``my-admin-1`` taken alongside
      ``my-admin-1-2`` taken → scanner skips those and lands on
      ``my-admin-1-3``. (The ``-2`` slot is occupied; the scanner
      bumps to ``-3``; ``-3`` is free.)
    """

    @pytest.mark.parametrize(
        ("pre_seeded_slugs", "desired", "expected_suggestion"),
        [
            (["villa-sud"], "villa-sud", "villa-sud-2"),
            (
                ["my-admin-1", "my-admin-1-2"],
                "my-admin-1",
                "my-admin-1-3",
            ),
        ],
    )
    def test_suggestion_is_valid_and_free(
        self,
        pre_seeded_slugs: list[str],
        desired: str,
        expected_suggestion: str,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        for i, slug in enumerate(pre_seeded_slugs):
            session.add(
                Workspace(
                    id=f"01HWAWSPRE{i:016d}",
                    slug=slug,
                    name=slug,
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
        session.flush()

        with pytest.raises(signup.SlugTaken) as excinfo:
            signup.start_signup(
                session,
                email="probe@example.com",
                desired_slug=desired,
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED,
                settings=settings,
            )
        assert excinfo.value.slug == desired
        assert excinfo.value.suggested_alternative == expected_suggestion


class TestSlugSuggestionLongBase:
    """Long slugs: the probe shortens the base so ``<base>-<n>`` stays ≤ 40.

    Spec §03 step 1 pins a 40-char ceiling on slugs; a naïve
    ``<desired>-2`` concatenation on a 39-char base would blow through
    it, :func:`validate_slug` would raise, and the scanner would fall
    back to the original taken slug — so the 409 body would suggest the
    exact slug the caller already knows is taken. We guard against this
    by truncating the base, dropping any trailing hyphen, and probing
    the shortened form.
    """

    def test_39_char_base_truncates_and_probes(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        # 39-char slug: starts [a-z], alnum interior, ends [a-z]. Shape
        # ``a<b*37>c`` = 39 chars. Adding ``-2`` would land at 41 → over
        # the cap. The suggestion must come from a shortened base.
        desired = "a" + "b" * 37 + "c"
        assert len(desired) == 39
        session.add(
            Workspace(
                id="01HWAWSLONG0000000000000AB",
                slug=desired,
                name=desired,
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.flush()

        with pytest.raises(signup.SlugTaken) as excinfo:
            signup.start_signup(
                session,
                email="long@example.com",
                desired_slug=desired,
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED,
                settings=settings,
            )
        suggestion = excinfo.value.suggested_alternative
        # The suggestion must (a) not be the taken slug, (b) fit the
        # 40-char ceiling, (c) pass :func:`validate_slug` and (d) share
        # a prefix with the desired slug so the UI still looks sensible.
        from app.tenancy import validate_slug as _validate_slug

        assert suggestion != desired
        assert len(suggestion) <= 40
        _validate_slug(suggestion)
        assert suggestion.startswith(desired[:30])

    def test_base_truncated_down_to_unusable_falls_back(self) -> None:
        """If the truncation ate every character, return the original slug.

        Unreachable in practice (the min-length regex rejects anything
        short enough to collapse), but keeping the invariant locked
        means future tweaks to ``_MAX_SUGGESTION_ATTEMPTS`` won't
        silently resurrect a broken fallback.
        """
        from app.auth.signup import _suggest_alternative_slug

        # Seed impossible-to-shorten candidates so the defensive path
        # fires: every possible suffix is already taken. Here we
        # simulate the edge by passing a reasonable slug with every
        # probe occupied.
        desired = "villa-sud"
        all_taken = [desired] + [f"{desired}-{n}" for n in range(2, 25)]
        # Every probe collides → fall back to the desired slug.
        assert _suggest_alternative_slug(desired, existing_slugs=all_taken) == desired


class TestRouterSlugTakenBody:
    """The router attaches the suggestion to the 409 body."""

    @pytest.fixture
    def client(
        self,
        capabilities_enabled: Capabilities,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        engine: Engine,
    ) -> Iterator[TestClient]:
        app = FastAPI()
        router = build_signup_router(
            mailer=mailer,
            throttle=throttle,
            capabilities=capabilities_enabled,
            base_url="https://crew.day",
            settings=settings,
        )
        app.include_router(router, prefix="/api/v1")

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

        def _db() -> Iterator[Session]:
            with factory() as s:
                yield s
                s.commit()

        app.dependency_overrides[db_session] = _db
        with TestClient(app) as c:
            yield c

    def test_slug_taken_returns_409_with_suggested_alternative(
        self,
        client: TestClient,
        engine: Engine,
    ) -> None:
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            s.add(
                Workspace(
                    id="01HWAWSRTAKEN000000000001A",
                    slug="villa-sud",
                    name="Villa Sud",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            s.commit()

        r = client.post(
            "/api/v1/signup/start",
            json={"email": "taken@example.com", "desired_slug": "villa-sud"},
        )
        assert r.status_code == 409
        body = r.json()["detail"]
        assert body["error"] == "slug_taken"
        assert body["suggested_alternative"] == "villa-sud-2"


class TestStartSignupRetry:
    """Same ``(email, desired_slug)`` inside TTL → refresh, not 500."""

    def test_retry_refreshes_attempt_and_invalidates_prior_nonce(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        # First request.
        signup.start_signup(
            session,
            email="retry@example.com",
            desired_slug="casa-retry",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        session.commit()
        first_token = _extract_token(mailer.sent[0])
        attempts = session.scalars(select(SignupAttempt)).all()
        assert len(attempts) == 1
        first_attempt_id = attempts[0].id

        # Second request 5 minutes later, same email + slug, different IP.
        later = _PINNED + timedelta(minutes=5)
        signup.start_signup(
            session,
            email="retry@example.com",
            desired_slug="casa-retry",
            ip="203.0.113.99",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=later,
            settings=settings,
        )
        session.commit()

        # Still exactly one signup_attempt — the second request reused
        # the existing row rather than hitting the UNIQUE violation.
        attempts_after = session.scalars(select(SignupAttempt)).all()
        assert len(attempts_after) == 1
        refreshed = attempts_after[0]
        assert refreshed.id == first_attempt_id
        # expires_at bumped by 15 minutes from the retry time.
        expected_expiry = later + timedelta(minutes=15)
        # SQLite drops tzinfo on the round-trip — compare normalised.
        refreshed_expiry = refreshed.expires_at
        if refreshed_expiry.tzinfo is None:
            refreshed_expiry = refreshed_expiry.replace(tzinfo=UTC)
        assert refreshed_expiry == expected_expiry

        # ip_hash reflects the latest IP (203.0.113.99 peppered).
        from app.auth.keys import derive_subkey

        pepper = derive_subkey(settings.root_key, purpose="magic-link")
        expected_ip_hash = hashlib.sha256()
        expected_ip_hash.update(b"203.0.113.99")
        expected_ip_hash.update(pepper)
        assert refreshed.ip_hash == expected_ip_hash.hexdigest()

        # Exactly one pending nonce now — the first was deleted.
        pending_nonces = session.scalars(
            select(MagicLinkNonce).where(MagicLinkNonce.consumed_at.is_(None))
        ).all()
        assert len(pending_nonces) == 1
        # And the pending one targets the SAME signup_attempt id.
        assert pending_nonces[0].subject_id == first_attempt_id

        # The prior token is no longer redeemable — the nonce row is
        # gone, so consume raises AlreadyConsumed.
        with pytest.raises(magic_link.AlreadyConsumed):
            signup.consume_verify(
                session,
                token=first_token,
                ip="203.0.113.99",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=later + timedelta(seconds=5),
                settings=settings,
            )

        # The freshly-minted token still consumes cleanly.
        fresh_token = _extract_token(mailer.sent[-1])
        outcome = signup.consume_verify(
            session,
            token=fresh_token,
            ip="203.0.113.99",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=later + timedelta(seconds=10),
            settings=settings,
        )
        assert outcome.signup_attempt_id == first_attempt_id


class TestStartSignupRetryPurposeGuard:
    """The retry nonce sweep is scoped to ``purpose='signup_verify'``.

    The ``magic_link_nonce.subject_id`` column is soft-typed: signup
    carries a signup_attempt ULID, recover / email-change / invite
    carry a user.id / invite.id ULID from the same 128-bit space.
    Collisions are astronomically unlikely, but the retry path should
    only ever sweep signup-verify rows — narrowing the DELETE predicate
    is defence-in-depth.

    Rather than rely on a real ULID collision (statistically impossible
    to reproduce in a test), we seed a parallel-purpose nonce with the
    **same** ``subject_id`` as the signup attempt and assert the retry
    leaves it untouched.
    """

    def test_retry_does_not_sweep_other_purpose_nonces(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        # First signup/start lands a signup_verify nonce.
        signup.start_signup(
            session,
            email="purpose@example.com",
            desired_slug="casa-purpose",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        session.commit()
        attempt = session.scalars(select(SignupAttempt)).one()

        # Simulate an unrelated purpose nonce that — hypothetically —
        # collides on subject_id. Here we seed it directly with the
        # same id so the predicate's purpose filter is exercised.
        from app.auth.keys import derive_subkey

        pepper = derive_subkey(settings.root_key, purpose="magic-link")
        collider = MagicLinkNonce(
            jti="01HWMLNCOLLIDER0000000001A",
            purpose="recover_passkey",
            subject_id=attempt.id,  # same ULID as the signup_attempt
            consumed_at=None,
            expires_at=_PINNED + timedelta(minutes=10),
            created_ip_hash=hashlib.sha256(b"127.0.0.1" + pepper).hexdigest(),
            created_email_hash=hashlib.sha256(
                b"other@example.com" + pepper
            ).hexdigest(),
            created_at=_PINNED,
        )
        session.add(collider)
        session.commit()

        # Trigger the retry path — same email + slug, 5 minutes later.
        signup.start_signup(
            session,
            email="purpose@example.com",
            desired_slug="casa-purpose",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED + timedelta(minutes=5),
            settings=settings,
        )
        session.commit()

        # The recover_passkey nonce must still be present, unconsumed.
        surviving = session.scalars(
            select(MagicLinkNonce).where(MagicLinkNonce.purpose == "recover_passkey")
        ).all()
        assert len(surviving) == 1
        assert surviving[0].jti == "01HWMLNCOLLIDER0000000001A"
        assert surviving[0].consumed_at is None


class TestStartSignupEnumerationGuard:
    """§15: a mailer outage must not fail ``start_signup``.

    Mirrors
    :class:`tests.unit.auth.test_recovery.TestRequestRecoveryEnumerationGuard
    .test_hit_branch_swallows_mail_delivery_error`. ``start_signup``
    delegates the actual send to :func:`app.auth.magic_link.request_link`
    via ``send_email=True``; the guard therefore lives inside
    :mod:`app.auth.magic_link` but the signup-level observable is the
    same: caller does NOT raise and both ``magic_link.sent`` +
    ``signup.requested`` audit rows commit so operators can still see
    the outage in forensic logs.
    """

    def test_signup_start_swallows_mail_delivery_error(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        from app.adapters.mail.ports import MailDeliveryError

        failing_mailer = _ExplodingMailer(MailDeliveryError("relay down"))
        # Must NOT raise — the magic-link send inside request_link
        # swallows MailDeliveryError per §15.
        signup.start_signup(
            session,
            email="outage@example.com",
            desired_slug="villa-outage",
            ip="127.0.0.1",
            mailer=failing_mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        # The signup_attempt row committed.
        attempts = session.scalars(select(SignupAttempt)).all()
        assert len(attempts) == 1
        assert attempts[0].desired_slug == "villa-outage"
        # The magic-link nonce committed so a retry / operator resend
        # is viable once SMTP recovers.
        nonces = session.scalars(select(MagicLinkNonce)).all()
        assert len(nonces) == 1
        # Both audit rows lived — forensic trail intact despite the
        # mailer outage.
        signup_audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "signup.requested")
        ).all()
        assert len(signup_audits) == 1
        magic_audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "magic_link.sent")
        ).all()
        assert len(magic_audits) == 1


class TestStartSignupAudit:
    def test_audit_row_carries_hashes_only(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        signup.start_signup(
            session,
            email="audit@example.com",
            desired_slug="villa-nord",
            ip="203.0.113.55",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        rows = session.scalars(
            select(AuditLog).where(AuditLog.action == "signup.requested")
        ).all()
        assert len(rows) == 1
        diff = rows[0].diff
        assert isinstance(diff, dict)
        assert len(diff["email_hash"]) == 64
        assert len(diff["ip_hash"]) == 64
        assert diff["desired_slug"] == "villa-nord"
        # Plaintext NEVER in diff.
        assert "audit@example.com" not in str(diff)
        assert "203.0.113.55" not in str(diff)


# ---------------------------------------------------------------------------
# consume_verify
# ---------------------------------------------------------------------------


class TestConsumeVerifyHappy:
    def test_round_trip_marks_verified(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        signup.start_signup(
            session,
            email="rv@example.com",
            desired_slug="casa-uno",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])

        ssn = signup.consume_verify(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED + timedelta(minutes=1),
            settings=settings,
        )
        attempt = session.scalars(select(SignupAttempt)).one()
        assert attempt.verified_at is not None
        assert ssn.signup_attempt_id == attempt.id
        assert ssn.email_lower == "rv@example.com"
        assert ssn.desired_slug == "casa-uno"


class TestConsumeVerifyErrors:
    def test_wrong_purpose_token_rejected(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        # Seed a user so the recover flow has a subject.
        session.add(
            User(
                id="01HWAUSR0000000000000000X1",
                email="rec@example.com",
                email_lower="rec@example.com",
                display_name="Rec",
                created_at=_PINNED,
            )
        )
        session.flush()
        # cd-9i7z: ``request_link`` returns a deferred-send pending;
        # fire the send so the recording mailer captures the body.
        pending = magic_link.request_link(
            session,
            email="rec@example.com",
            purpose="recover_passkey",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        assert pending is not None
        pending.deliver()
        token = _extract_token(mailer.sent[0])
        with pytest.raises(magic_link.PurposeMismatch):
            signup.consume_verify(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED + timedelta(minutes=1),
                settings=settings,
            )

    def test_expired_token_raises(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        signup.start_signup(
            session,
            email="exp@example.com",
            desired_slug="casa-dos",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])
        with pytest.raises(magic_link.TokenExpired):
            signup.consume_verify(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED + timedelta(minutes=20),
                settings=settings,
            )

    def test_replay_raises_already_consumed(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        signup.start_signup(
            session,
            email="re@example.com",
            desired_slug="casa-tres",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])
        signup.consume_verify(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED + timedelta(minutes=1),
            settings=settings,
        )
        with pytest.raises(magic_link.AlreadyConsumed):
            signup.consume_verify(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=throttle,
                capabilities=capabilities_enabled,
                now=_PINNED + timedelta(minutes=2),
                settings=settings,
            )


# ---------------------------------------------------------------------------
# complete_signup
# ---------------------------------------------------------------------------


def _start_and_verify(
    session: Session,
    mailer: _RecordingMailer,
    throttle: Throttle,
    settings: Settings,
    capabilities: Capabilities,
    *,
    email: str = "owner@example.com",
    desired_slug: str = "villa-sud",
    ip: str = "127.0.0.1",
) -> signup.SignupSession:
    """Run start + verify and return the :class:`SignupSession`."""
    signup.start_signup(
        session,
        email=email,
        desired_slug=desired_slug,
        ip=ip,
        mailer=mailer,
        base_url="https://crew.day",
        throttle=throttle,
        capabilities=capabilities,
        now=_PINNED,
        settings=settings,
    )
    token = _extract_token(mailer.sent[-1])
    return signup.consume_verify(
        session,
        token=token,
        ip=ip,
        throttle=throttle,
        capabilities=capabilities,
        now=_PINNED + timedelta(minutes=1),
        settings=settings,
    )


class TestCompleteSignupHappy:
    def test_creates_all_rows_in_one_transaction(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_verify_registration(monkeypatch)
        ssn = _start_and_verify(
            session, mailer, throttle, settings, capabilities_enabled
        )
        challenge_id = _make_signup_challenge(
            session,
            signup_attempt_id=ssn.signup_attempt_id,
            now=_PINNED + timedelta(minutes=1),
        )
        result = signup.complete_signup(
            session,
            signup_attempt_id=ssn.signup_attempt_id,
            display_name="Owner One",
            timezone="Pacific/Auckland",
            challenge_id=challenge_id,
            passkey_payload=_dummy_credential(),
            ip="127.0.0.1",
            capabilities=capabilities_enabled,
            now=_PINNED + timedelta(minutes=2),
            settings=settings,
        )
        assert result.slug == "villa-sud"

        # Workspace + user + user_workspace all landed.
        workspace = session.scalars(
            select(Workspace).where(Workspace.id == result.workspace_id)
        ).one()
        assert workspace.slug == "villa-sud"
        assert workspace.plan == "free"
        # Tight initial caps — 10% of free-tier seeds.
        assert workspace.quota_json["llm_budget_cents_30d"] == 50

        user = session.scalars(select(User).where(User.id == result.user_id)).one()
        assert user.display_name == "Owner One"
        assert user.timezone == "Pacific/Auckland"

        memberships = session.scalars(
            select(UserWorkspace).where(UserWorkspace.user_id == result.user_id)
        ).all()
        assert len(memberships) == 1
        assert memberships[0].source == "workspace_grant"

        # Four system groups + one owners membership + one manager grant.
        groups = session.scalars(
            select(PermissionGroup).where(
                PermissionGroup.workspace_id == result.workspace_id
            )
        ).all()
        group_slugs = {g.slug for g in groups}
        assert group_slugs == {"owners", "managers", "all_workers", "all_clients"}

        owners_group = next(g for g in groups if g.slug == "owners")
        members = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == owners_group.id
            )
        ).all()
        assert len(members) == 1
        assert members[0].user_id == result.user_id

        grants = session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == result.user_id)
        ).all()
        assert len(grants) == 1
        assert grants[0].grant_role == "manager"
        assert grants[0].scope_property_id is None

        # Passkey credential row landed.
        cred = session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.user_id == result.user_id)
        ).one()
        assert cred.user_id == result.user_id

        # Signup attempt marked completed + workspace_id populated.
        attempt = session.scalars(select(SignupAttempt)).one()
        assert attempt.completed_at is not None
        assert attempt.workspace_id == result.workspace_id

        # audit.signup.completed carries the real workspace ctx.
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "signup.completed")
        ).one()
        assert audit.workspace_id == result.workspace_id
        assert audit.actor_id == result.user_id
        assert audit.actor_kind == "user"


class TestCompleteSignupAtomicity:
    def test_passkey_failure_rolls_back_every_insert(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC #1 — every insert rolls back on any downstream failure."""
        _stub_verify_registration(monkeypatch, fail=True)
        ssn = _start_and_verify(
            session, mailer, throttle, settings, capabilities_enabled
        )
        challenge_id = _make_signup_challenge(
            session,
            signup_attempt_id=ssn.signup_attempt_id,
            now=_PINNED + timedelta(minutes=1),
        )

        with pytest.raises(passkey.InvalidRegistration):
            signup.complete_signup(
                session,
                signup_attempt_id=ssn.signup_attempt_id,
                display_name="Owner",
                timezone="Pacific/Auckland",
                challenge_id=challenge_id,
                passkey_payload=_dummy_credential(),
                ip="127.0.0.1",
                capabilities=capabilities_enabled,
                now=_PINNED + timedelta(minutes=2),
                settings=settings,
            )
        session.rollback()

        # No workspace / user / membership row should exist.
        assert session.scalars(select(Workspace)).all() == []
        assert session.scalars(select(User)).all() == []
        assert session.scalars(select(UserWorkspace)).all() == []
        assert session.scalars(select(PermissionGroup)).all() == []


class TestCompleteSignupGuards:
    def test_not_verified_attempt_rejected(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_verify_registration(monkeypatch)
        # Start but don't verify.
        signup.start_signup(
            session,
            email="x@example.com",
            desired_slug="casa-free",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        attempt = session.scalars(select(SignupAttempt)).one()
        challenge_id = _make_signup_challenge(
            session, signup_attempt_id=attempt.id, now=_PINNED
        )
        with pytest.raises(signup.SignupAttemptExpired) as excinfo:
            signup.complete_signup(
                session,
                signup_attempt_id=attempt.id,
                display_name="Owner",
                timezone="Pacific/Auckland",
                challenge_id=challenge_id,
                passkey_payload=_dummy_credential(),
                ip="127.0.0.1",
                capabilities=capabilities_enabled,
                now=_PINNED + timedelta(minutes=1),
                settings=settings,
            )
        assert excinfo.value.state == "not_verified"


# ---------------------------------------------------------------------------
# signup_gc — prune_stale_signups
# ---------------------------------------------------------------------------


class TestPruneStaleSignups:
    def test_prunes_only_orphaned_old_workspaces(
        self,
        session: Session,
    ) -> None:
        # Orphan A: no users, old. Should be pruned.
        orphan_a = Workspace(
            id="01HWAWSORPHAN0000000000001",
            slug="orphan-a",
            name="Orphan A",
            plan="free",
            quota_json={},
            created_at=_PINNED - timedelta(hours=2),
        )
        # Orphan B: no users, young. Should be kept.
        orphan_b = Workspace(
            id="01HWAWSORPHAN0000000000002",
            slug="orphan-b",
            name="Orphan B",
            plan="free",
            quota_json={},
            created_at=_PINNED - timedelta(minutes=10),
        )
        # Kept: has a user, old. Should be kept.
        kept = Workspace(
            id="01HWAWSKEPT00000000000003",
            slug="kept",
            name="Kept",
            plan="free",
            quota_json={},
            created_at=_PINNED - timedelta(hours=2),
        )
        session.add_all([orphan_a, orphan_b, kept])
        session.flush()
        session.add(
            UserWorkspace(
                user_id="01HWAUSERID00000000000ABCD",
                workspace_id=kept.id,
                source="workspace_grant",
                added_at=_PINNED - timedelta(hours=1),
            )
        )
        session.flush()

        deleted = signup.prune_stale_signups(session, now=_PINNED)

        assert deleted == [orphan_a.id]
        remaining = session.scalars(select(Workspace.id)).all()
        assert orphan_a.id not in remaining
        assert orphan_b.id in remaining
        assert kept.id in remaining


# ---------------------------------------------------------------------------
# provision_workspace_and_owner_seat — budget ledger seeding (cd-tubi)
# ---------------------------------------------------------------------------


def _capabilities_with_cap(cap_cents: int) -> Capabilities:
    """Build a :class:`Capabilities` pinned at ``cap_cents``."""
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
        settings=DeploymentSettings(
            signup_enabled=True,
            captcha_required=False,
            llm_default_budget_cents_30d=cap_cents,
        ),
    )


class TestProvisionSeedsBudgetLedger:
    """cd-tubi: every fresh workspace lands a :class:`BudgetLedger` row.

    Before cd-tubi the ledger was seeded ad-hoc in tests but never by
    the actual provisioning path — :func:`check_budget` fell closed
    on every call (§11 "Cap": *"This row is inserted in the same
    transaction as the workspace row."*). These tests pin the
    invariant at the `provision_workspace_and_owner_seat` seam so
    both self-serve signup (``complete_signup``) and the dev-login
    script inherit the behaviour for free.
    """

    def test_provision_seeds_budget_ledger_with_default_cap(
        self, session: Session
    ) -> None:
        """Without ``capabilities``, the ledger seeds at the tight-cap fraction.

        §03 "Tight initial caps": freshly-signed-up workspaces run at
        10 % of the plan ceiling until human verification. The ledger
        ``cap_cents`` and ``workspace.quota_json['llm_budget_cents_30d']``
        must therefore agree on the scaled number (50 cents, not 500)
        — otherwise an abusive signup could burn the full free-tier
        budget the spec pins at the verified state.
        """
        from app.domain.plans import tight_cap_cents

        workspace_id = "01HWAWSTUBI00000000000FLBK"
        user_id = "01HWAUSRTUBI000000000FLBKU"

        signup.provision_workspace_and_owner_seat(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            slug="tubi-fallback",
            email_lower="fallback@example.com",
            display_name="Fallback",
            timezone="UTC",
            now=_PINNED,
        )

        ledger = session.scalars(
            select(BudgetLedger).where(BudgetLedger.workspace_id == workspace_id)
        ).one()
        # FALLBACK_CAP_CENTS is the full-tier ceiling (500); the signup
        # seam scales it to 50 before writing the row.
        assert signup.FALLBACK_CAP_CENTS == 500
        assert ledger.cap_cents == tight_cap_cents(signup.FALLBACK_CAP_CENTS)
        assert ledger.cap_cents == 50
        # Ledger ``cap_cents`` matches the quota blob's entry — both the
        # ledger (§11 "Cap") and ``workspace.quota_json`` now agree on
        # the same number.
        workspace = session.scalars(
            select(Workspace).where(Workspace.id == workspace_id)
        ).one()
        assert workspace.quota_json["llm_budget_cents_30d"] == ledger.cap_cents
        assert ledger.spent_cents == 0
        # 30-day rolling window; :func:`_window_bounds(now)` returns
        # ``[now-30d, now]`` so the freshly-seeded row's bounds match
        # what :func:`refresh_aggregate` would write next. Naive compare
        # survives SQLite's tzinfo strip on round-trip.
        assert ledger.period_end.replace(tzinfo=None) - ledger.period_start.replace(
            tzinfo=None
        ) == timedelta(days=30)
        assert ledger.period_end.replace(tzinfo=None) == _PINNED.replace(tzinfo=None)
        assert ledger.period_start.replace(tzinfo=None) == (
            _PINNED - timedelta(days=30)
        ).replace(tzinfo=None)

    def test_provision_seeds_budget_ledger_with_capabilities_override(
        self, session: Session
    ) -> None:
        """``Capabilities`` with a 10-cent full cap seeds tight 1-cent ledger.

        §03 scales the operator-supplied cap too — the operator's
        ``deployment_setting.llm_default_budget_cents_30d`` is the full
        post-verification ceiling; the freshly-signed-up workspace
        starts at 10 % of it (floored at 1 cent).
        """
        workspace_id = "01HWAWSTUBI000000000000DEM"
        user_id = "01HWAUSRTUBI00000000000DEM"
        caps = _capabilities_with_cap(10)

        signup.provision_workspace_and_owner_seat(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            slug="tubi-demo",
            email_lower="demo@example.com",
            display_name="Demo",
            timezone="UTC",
            now=_PINNED,
            capabilities=caps,
        )

        ledger = session.scalars(
            select(BudgetLedger).where(BudgetLedger.workspace_id == workspace_id)
        ).one()
        # 10-cent full cap → 1-cent tight cap (floored).
        assert ledger.cap_cents == 1
        assert ledger.spent_cents == 0

    def test_provision_budget_ledger_rolls_back_on_workspace_failure(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ledger insert shares the outer UoW — rolls back on any downstream failure.

        Induces a passkey-verification failure after
        :func:`provision_workspace_and_owner_seat` has already inserted
        the ledger row and asserts that the row is NOT visible
        post-rollback. Proves the same-transaction atomicity guarantee
        §11 "Cap" pins.
        """
        _stub_verify_registration(monkeypatch, fail=True)
        ssn = _start_and_verify(
            session, mailer, throttle, settings, capabilities_enabled
        )
        challenge_id = _make_signup_challenge(
            session,
            signup_attempt_id=ssn.signup_attempt_id,
            now=_PINNED + timedelta(minutes=1),
        )

        with pytest.raises(passkey.InvalidRegistration):
            signup.complete_signup(
                session,
                signup_attempt_id=ssn.signup_attempt_id,
                display_name="Owner",
                timezone="Pacific/Auckland",
                challenge_id=challenge_id,
                passkey_payload=_dummy_credential(),
                ip="127.0.0.1",
                capabilities=capabilities_enabled,
                now=_PINNED + timedelta(minutes=2),
                settings=settings,
            )
        session.rollback()

        # Ledger, workspace, user, membership — none should have landed.
        assert session.scalars(select(BudgetLedger)).all() == []
        assert session.scalars(select(Workspace)).all() == []

    def test_complete_signup_threads_capabilities_cap_to_ledger(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``complete_signup`` forwards ``capabilities`` through to the seed.

        The prod path is the one that matters: an operator's
        ``deployment_setting.llm_default_budget_cents_30d`` override
        takes effect on the very first workspace created after the
        admin mutation (the :class:`Capabilities` envelope is the
        single source of truth the provisioning seam reads). The
        operator's 420-cent full cap lands as 42 cents on the ledger
        per §03 "Tight initial caps" (10 %).
        """
        _stub_verify_registration(monkeypatch)
        # 420 cents full cap → 42 cents tight ledger (10 %).
        caps = _capabilities_with_cap(420)
        ssn = _start_and_verify(session, mailer, throttle, settings, caps)
        challenge_id = _make_signup_challenge(
            session,
            signup_attempt_id=ssn.signup_attempt_id,
            now=_PINNED + timedelta(minutes=1),
        )

        result = signup.complete_signup(
            session,
            signup_attempt_id=ssn.signup_attempt_id,
            display_name="Owner",
            timezone="UTC",
            challenge_id=challenge_id,
            passkey_payload=_dummy_credential(),
            ip="127.0.0.1",
            capabilities=caps,
            now=_PINNED + timedelta(minutes=2),
            settings=settings,
        )

        ledger = session.scalars(
            select(BudgetLedger).where(BudgetLedger.workspace_id == result.workspace_id)
        ).one()
        assert ledger.cap_cents == 42


# ---------------------------------------------------------------------------
# HTTP router — signup_enabled gate
# ---------------------------------------------------------------------------


class TestRouterSignupDisabled:
    """Every signup route returns 404 when ``signup_enabled=false``."""

    @pytest.fixture
    def client(
        self,
        capabilities_disabled: Capabilities,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        engine: Engine,
    ) -> Iterator[TestClient]:
        app = FastAPI()
        router = build_signup_router(
            mailer=mailer,
            throttle=throttle,
            capabilities=capabilities_disabled,
            base_url="https://crew.day",
            settings=settings,
        )
        app.include_router(router, prefix="/api/v1")

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

        def _db() -> Iterator[Session]:
            with factory() as s:
                yield s

        app.dependency_overrides[db_session] = _db
        with TestClient(app) as c:
            yield c

    def test_start_returns_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/signup/start",
            json={"email": "x@example.com", "desired_slug": "casa-mia"},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "not_found"

    def test_verify_returns_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/signup/verify", json={"token": "anything"})
        assert r.status_code == 404

    def test_passkey_start_returns_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/signup/passkey/start",
            json={
                "signup_session_id": "01HWAAAAAAAAAAAAAAAAAAAAAA",
                "display_name": "Owner",
            },
        )
        assert r.status_code == 404

    def test_passkey_finish_returns_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/signup/passkey/finish",
            json={
                "signup_session_id": "01HWAAAAAAAAAAAAAAAAAAAAAA",
                "challenge_id": "x",
                "display_name": "Owner",
                "timezone": "UTC",
                "credential": {},
            },
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# HTTP router — error mapping for the start endpoint
# ---------------------------------------------------------------------------


class TestRouterStartErrors:
    @pytest.fixture
    def client(
        self,
        capabilities_enabled: Capabilities,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        engine: Engine,
    ) -> Iterator[TestClient]:
        app = FastAPI()
        router = build_signup_router(
            mailer=mailer,
            throttle=throttle,
            capabilities=capabilities_enabled,
            base_url="https://crew.day",
            settings=settings,
        )
        app.include_router(router, prefix="/api/v1")

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

        def _db() -> Iterator[Session]:
            with factory() as s:
                yield s
                s.commit()

        app.dependency_overrides[db_session] = _db
        with TestClient(app) as c:
            yield c

    def test_reserved_slug_returns_409(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/signup/start",
            json={"email": "a@example.com", "desired_slug": "admin"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "slug_reserved"

    def test_invalid_slug_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/signup/start",
            json={"email": "a@example.com", "desired_slug": "_Bad"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "invalid_slug"

    def test_homoglyph_collision_surfaces_colliding_slug(
        self,
        client: TestClient,
        engine: Engine,
    ) -> None:
        # Seed a workspace the new signup collides with.
        with sessionmaker(bind=engine)() as s:
            s.add(
                Workspace(
                    id="01HWAWSCOL000000000000001A",
                    slug="micasa",
                    name="Mi Casa",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            s.commit()
        r = client.post(
            "/api/v1/signup/start",
            json={"email": "a@example.com", "desired_slug": "rnicasa"},
        )
        assert r.status_code == 409
        body = r.json()["detail"]
        assert body["error"] == "slug_homoglyph_collision"
        assert body["colliding_slug"] == "micasa"


# ---------------------------------------------------------------------------
# Internal: audit rows on verify carry hashes only
# ---------------------------------------------------------------------------


class TestVerifyAudit:
    def test_verify_writes_audit_with_hashes(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        ssn = _start_and_verify(
            session,
            mailer,
            throttle,
            settings,
            capabilities_enabled,
            email="audit-v@example.com",
            desired_slug="casa-audit",
            ip="203.0.113.77",
        )
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "signup.verified")
        ).one()
        diff = audit.diff
        assert isinstance(diff, dict)
        assert len(diff["email_hash"]) == 64
        assert len(diff["ip_hash_at_verify"]) == 64
        assert diff["desired_slug"] == "casa-audit"
        assert "audit-v@example.com" not in str(diff)
        assert "203.0.113.77" not in str(diff)
        # Sanity: the attempt id is on entity_id.
        assert audit.entity_id == ssn.signup_attempt_id


# ---------------------------------------------------------------------------
# Email hash shape — parity with magic-link
# ---------------------------------------------------------------------------


class TestEmailHashParity:
    def test_signup_and_magic_link_hash_identically(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        capabilities_enabled: Capabilities,
    ) -> None:
        """PII minimisation: the two tables carry the same email hash
        so abuse-correlation joins are trivial."""
        signup.start_signup(
            session,
            email="parity@example.com",
            desired_slug="casa-parity",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            throttle=throttle,
            capabilities=capabilities_enabled,
            now=_PINNED,
            settings=settings,
        )
        attempt = session.scalars(select(SignupAttempt)).one()
        nonce = session.scalars(select(MagicLinkNonce)).one()
        assert attempt.email_hash == nonce.created_email_hash

        # And the shape matches sha256 hex.
        from app.auth.keys import derive_subkey

        pepper = derive_subkey(settings.root_key, purpose="magic-link")
        expected = hashlib.sha256()
        expected.update(b"parity@example.com")
        expected.update(pepper)
        assert attempt.email_hash == expected.hexdigest()
