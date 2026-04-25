"""Unit tests for the bare-host ``/api/v1/{me/email,auth/email}`` router.

Exercises :mod:`app.api.v1.auth.email_change` (cd-601a) against a
minimal FastAPI instance — no factory, no tenancy middleware, no
CSRF middleware. The integration suite carries the full stack;
this file owns the per-route behaviour of the three email-change
endpoints + the dep-edge auth posture that rejects PATs / agent /
delegated tokens with ``403 forbidden``.

Coverage (cd-601a acceptance criteria):

* ``POST /me/email/change_request`` happy path with a passkey
  session — pending row lands, magic link is mailed to the new
  address, notice mailed to the old address, audit row written.
* ``POST /me/email/change_request`` refuses any
  ``Authorization: Bearer …`` header (PAT / delegated / agent) with
  ``403 forbidden`` — even if a session cookie is also present.
* ``POST /me/email/change_request`` refuses an unknown / expired
  session cookie with ``401 session_invalid``.
* ``POST /me/email/change_request`` returns ``422 invalid_email`` on
  syntactically broken addresses.
* ``POST /me/email/change_request`` returns ``409 email_in_use`` when
  another user already holds the address.
* ``POST /me/email/change_request`` returns ``409 recent_reenrollment``
  when the caller's newest passkey is younger than the cool-off.
* ``POST /auth/email/verify`` happy path — token consumes, email
  swap lands on the User row, revert link mails to the old address,
  audit row written.
* ``POST /auth/email/verify`` returns ``410 expired`` when the
  consume races (token was already burnt) or the token is expired.
* ``POST /auth/email/verify`` returns ``403 session_user_mismatch``
  when the session belongs to a different user than the token.
* ``POST /auth/email/revert`` happy path — token consumes, User.email
  is restored, audit row written.
* ``POST /auth/email/revert`` returns ``410 expired`` for a token
  whose 72-hour TTL has lapsed.
* All three routes refuse Bearer tokens at the dep edge.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change"
and ``docs/specs/12-rest-api.md`` §"Auth".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

# Pull in the workspace + authz model packages so :data:`Base.metadata`
# resolves every FK the identity tables reference.
from app.adapters.db import audit, authz, workspace  # noqa: F401
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    EmailChangePending,
    PasskeyCredential,
    User,
)
from app.adapters.db.session import make_engine
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import email_change as email_change_module
from app.auth._throttle import Throttle
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer

# Pinned UA / Accept-Language so the :func:`validate` fingerprint gate
# agrees with the seed :func:`issue` call.
_TEST_UA: str = "pytest-email-change"
_TEST_ACCEPT_LANGUAGE: str = "en"
_BASE_URL: str = "https://crew.day"
_PINNED: datetime = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Pin :class:`Settings` to in-memory SQLite + a fixed root key."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-email-change-root-key-0123456789"),
        public_url=_BASE_URL,
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every model's table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> str:
    """Seed a :class:`User` and return its id.

    The User row is flushed before the :class:`PasskeyCredential`
    insert because SQLAlchemy's unit-of-work doesn't reorder
    siblings to satisfy a FK on a non-relationship-attribute
    column — explicit flush ordering is the simplest fix here.
    """
    user_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            User(
                id=user_id,
                email="alice.old@example.com",
                email_lower="alice.old@example.com",
                display_name="Alice",
                created_at=_PINNED - timedelta(days=30),
            )
        )
        s.flush()
        # Seed a passkey older than the 15-minute cool-off so the
        # change_request happy path is not blocked.
        s.add(
            PasskeyCredential(
                id=f"pk-{user_id}".encode(),
                user_id=user_id,
                public_key=b"fake-pubkey",
                sign_count=0,
                transports=None,
                backup_eligible=False,
                label="primary",
                created_at=_PINNED - timedelta(days=30),
                last_used_at=None,
            )
        )
        s.commit()
    return user_id


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture(autouse=True)
def _redirect_default_uow_to_test_engine(
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[None]:
    """Redirect ``make_uow`` to the per-test engine.

    The cd-9slq commit-before-send shape on
    ``POST /me/email/change_request`` and ``POST /auth/email/verify``
    opens its own ``with make_uow() as session:`` block so the SMTP
    sends fire post-commit. ``make_uow`` reads the module-level
    default sessionmaker — without this redirect the router's UoW
    would bind to whatever DB the default factory was last built for
    instead of the per-test in-memory engine. Mirrors
    :func:`tests.unit.auth.test_recovery.redirect_default_engine`.
    """
    import app.adapters.db.session as _session_mod

    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = session_factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    mailer: InMemoryMailer,
    throttle: Throttle,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Mount the email-change router on a minimal FastAPI app."""
    # Pin the magic-link + session pepper to the same Settings.
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
    monkeypatch.setattr("app.auth.magic_link.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.domain.identity.email_change.get_settings", lambda: settings
    )

    app = FastAPI()
    app.include_router(
        email_change_module.build_email_change_router(
            mailer=mailer,
            throttle=throttle,
            settings=settings,
        ),
        prefix="/api/v1",
    )

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


def _issue_cookie(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a live session and return the raw cookie value."""
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def _extract_token_from_body(body: str, *, base_url: str = _BASE_URL) -> str:
    """Pull the magic-link token out of a rendered mail body.

    The magic-link template carries the URL on its own line; the
    revert template embeds the token in a ``?token=…`` query string.
    Both shapes flow through here.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(base_url):
            if "?token=" in stripped:
                return stripped.split("?token=", 1)[1]
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"no URL in body: {body!r}")


# ---------------------------------------------------------------------------
# /me/email/change_request — happy path
# ---------------------------------------------------------------------------


class TestChangeRequestHappyPath:
    """``POST /me/email/change_request`` with a passkey session."""

    def test_change_request_lands_pending_row_and_two_mails(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """Spec §03 step 4 + 5: magic link to new, notice to old."""
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.NEW@example.com"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "accepted"
        pending_id = body["pending_id"]
        assert isinstance(pending_id, str) and len(pending_id) > 0

        # Pending row landed with both addresses snapshotted.
        with session_factory() as s, tenant_agnostic():
            row = s.get(EmailChangePending, pending_id)
            assert row is not None
            assert row.user_id == seed_user
            # ``new_email`` keeps the user-typed casing for display;
            # ``new_email_lower`` is canonicalised.
            assert row.new_email == "alice.NEW@example.com"
            assert row.new_email_lower == "alice.new@example.com"
            assert row.previous_email == "alice.old@example.com"
            assert row.verified_at is None
            assert row.reverted_at is None

        # Two mails — magic link to new, notice to old.
        assert len(mailer.sent) == 2
        new_mail = next(m for m in mailer.sent if "alice.NEW@example.com" in m.to)
        old_mail = next(m for m in mailer.sent if "alice.old@example.com" in m.to)
        # The magic-link body carries the canonical
        # ``{base}/auth/magic/<token>`` URL; the notice does NOT
        # carry any link.
        assert "/auth/magic/" in new_mail.body_text
        assert "/auth/magic/" not in old_mail.body_text
        # Old-address notice masks the new email and never echoes
        # the full address.
        assert "alice.NEW@example.com" not in old_mail.body_text
        # ``a***@example.com`` mask shape.
        assert "a***@example.com" in old_mail.body_text

    def test_change_request_writes_audit_row(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """``email.change_requested`` row lands with hashed PII only."""
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.new@example.com"},
        )
        assert r.status_code == 200, r.text

        with session_factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "email.change_requested",
                        AuditLog.entity_id == seed_user,
                    )
                ).all()
            )
        assert len(rows) == 1
        diff = rows[0].diff
        assert isinstance(diff, dict)
        assert diff["user_id"] == seed_user
        # Hashed forms — never the plaintext.
        assert "old_email_hash" in diff
        assert "new_email_hash" in diff
        assert "ip_hash" in diff
        # Hashes are 64-char hex digests.
        assert len(diff["old_email_hash"]) == 64
        assert len(diff["new_email_hash"]) == 64
        # Plaintext addresses must NOT appear in the diff (the
        # redactor would catch them anyway, but belt-and-braces).
        assert "alice.old@example.com" not in str(diff)
        assert "alice.new@example.com" not in str(diff)


# ---------------------------------------------------------------------------
# /me/email/change_request — auth posture (passkey session only)
# ---------------------------------------------------------------------------


class TestChangeRequestAuthPosture:
    """Bearer tokens — PAT, delegated, agent — are refused at dep edge."""

    def test_change_request_refuses_bearer_with_no_cookie(
        self,
        client: TestClient,
    ) -> None:
        """No cookie + a Bearer header → 403 forbidden, not 401."""
        # Deliberately inject a forged Bearer header. The router
        # never validates the token because the dep-edge gate fires
        # first — even a syntactically-bogus value reaches the gate.
        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.new@example.com"},
            headers={"Authorization": "Bearer bogus-pat-token"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "forbidden"

    def test_change_request_refuses_bearer_even_with_valid_cookie(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """Spec posture: any Bearer header refuses, cookie is irrelevant."""
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.new@example.com"},
            headers={"Authorization": "Bearer mit_some_personal_token"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "forbidden"

    def test_change_request_no_session_is_401(self, client: TestClient) -> None:
        """No session cookie + no Bearer → 401 session_required."""
        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.new@example.com"},
        )
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"

    def test_change_request_invalid_session_is_401(
        self,
        client: TestClient,
    ) -> None:
        """Cookie that does not resolve → 401 session_invalid."""
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-cookie")
        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.new@example.com"},
        )
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_invalid"


# ---------------------------------------------------------------------------
# /me/email/change_request — validation + uniqueness + reenrollment
# ---------------------------------------------------------------------------


class TestChangeRequestValidation:
    """Email syntax / uniqueness / re-enrollment guard rejection paths."""

    @pytest.mark.parametrize(
        "bad_email",
        [
            "no-at-sign",
            "@no-local",
            "no-domain@",
            "spa ce@example.com",
        ],
    )
    def test_invalid_email_is_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        bad_email: str,
    ) -> None:
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": bad_email},
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_email"

    def test_email_in_use_is_409(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """Another user holding the address → 409 email_in_use."""
        # Seed a second user with the conflicting address.
        with session_factory() as s, tenant_agnostic():
            s.add(
                User(
                    id=new_ulid(),
                    email="taken@example.com",
                    email_lower="taken@example.com",
                    display_name="Other",
                    created_at=_PINNED - timedelta(days=10),
                )
            )
            s.commit()

        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "TAKEN@example.com"},  # case-insensitive
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "email_in_use"

    def test_recent_reenrollment_is_409(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """A passkey under 15 minutes old → 409 recent_reenrollment.

        Spec §15 "Recent re-enrollment cool-off" bounds the
        post-recovery hijack window. The seed_user fixture stamps an
        old passkey; we add a fresh one here to trip the gate.
        """
        # Seed a fresh passkey to trip the cool-off gate. The
        # service compares the most-recent ``created_at`` against
        # ``now - 15min``; the production clock will be newer than
        # ``_PINNED + 5min`` by the time the request fires, so a
        # ``created_at`` of "now" trips the gate.
        from app.util.clock import SystemClock

        now = SystemClock().now()
        with session_factory() as s, tenant_agnostic():
            s.add(
                PasskeyCredential(
                    id=b"recent-passkey",
                    user_id=seed_user,
                    public_key=b"fresh",
                    sign_count=0,
                    transports=None,
                    backup_eligible=False,
                    label="recently re-enrolled",
                    created_at=now - timedelta(minutes=2),
                    last_used_at=None,
                )
            )
            s.commit()

        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/email/change_request",
            json={"new_email": "alice.new@example.com"},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "recent_reenrollment"


# ---------------------------------------------------------------------------
# /auth/email/verify — happy path + rejections
# ---------------------------------------------------------------------------


def _request_change(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    user_id: str,
    new_email: str,
) -> str:
    """Drive ``change_request`` and return the magic-link token."""
    cookie_value = _issue_cookie(session_factory, user_id=user_id, settings=settings)
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
    r = client.post(
        "/api/v1/me/email/change_request",
        json={"new_email": new_email},
    )
    assert r.status_code == 200, r.text
    return cookie_value


class TestVerifyHappyPath:
    """``POST /auth/email/verify`` consumes the token and swaps email."""

    def test_verify_swaps_user_email_and_mints_revert(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        cookie_value = _request_change(
            client,
            session_factory,
            settings,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )

        # Pull the magic-link token out of the new-address mail.
        new_mail = next(m for m in mailer.sent if "alice.new@example.com" in m.to)
        token = _extract_token_from_body(new_mail.body_text)

        # Reset the mailer log so revert + confirmation mails are
        # the only ones we assert about.
        mailer.sent.clear()

        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "verified"
        assert body["user_id"] == seed_user

        # User row swapped.
        with session_factory() as s, tenant_agnostic():
            user = s.get(User, seed_user)
            assert user is not None
            assert user.email == "alice.new@example.com"
            assert user.email_lower == "alice.new@example.com"

            pending = s.scalars(
                select(EmailChangePending).where(
                    EmailChangePending.user_id == seed_user
                )
            ).first()
            assert pending is not None
            assert pending.verified_at is not None
            assert pending.revert_jti is not None
            assert pending.revert_expires_at is not None

        # Two mails: confirmation to new + revert link to old.
        assert len(mailer.sent) == 2
        new_confirm = next(m for m in mailer.sent if "alice.new@example.com" in m.to)
        old_revert = next(m for m in mailer.sent if "alice.old@example.com" in m.to)
        assert "?token=" in old_revert.body_text  # revert link present
        assert "?token=" not in new_confirm.body_text  # confirmation has none

    def test_verify_writes_audit_row(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """``email.change_verified`` row lands with hashed PII only."""
        cookie_value = _request_change(
            client,
            session_factory,
            settings,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        token = _extract_token_from_body(
            next(m for m in mailer.sent if "alice.new@example.com" in m.to).body_text
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 200, r.text

        with session_factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "email.change_verified",
                        AuditLog.entity_id == seed_user,
                    )
                ).all()
            )
        assert len(rows) == 1
        diff = rows[0].diff
        assert isinstance(diff, dict)
        assert diff["user_id"] == seed_user
        assert len(diff["old_email_hash"]) == 64
        assert len(diff["new_email_hash"]) == 64
        assert "request_jti" in diff
        assert "revert_jti" in diff


class TestVerifyRejections:
    """Verify failure paths."""

    def test_verify_invalid_token_is_400(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/auth/email/verify",
            json={"token": "garbage-token-not-signed"},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "invalid_token"

    def test_verify_already_consumed_is_409(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """A second consume of the same token → 409 already_consumed."""
        cookie_value = _request_change(
            client,
            session_factory,
            settings,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        token = _extract_token_from_body(
            next(m for m in mailer.sent if "alice.new@example.com" in m.to).body_text
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        # First verify wins.
        r1 = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r1.status_code == 200, r1.text
        # Second verify hits the already-consumed nonce.
        r2 = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"]["error"] == "already_consumed"

    def test_verify_session_user_mismatch_is_403(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """Spec §03 step 2: verify requires the SAME user's session.

        We mint the change_request from one user but verify from
        another user's session.
        """
        # Seed a second user.
        other_user_id = new_ulid()
        with session_factory() as s, tenant_agnostic():
            s.add(
                User(
                    id=other_user_id,
                    email="bob@example.com",
                    email_lower="bob@example.com",
                    display_name="Bob",
                    created_at=_PINNED - timedelta(days=30),
                )
            )
            s.flush()
            s.add(
                PasskeyCredential(
                    id=b"bob-pk",
                    user_id=other_user_id,
                    public_key=b"bob-pubkey",
                    sign_count=0,
                    transports=None,
                    backup_eligible=False,
                    label="bob-key",
                    created_at=_PINNED - timedelta(days=30),
                    last_used_at=None,
                )
            )
            s.commit()

        # Drive the change_request from alice (seed_user).
        _request_change(
            client,
            session_factory,
            settings,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        token = _extract_token_from_body(
            next(m for m in mailer.sent if "alice.new@example.com" in m.to).body_text
        )

        # Now switch the session cookie to bob's session.
        bob_cookie = _issue_cookie(
            session_factory, user_id=other_user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, bob_cookie)
        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "session_user_mismatch"

        # The mismatched-session attempt MUST NOT burn the magic-link
        # nonce — otherwise an attacker holding any session can DoS
        # the legit user's swap by submitting the phished link from
        # their own session. Re-attempt from alice's (correct) session
        # and assert the verify still lands.
        alice_cookie = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, alice_cookie)
        r2 = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r2.status_code == 200, r2.text
        assert r2.json()["status"] == "verified"

    def test_verify_no_session_is_401(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """No session cookie → 401 session_required."""
        _request_change(
            client,
            session_factory,
            settings,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        token = _extract_token_from_body(
            next(m for m in mailer.sent if "alice.new@example.com" in m.to).body_text
        )

        # Drop the cookie and retry — this is the "user clicked the
        # link in a signed-out browser" branch (which the spec
        # routes through the SPA's passkey-sign-in flow). The
        # router enforces session presence directly.
        client.cookies.clear()
        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"

    def test_verify_refuses_bearer_token(
        self,
        client: TestClient,
    ) -> None:
        """Verify also refuses Bearer headers — same posture as request."""
        r = client.post(
            "/api/v1/auth/email/verify",
            json={"token": "anything"},
            headers={"Authorization": "Bearer pat-token"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "forbidden"

    def test_verify_expired_token_is_410(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """A confirm token whose 15-min TTL lapsed → 410 expired.

        The verify route shares the same persisted-TTL gate as the
        revert route; we hand-flip the underlying nonce row's
        ``expires_at`` to the past and expect the consume step to
        trip with :class:`TokenExpired`.
        """
        from app.adapters.db.identity.models import MagicLinkNonce
        from app.auth.magic_link import inspect_token_jti

        cookie_value = _request_change(
            client,
            session_factory,
            settings,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        token = _extract_token_from_body(
            next(m for m in mailer.sent if "alice.new@example.com" in m.to).body_text
        )

        # Lapse the persisted TTL. The signed-payload ``exp`` is still
        # in the future (15 minutes from now), so this isolates the
        # row-level expiry gate.
        request_jti = inspect_token_jti(token, settings=settings)
        with session_factory() as s, tenant_agnostic():
            row = s.get(MagicLinkNonce, request_jti)
            assert row is not None
            row.expires_at = _PINNED - timedelta(days=10)
            s.commit()

        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 410, r.text
        assert r.json()["detail"]["error"] == "expired"


# ---------------------------------------------------------------------------
# /auth/email/revert — happy path + rejections
# ---------------------------------------------------------------------------


def _drive_full_change(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
    mailer: InMemoryMailer,
    *,
    user_id: str,
    new_email: str,
) -> str:
    """Run change_request + verify; return the revert token."""
    _request_change(
        client,
        session_factory,
        settings,
        user_id=user_id,
        new_email=new_email,
    )
    confirm_token = _extract_token_from_body(
        next(m for m in mailer.sent if new_email in m.to).body_text
    )

    # Reset mail log to isolate revert-side mails.
    mailer.sent.clear()

    cookie_value = _issue_cookie(session_factory, user_id=user_id, settings=settings)
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
    r = client.post("/api/v1/auth/email/verify", json={"token": confirm_token})
    assert r.status_code == 200, r.text

    revert_mail = next(m for m in mailer.sent if "alice.old@example.com" in m.to)
    return _extract_token_from_body(revert_mail.body_text)


class TestRevertHappyPath:
    """``POST /auth/email/revert`` restores the previous email."""

    def test_revert_restores_user_email(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        revert_token = _drive_full_change(
            client,
            session_factory,
            settings,
            mailer,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )

        # No session needed — the spec pins the revert as a non-auth
        # primitive consumed against the old address by virtue of
        # mailbox control.
        client.cookies.clear()
        r = client.post("/api/v1/auth/email/revert", json={"token": revert_token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "reverted"
        assert body["user_id"] == seed_user

        with session_factory() as s, tenant_agnostic():
            user = s.get(User, seed_user)
            assert user is not None
            # Restored to the snapshot.
            assert user.email == "alice.old@example.com"
            assert user.email_lower == "alice.old@example.com"

            pending = s.scalars(
                select(EmailChangePending).where(
                    EmailChangePending.user_id == seed_user
                )
            ).first()
            assert pending is not None
            assert pending.reverted_at is not None

    def test_revert_writes_audit_row(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        revert_token = _drive_full_change(
            client,
            session_factory,
            settings,
            mailer,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        client.cookies.clear()
        r = client.post("/api/v1/auth/email/revert", json={"token": revert_token})
        assert r.status_code == 200, r.text

        with session_factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "email.change_reverted",
                        AuditLog.entity_id == seed_user,
                    )
                ).all()
            )
        assert len(rows) == 1
        diff = rows[0].diff
        assert isinstance(diff, dict)
        assert diff["user_id"] == seed_user
        assert len(diff["old_email_hash"]) == 64
        assert len(diff["new_email_hash"]) == 64
        assert "revert_jti" in diff


class TestRevertRejections:
    """Revert failure paths."""

    def test_revert_invalid_token_is_400(
        self,
        client: TestClient,
    ) -> None:
        r = client.post(
            "/api/v1/auth/email/revert",
            json={"token": "garbage-not-signed"},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "invalid_token"

    def test_revert_already_consumed_is_409(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """Second consume of the same revert token → 409 already_consumed."""
        revert_token = _drive_full_change(
            client,
            session_factory,
            settings,
            mailer,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        client.cookies.clear()
        r1 = client.post("/api/v1/auth/email/revert", json={"token": revert_token})
        assert r1.status_code == 200, r1.text
        r2 = client.post("/api/v1/auth/email/revert", json={"token": revert_token})
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"]["error"] == "already_consumed"

    def test_revert_expired_token_is_410(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """A revert token past its 72h TTL → 410 expired.

        We simulate the lapse by hand-flipping ``expires_at`` on the
        underlying :class:`MagicLinkNonce` row to a past instant. The
        consume gate then trips on persisted-TTL.
        """
        from app.adapters.db.identity.models import MagicLinkNonce

        revert_token = _drive_full_change(
            client,
            session_factory,
            settings,
            mailer,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )

        # Flip the nonce row's TTL to "yesterday" so the consume
        # trips the expiry gate.
        from app.auth.magic_link import inspect_token_jti

        revert_jti = inspect_token_jti(revert_token, settings=settings)
        with session_factory() as s, tenant_agnostic():
            row = s.get(MagicLinkNonce, revert_jti)
            assert row is not None
            row.expires_at = _PINNED - timedelta(days=10)
            s.commit()

        client.cookies.clear()
        r = client.post("/api/v1/auth/email/revert", json={"token": revert_token})
        assert r.status_code == 410, r.text
        assert r.json()["detail"]["error"] == "expired"

    def test_revert_refuses_bearer_token(
        self,
        client: TestClient,
    ) -> None:
        r = client.post(
            "/api/v1/auth/email/revert",
            json={"token": "anything"},
            headers={"Authorization": "Bearer some-token"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"] == "forbidden"


# ---------------------------------------------------------------------------
# Cross-flow / isolation
# ---------------------------------------------------------------------------


class TestCrossFlowIsolation:
    """Bare-host email change does not leak across users."""

    def test_revert_from_unrelated_user_does_not_affect_other(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        mailer: InMemoryMailer,
    ) -> None:
        """A revert against alice's row must not change bob's email.

        This isn't a membership-tenancy scenario (email-change is
        bare-host and identity-scoped) but the equivalent isolation
        is "revert acts on exactly one user". We seed bob, drive
        alice's full change + revert, and assert bob's row is
        untouched.
        """
        # Seed bob with a fresh passkey + a settled email.
        bob_id = new_ulid()
        with session_factory() as s, tenant_agnostic():
            s.add(
                User(
                    id=bob_id,
                    email="bob@example.com",
                    email_lower="bob@example.com",
                    display_name="Bob",
                    created_at=_PINNED - timedelta(days=30),
                )
            )
            s.flush()
            s.add(
                PasskeyCredential(
                    id=b"bob-pk-isolate",
                    user_id=bob_id,
                    public_key=b"bob",
                    sign_count=0,
                    transports=None,
                    backup_eligible=False,
                    label="bob-key",
                    created_at=_PINNED - timedelta(days=30),
                    last_used_at=None,
                )
            )
            s.commit()

        revert_token = _drive_full_change(
            client,
            session_factory,
            settings,
            mailer,
            user_id=seed_user,
            new_email="alice.new@example.com",
        )
        client.cookies.clear()
        r = client.post("/api/v1/auth/email/revert", json={"token": revert_token})
        assert r.status_code == 200, r.text

        with session_factory() as s, tenant_agnostic():
            bob = s.get(User, bob_id)
            assert bob is not None
            assert bob.email == "bob@example.com"  # untouched
