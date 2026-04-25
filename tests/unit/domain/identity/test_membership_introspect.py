"""Unit tests for :func:`app.domain.identity.membership.introspect_invite`.

Drives the read-only invite preview directly against the in-memory
SQLite engine from :mod:`tests.unit.domain.identity.conftest`. The
tests cover the happy paths for new-user / existing-user shapes,
each typed error mode, and the "read-only" invariant — a successful
introspect leaves the underlying magic-link nonce redeemable so the
subsequent ``POST /invites/{token}/accept`` still wins.

Integration-level coverage of the HTTP surface lives in
``tests/integration/api/auth/test_invite_introspect.py``.

See ``docs/specs/12-rest-api.md`` §"Auth" and
``docs/specs/03-auth-and-tokens.md`` §"Additional users".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import (
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    canonicalise_email,
)
from app.adapters.db.workspace.models import Workspace
from app.auth import magic_link
from app.auth._throttle import Throttle
from app.config import Settings
from app.domain.identity import membership
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.unit.domain.identity.conftest import (
    make_user,
    make_workspace,
)

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"

_TEST_SETTINGS = Settings(
    root_key=SecretStr("test-root-key-for-introspect-0123456789abcdef"),
    public_url=_BASE_URL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_passkey(session: Session, *, user_id: str) -> None:
    """Stand in for a completed WebAuthn ceremony.

    A user with at least one registered :class:`PasskeyCredential` is
    classified as ``existing_user`` by :func:`introspect_invite` (and
    ``consume_invite_token``); seeding one row directly is the
    cheapest way to flip that branch in a unit test.
    """
    with tenant_agnostic():
        session.add(
            PasskeyCredential(
                id=f"pk-{user_id}".encode(),
                user_id=user_id,
                public_key=b"test-public-key",
                sign_count=0,
                transports=None,
                backup_eligible=False,
                label="test passkey",
                created_at=_PINNED,
                last_used_at=None,
            )
        )
        session.flush()


def _mint_invite(
    session: Session,
    *,
    workspace: Workspace,
    inviter_id: str,
    invitee_email: str = "alice@example.com",
    invitee_display_name: str = "Alice Example",
    grants: list[dict[str, object]] | None = None,
    invitee_user_id: str | None = None,
) -> tuple[str, str]:
    """Insert an :class:`Invite` row + matching magic-link nonce.

    Returns ``(invite_id, signed_token)`` so the test can call
    :func:`introspect_invite` against a real signed token. Mirrors
    what :func:`membership.invite` does end-to-end but without the
    mailer / audit emission so the unit test stays scoped.
    """
    invite_id = new_ulid()
    user_id = invitee_user_id if invitee_user_id is not None else new_ulid()
    if invitee_user_id is None:
        # Spawn a minimal :class:`User` row for the invitee so the
        # passkey-presence branch can run.
        from app.adapters.db.identity.models import User

        with tenant_agnostic():
            session.add(
                User(
                    id=user_id,
                    email=invitee_email,
                    email_lower=canonicalise_email(invitee_email),
                    display_name=invitee_display_name,
                    created_at=_PINNED,
                )
            )
            session.flush()

    invite = Invite(
        id=invite_id,
        workspace_id=workspace.id,
        user_id=user_id,
        pending_email=canonicalise_email(invitee_email),
        pending_email_lower=canonicalise_email(invitee_email),
        email_hash="hash-placeholder",
        display_name=invitee_display_name,
        state="pending",
        grants_json=list(
            grants
            if grants is not None
            else [
                {
                    "scope_kind": "workspace",
                    "scope_id": workspace.id,
                    "grant_role": "worker",
                }
            ]
        ),
        group_memberships_json=[],
        invited_by_user_id=inviter_id,
        created_at=_PINNED,
        expires_at=_PINNED + timedelta(hours=24),
        accepted_at=None,
        revoked_at=None,
    )
    session.add(invite)
    session.flush()

    # Mint the magic link directly so the test owns the signed token.
    # ``request_link`` with ``send_email=False`` avoids the mailer
    # branch and returns a :class:`PendingMagicLink` whose ``url``
    # we can extract the token from. cd-9i7z replaced the old
    # str-returning shape with the deferred-send pending so the
    # SMTP send can be sequenced after a router-level commit.
    pending = magic_link.request_link(
        session,
        email=invitee_email,
        purpose="grant_invite",
        ip="127.0.0.1",
        mailer=None,
        base_url=_BASE_URL,
        now=_PINNED,
        ttl=timedelta(hours=24),
        throttle=Throttle(),
        settings=_TEST_SETTINGS,
        clock=FrozenClock(_PINNED),
        subject_id=invite_id,
        send_email=False,
    )
    assert pending is not None
    token = pending.url.rsplit("/", 1)[-1]
    return invite_id, token


# ---------------------------------------------------------------------------
# Public surface — exports and shape
# ---------------------------------------------------------------------------


class TestPublicSurface:
    """``introspect_invite`` + ``InviteIntrospection`` are exported."""

    def test_introspect_invite_exported(self) -> None:
        assert membership.introspect_invite is not None

    def test_invite_introspection_dataclass_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        intr = membership.InviteIntrospection(
            kind="new_user",
            invite_id="01HWA0000000000000000INV1",
            workspace_id="01HWA0000000000000000WS01",
            workspace_slug="ws",
            workspace_name="WS",
            inviter_display_name="Owner",
            email_lower="alice@example.com",
            expires_at=_PINNED,
            grants=[],
            permission_group_memberships=[],
        )
        with pytest.raises(FrozenInstanceError):
            intr.kind = "existing_user"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestNewUserPreview:
    """An invitee with no passkey lands ``kind="new_user"``."""

    def test_returns_new_user_preview_with_full_shape(self, session: Session) -> None:
        ws = make_workspace(session, slug="acme")
        inviter = make_user(
            session, email="owner@acme.test", display_name="Owner Person"
        )
        invite_id, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
            invitee_display_name="Alice Example",
        )

        preview = membership.introspect_invite(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )

        assert preview.kind == "new_user"
        assert preview.invite_id == invite_id
        assert preview.workspace_id == ws.id
        assert preview.workspace_slug == "acme"
        assert preview.workspace_name == ws.name
        assert preview.inviter_display_name == "Owner Person"
        assert preview.email_lower == "alice@example.com"
        # Aware UTC datetime (§02 "Time").
        assert preview.expires_at.tzinfo is not None
        assert preview.expires_at == _PINNED + timedelta(hours=24)
        assert preview.grants == [
            {
                "scope_kind": "workspace",
                "scope_id": ws.id,
                "grant_role": "worker",
            }
        ]
        assert preview.permission_group_memberships == []


class TestExistingUserPreview:
    """An invitee with a registered passkey lands ``kind="existing_user"``."""

    def test_returns_existing_user_preview(self, session: Session) -> None:
        ws = make_workspace(session, slug="globex")
        inviter = make_user(session, email="owner@globex.test", display_name="Owner")
        invitee = make_user(session, email="bob@example.com", display_name="Bob")
        _seed_passkey(session, user_id=invitee.id)

        invite_id, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="bob@example.com",
            invitee_display_name="Bob",
            invitee_user_id=invitee.id,
        )

        preview = membership.introspect_invite(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )

        assert preview.kind == "existing_user"
        assert preview.invite_id == invite_id
        assert preview.workspace_id == ws.id
        assert preview.email_lower == "bob@example.com"


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


class TestReadOnly:
    """Introspect does not touch the magic-link nonce or audit table."""

    def test_does_not_burn_nonce(self, session: Session) -> None:
        ws = make_workspace(session, slug="readonly")
        inviter = make_user(session, email="o@r.test", display_name="O")
        _, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        # Nonce row pre-introspect.
        with tenant_agnostic():
            pre_rows = list(session.scalars(select(MagicLinkNonce)).all())
        assert len(pre_rows) == 1
        assert pre_rows[0].consumed_at is None

        membership.introspect_invite(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )

        # Nonce still pending — introspect did NOT flip ``consumed_at``.
        with tenant_agnostic():
            post = session.get(MagicLinkNonce, pre_rows[0].jti)
        assert post is not None
        assert post.consumed_at is None

    def test_does_not_write_audit_row(self, session: Session) -> None:
        ws = make_workspace(session, slug="audit-readonly")
        inviter = make_user(session, email="o@ar.test", display_name="O")
        invite_id, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        # Audit rows that exist before the introspect call (request_link
        # writes ``magic_link.sent``). We compare counts before/after so
        # any new audit row would surface.
        pre_count = len(list(session.scalars(select(AuditLog)).all()))

        membership.introspect_invite(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )

        post_count = len(list(session.scalars(select(AuditLog)).all()))
        assert post_count == pre_count

        # No audit row keyed off the invite id either.
        invite_audit = list(
            session.scalars(
                select(AuditLog).where(AuditLog.entity_id == invite_id)
            ).all()
        )
        assert invite_audit == []

    def test_subsequent_consume_still_succeeds(self, session: Session) -> None:
        """Introspect → consume sequence: the nonce is still redeemable."""
        ws = make_workspace(session, slug="seq")
        inviter = make_user(session, email="o@seq.test", display_name="O")
        _, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        membership.introspect_invite(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )

        # The same token must still consume — peek did not flip the nonce.
        outcome = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )
        assert isinstance(outcome, membership.NewUserAcceptance)


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


class TestErrorModes:
    """Each domain error path raises the right typed exception."""

    def test_invalid_token_raises(self, session: Session) -> None:
        with pytest.raises(magic_link.InvalidToken):
            membership.introspect_invite(
                session,
                token="garbage.not.a.valid.token",
                ip="127.0.0.1",
                throttle=Throttle(),
                settings=_TEST_SETTINGS,
                now=_PINNED,
            )

    def test_expired_token_raises(self, session: Session) -> None:
        ws = make_workspace(session, slug="expiry")
        inviter = make_user(session, email="o@exp.test", display_name="O")
        _, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        # Move "now" past the token's exp claim (24h TTL on grant_invite).
        future = _PINNED + timedelta(hours=25)
        with pytest.raises(magic_link.TokenExpired):
            membership.introspect_invite(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=Throttle(),
                settings=_TEST_SETTINGS,
                now=future,
            )

    def test_already_consumed_token_raises(self, session: Session) -> None:
        ws = make_workspace(session, slug="consumed")
        inviter = make_user(session, email="o@con.test", display_name="O")
        _, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        # Burn the nonce via consume — a subsequent introspect must
        # raise :class:`AlreadyConsumed`.
        membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            now=_PINNED,
        )

        with pytest.raises(magic_link.AlreadyConsumed):
            membership.introspect_invite(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=Throttle(),
                settings=_TEST_SETTINGS,
                now=_PINNED,
            )

    def test_invite_state_revoked_raises(self, session: Session) -> None:
        ws = make_workspace(session, slug="revoked")
        inviter = make_user(session, email="o@rv.test", display_name="O")
        invite_id, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        invite_row = session.get(Invite, invite_id)
        assert invite_row is not None
        invite_row.state = "revoked"
        session.flush()

        with pytest.raises(membership.InviteStateInvalid):
            membership.introspect_invite(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=Throttle(),
                settings=_TEST_SETTINGS,
                now=_PINNED,
            )

    def test_invite_already_accepted_raises(self, session: Session) -> None:
        ws = make_workspace(session, slug="accepted")
        inviter = make_user(session, email="o@ac.test", display_name="O")
        invite_id, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        invite_row = session.get(Invite, invite_id)
        assert invite_row is not None
        invite_row.state = "accepted"
        invite_row.accepted_at = _PINNED
        session.flush()

        with pytest.raises(membership.InviteAlreadyAccepted):
            membership.introspect_invite(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=Throttle(),
                settings=_TEST_SETTINGS,
                now=_PINNED,
            )

    def test_invite_row_expired_raises(self, session: Session) -> None:
        """Token still valid, but the invite row's TTL has lapsed."""
        ws = make_workspace(session, slug="ws-expiry")
        inviter = make_user(session, email="o@we.test", display_name="O")
        invite_id, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        invite_row = session.get(Invite, invite_id)
        assert invite_row is not None
        # Backdate the row's expires_at without invalidating the token.
        invite_row.expires_at = _PINNED - timedelta(hours=1)
        session.flush()

        with pytest.raises(membership.InviteExpired):
            membership.introspect_invite(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=Throttle(),
                settings=_TEST_SETTINGS,
                now=_PINNED,
            )

    def test_does_not_raise_passkey_session_required(self, session: Session) -> None:
        """Introspect is session-agnostic — never raises this exception.

        Regression guard: existing-user invitee + no active session
        is exactly the path :func:`consume_invite_token` rejects with
        :class:`PasskeySessionRequired`, but introspect must succeed
        because the SPA needs the preview before the user signs in.
        """
        ws = make_workspace(session, slug="agnostic")
        inviter = make_user(session, email="o@ag.test", display_name="O")
        invitee = make_user(session, email="bob@example.com", display_name="Bob")
        _seed_passkey(session, user_id=invitee.id)
        _, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="bob@example.com",
            invitee_display_name="Bob",
            invitee_user_id=invitee.id,
        )

        # active_user_id deliberately ``None`` — no live session.
        preview = membership.introspect_invite(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=Throttle(),
            settings=_TEST_SETTINGS,
            active_user_id=None,
            now=_PINNED,
        )
        assert preview.kind == "existing_user"


# ---------------------------------------------------------------------------
# Throttle integration
# ---------------------------------------------------------------------------


class TestThrottleBucketShared:
    """Peek failures count toward the same bucket as accept failures.

    Spec §15 "Rate limiting and abuse controls": consume-failure
    lockout is 3 fails / 60 s → 10-minute IP lockout. Introspect
    routes through :func:`magic_link.peek_link`, which uses the
    same :meth:`Throttle.check_consume_allowed` /
    :meth:`Throttle.record_consume_failure` pair, so a brute-force
    introspect probe must trip the same lockout that an attacker
    burning bad accept tokens would.
    """

    def test_locked_out_ip_cannot_peek(self, session: Session) -> None:
        ws = make_workspace(session, slug="locked")
        inviter = make_user(session, email="o@lk.test", display_name="O")
        _, token = _mint_invite(
            session,
            workspace=ws,
            inviter_id=inviter.id,
            invitee_email="alice@example.com",
        )

        throttle = Throttle()
        # Trip the lockout by recording the consume-fail threshold.
        for _ in range(3):
            throttle.record_consume_failure(ip="127.0.0.1", now=_PINNED)

        # A locked-out IP must be refused — same shape consume sees.
        with pytest.raises(magic_link.ConsumeLockout):
            membership.introspect_invite(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=throttle,
                settings=_TEST_SETTINGS,
                now=_PINNED,
            )
