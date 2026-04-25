"""Integration tests for ``GET /api/v1/invites/{token}`` + the plural accept.

Exercises :func:`app.api.v1.auth.invite.build_invites_router` end-to-end
against a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``). FastAPI :class:`TestClient` drives the
router exactly as the SPA would; every assertion reads from the same
DB the router writes against.

Coverage:

* **Happy paths.** New-user invite returns ``kind="new_user"``;
  existing-user invite (passkey on file) returns ``kind="existing_user"``;
  the response carries inviter, workspace, grants, expiry.
* **Read-only invariant.** A successful introspect leaves the magic-link
  nonce redeemable — the subsequent ``POST /invites/{token}/accept``
  succeeds.
* **Error mapping.** Bad token / expired token / already-consumed token
  all collapse onto 404 ``invite_not_found`` so the introspect endpoint
  cannot be used as a token-validity oracle.
* **Throttle.** A locked-out IP can neither introspect nor accept —
  both surfaces share the bucket.
* **URL-shape parity.** ``POST /invites/{token}/accept`` returns the
  same body shape as the legacy ``POST /invite/accept`` with the token
  in the body.

See ``docs/specs/12-rest-api.md`` §"Auth" and
``docs/specs/03-auth-and-tokens.md`` §"Additional users".
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

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import (
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import invite as invite_module
from app.auth import magic_link
from app.auth._throttle import Throttle
from app.config import Settings
from app.tenancy import registry, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


# Anchored slightly behind real wall-clock so the magic-link token minted
# by ``_seed_invite`` (TTL 24h) still has a valid ``exp`` claim when the
# handler resolves it via :class:`SystemClock`. A fixed past instant
# bit-rots the moment it falls outside that window — see cd-7920.
_PINNED = datetime.now(tz=UTC) - timedelta(hours=1)
_BASE_URL = "https://crew.day"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        root_key=SecretStr("integration-invite-introspect-root-key-0123456"),
        public_url=_BASE_URL,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register workspace-scoped tables this module reads."""
    registry.register("invite")
    registry.register("audit_log")
    registry.register("user_workspace")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("role_grant")


@pytest.fixture
def throttle() -> Throttle:
    """Per-test throttle so brute-force buckets do not leak between cases."""
    return Throttle()


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    throttle: Throttle,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounting both invite routers.

    Both routers share ``throttle`` — the production factory does the
    same so brute-force probes against either surface trip the same
    consume-failure lockout.
    """
    app = FastAPI()
    app.include_router(
        invite_module.build_invite_router(throttle=throttle, settings=settings),
        prefix="/api/v1",
    )
    app.include_router(
        invite_module.build_invites_router(throttle=throttle, settings=settings),
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

    with TestClient(app, base_url="https://testserver") as c:
        yield c

    # Sweep committed rows so sibling tests see a clean slate.
    with session_factory() as s:
        for model in (
            AuditLog,
            MagicLinkNonce,
            Invite,
            PasskeyCredential,
            User,
        ):
            with tenant_agnostic():
                for row in s.scalars(select(model)).all():
                    s.delete(row)
        s.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_workspace_with_owner(
    session_factory: sessionmaker[Session],
    *,
    slug: str,
    owner_email: str,
    owner_display_name: str,
) -> tuple[str, str, str]:
    """Seed an owner + workspace and return ``(workspace_id, owner_id, slug)``."""
    with session_factory() as s:
        owner = bootstrap_user(
            s,
            email=owner_email,
            display_name=owner_display_name,
            clock=FrozenClock(_PINNED),
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name=f"WS {slug}",
            owner_user_id=owner.id,
            clock=FrozenClock(_PINNED),
        )
        s.commit()
        return ws.id, owner.id, ws.slug


def _seed_invite(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    inviter_id: str,
    invitee_email: str,
    invitee_display_name: str,
    seed_passkey: bool = False,
    pre_seed_user: bool = False,
) -> tuple[str, str, str]:
    """Insert an :class:`Invite` row + nonce; return ``(invite_id, token, user_id)``."""
    with session_factory() as s:
        invitee_id: str | None = None
        if pre_seed_user:
            invitee = bootstrap_user(
                s,
                email=invitee_email,
                display_name=invitee_display_name,
                clock=FrozenClock(_PINNED),
            )
            invitee_id = invitee.id
        else:
            invitee_id = new_ulid()
            with tenant_agnostic():
                s.add(
                    User(
                        id=invitee_id,
                        email=invitee_email,
                        email_lower=canonicalise_email(invitee_email),
                        display_name=invitee_display_name,
                        created_at=_PINNED,
                    )
                )
                s.flush()

        if seed_passkey:
            with tenant_agnostic():
                s.add(
                    PasskeyCredential(
                        id=f"pk-{invitee_id}".encode(),
                        user_id=invitee_id,
                        public_key=b"test-public-key",
                        sign_count=0,
                        transports=None,
                        backup_eligible=False,
                        label="test passkey",
                        created_at=_PINNED,
                        last_used_at=None,
                    )
                )
                s.flush()

        invite_id = new_ulid()
        invite_row = Invite(
            id=invite_id,
            workspace_id=workspace_id,
            user_id=invitee_id,
            pending_email=canonicalise_email(invitee_email),
            pending_email_lower=canonicalise_email(invitee_email),
            email_hash="test-email-hash",
            display_name=invitee_display_name,
            state="pending",
            grants_json=[
                {
                    "scope_kind": "workspace",
                    "scope_id": workspace_id,
                    "grant_role": "worker",
                }
            ],
            group_memberships_json=[],
            invited_by_user_id=inviter_id,
            created_at=_PINNED,
            expires_at=_PINNED + timedelta(hours=24),
            accepted_at=None,
            revoked_at=None,
        )
        with tenant_agnostic():
            s.add(invite_row)
            s.flush()

        url = magic_link.request_link(
            s,
            email=invitee_email,
            purpose="grant_invite",
            ip="127.0.0.1",
            mailer=None,
            base_url=_BASE_URL,
            now=_PINNED,
            ttl=timedelta(hours=24),
            throttle=Throttle(),
            settings=Settings(
                root_key=SecretStr("integration-invite-introspect-root-key-0123456"),
                public_url=_BASE_URL,
            ),
            clock=FrozenClock(_PINNED),
            subject_id=invite_id,
            send_email=False,
        )
        s.commit()
        assert url is not None
        token = url.rsplit("/", 1)[-1]
        return invite_id, token, invitee_id


def _expire_token(session_factory: sessionmaker[Session], *, token: str) -> None:
    """Backdate the magic-link row's ``expires_at`` so the token is expired.

    Editing the row instead of waiting on real time keeps the test
    deterministic without monkey-patching the SystemClock seam.
    """
    # Decode the token to find the ``jti``.
    with session_factory() as s:
        # Decoding via the magic-link helper avoids re-implementing the
        # itsdangerous unseal here; the helper is best-effort and never
        # throws.
        cfg = Settings(
            root_key=SecretStr("integration-invite-introspect-root-key-0123456"),
            public_url=_BASE_URL,
        )
        jti, _purpose = magic_link._best_effort_unseal(token, settings=cfg)
        assert jti is not None
        with tenant_agnostic():
            row = s.get(MagicLinkNonce, jti)
            assert row is not None
            row.expires_at = _PINNED - timedelta(hours=1)
        s.commit()


def _consume_token_directly(
    session_factory: sessionmaker[Session], *, token: str
) -> None:
    """Burn the magic-link nonce out-of-band to simulate a "consumed" state."""
    with session_factory() as s:
        cfg = Settings(
            root_key=SecretStr("integration-invite-introspect-root-key-0123456"),
            public_url=_BASE_URL,
        )
        magic_link.consume_link(
            s,
            token=token,
            expected_purpose="grant_invite",
            ip="127.0.0.1",
            now=_PINNED,
            throttle=Throttle(),
            settings=cfg,
        )
        s.commit()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestIntrospectHappyPath:
    """``GET /invites/{token}`` returns the Accept-card preview."""

    def test_new_user_invite_returns_new_user_kind(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        ws_id, owner_id, slug = _seed_workspace_with_owner(
            session_factory,
            slug="acme-new",
            owner_email="owner-new@acme.test",
            owner_display_name="Acme Owner",
        )
        invite_id, token, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice@example.com",
            invitee_display_name="Alice",
        )

        resp = client.get(f"/api/v1/invites/{token}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "new_user"
        assert body["invite_id"] == invite_id
        assert body["workspace_id"] == ws_id
        assert body["workspace_slug"] == slug
        assert body["inviter_display_name"] == "Acme Owner"
        assert body["email_lower"] == "alice@example.com"
        assert body["grants"] == [
            {
                "scope_kind": "workspace",
                "scope_id": ws_id,
                "grant_role": "worker",
            }
        ]
        assert body["permission_group_memberships"] == []

    def test_existing_user_invite_returns_existing_user_kind(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        ws_id, owner_id, _slug = _seed_workspace_with_owner(
            session_factory,
            slug="acme-existing",
            owner_email="owner-existing@acme.test",
            owner_display_name="Owner",
        )
        _invite_id, token, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="bob@example.com",
            invitee_display_name="Bob",
            seed_passkey=True,
            pre_seed_user=True,
        )

        resp = client.get(f"/api/v1/invites/{token}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "existing_user"


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


class TestReadOnlyInvariant:
    """Introspect does not burn the underlying nonce."""

    def test_introspect_then_accept_succeeds(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        ws_id, owner_id, _slug = _seed_workspace_with_owner(
            session_factory,
            slug="readonly",
            owner_email="owner-ro@acme.test",
            owner_display_name="Owner",
        )
        invite_id, token, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice-ro@example.com",
            invitee_display_name="Alice",
        )

        # Introspect first.
        peek = client.get(f"/api/v1/invites/{token}")
        assert peek.status_code == 200, peek.text

        # Same token still consumes via the plural accept endpoint.
        accept = client.post(f"/api/v1/invites/{token}/accept")
        assert accept.status_code == 200, accept.text
        body = accept.json()
        assert body["kind"] == "new_user"
        assert body["invite_id"] == invite_id


# ---------------------------------------------------------------------------
# Error mapping (existence-leak guard)
# ---------------------------------------------------------------------------


class TestIntrospectErrorsCollapseTo404:
    """Token-validity errors all surface as 404 ``invite_not_found``."""

    def test_bad_token_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/invites/garbage.not.a.valid.token")
        assert resp.status_code == 404
        # The 404 envelope is wrapped by FastAPI's default exception
        # handler when no problem+json seam is installed; the body still
        # carries our ``invite_not_found`` symbol.
        assert "invite_not_found" in resp.text

    def test_expired_token_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        ws_id, owner_id, _slug = _seed_workspace_with_owner(
            session_factory,
            slug="expiry",
            owner_email="owner-exp@acme.test",
            owner_display_name="Owner",
        )
        _invite_id, token, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice-exp@example.com",
            invitee_display_name="Alice",
        )
        _expire_token(session_factory, token=token)

        resp = client.get(f"/api/v1/invites/{token}")
        assert resp.status_code == 404
        assert "invite_not_found" in resp.text

    def test_consumed_token_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        ws_id, owner_id, _slug = _seed_workspace_with_owner(
            session_factory,
            slug="consumed",
            owner_email="owner-cs@acme.test",
            owner_display_name="Owner",
        )
        _invite_id, token, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice-cs@example.com",
            invitee_display_name="Alice",
        )
        _consume_token_directly(session_factory, token=token)

        resp = client.get(f"/api/v1/invites/{token}")
        assert resp.status_code == 404
        assert "invite_not_found" in resp.text


# ---------------------------------------------------------------------------
# Throttle integration
# ---------------------------------------------------------------------------


class TestThrottleSharedWithAccept:
    """Locked-out IP cannot peek — both surfaces share the bucket.

    Spec §15 "Rate limiting and abuse controls": once a per-IP
    lockout is set (3 consume-fails / 60s on the magic-link
    surface), every consume-style call from that IP is refused
    until the lockout window lapses. ``peek_link`` gates on the
    same :meth:`Throttle.check_consume_allowed` predicate, so an
    attacker cannot bypass an active lockout by switching to the
    introspect endpoint.

    We seed the lockout state directly on the shared throttle so
    the test stays decoupled from whichever router is responsible
    for bumping the counter (today: only the magic-link consume
    router does — invite-side bumps land with the cd-7huk
    migration). The shared-bucket assertion is the load-bearing
    piece for this task.
    """

    def test_locked_out_ip_cannot_introspect(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        throttle: Throttle,
    ) -> None:
        ws_id, owner_id, _slug = _seed_workspace_with_owner(
            session_factory,
            slug="lockout",
            owner_email="owner-lk@acme.test",
            owner_display_name="Owner",
        )
        _invite_id, token, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice-lk@example.com",
            invitee_display_name="Alice",
        )

        # Trip the per-IP lockout directly on the shared throttle.
        # The :class:`TestClient` uses ``testclient`` as the source
        # IP by default; recording three failures against that key
        # flips the lockout for both routers.
        for _ in range(3):
            throttle.record_consume_failure(ip="testclient", now=datetime.now(tz=UTC))

        # Both surfaces refuse — same throttle bucket.
        peek = client.get(f"/api/v1/invites/{token}")
        accept = client.post(f"/api/v1/invites/{token}/accept")

        assert peek.status_code == 429, peek.text
        assert "consume_locked_out" in peek.text
        assert accept.status_code == 429, accept.text
        assert "consume_locked_out" in accept.text


# ---------------------------------------------------------------------------
# URL-shape parity
# ---------------------------------------------------------------------------


class TestPluralAcceptParity:
    """``POST /invites/{token}/accept`` matches the legacy body-carried shape."""

    def test_plural_accept_matches_singular_body(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        ws_id, owner_id, _slug = _seed_workspace_with_owner(
            session_factory,
            slug="parity",
            owner_email="owner-par@acme.test",
            owner_display_name="Owner",
        )
        # Two invites — one each for the singular and plural surfaces.
        invite_a, token_a, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice-par-a@example.com",
            invitee_display_name="Alice A",
        )
        invite_b, token_b, _ = _seed_invite(
            session_factory,
            workspace_id=ws_id,
            inviter_id=owner_id,
            invitee_email="alice-par-b@example.com",
            invitee_display_name="Alice B",
        )

        # Plural surface: token in path.
        plural = client.post(f"/api/v1/invites/{token_a}/accept")
        assert plural.status_code == 200, plural.text
        plural_body = plural.json()

        # Singular surface: token in body (legacy shape).
        singular = client.post(
            "/api/v1/invite/accept",
            json={"token": token_b},
        )
        assert singular.status_code == 200, singular.text
        singular_body = singular.json()

        # Both surfaces return the same envelope keys + ``kind``.
        assert plural_body["kind"] == singular_body["kind"] == "new_user"
        assert plural_body["invite_id"] == invite_a
        assert singular_body["invite_id"] == invite_b
        assert set(plural_body.keys()) == set(singular_body.keys())
