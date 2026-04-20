"""Integration tests for :mod:`app.domain.identity.membership`.

Exercises the invite → accept → remove flow + the workspace switch
path against a real DB with the tenant filter installed. Each test:

* Bootstraps a user + workspace (so ``owners`` + the three
  non-owners system groups are seeded).
* Sets a :class:`WorkspaceContext` for that workspace.
* Calls the domain service through its public surface.
* Asserts the row changes, session revocation, and audit shape.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import (
    Invite,
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.adapters.db.identity.models import (
    Session as SessionRow,
)
from app.adapters.db.workspace.models import UserWorkspace
from app.auth._throttle import Throttle
from app.domain.identity import membership
from app.domain.identity.permission_groups import (
    add_member,
    list_groups,
    write_member_remove_rejected_audit,
)
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"


# Minimal :class:`Settings` stand-in so the domain service's
# :func:`derive_subkey` calls have a :class:`SecretStr` to unwrap.
# Import :class:`Settings` here so the integration test doesn't
# reach for a real env config.
from pydantic import SecretStr  # noqa: E402

from app.config import Settings  # noqa: E402

_TEST_SETTINGS = Settings(
    root_key=SecretStr("test-root-key-for-hash-pepper-0123456789abcdef"),
    public_url=_BASE_URL,
)


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
    """Re-register the workspace-scoped tables this test module depends on."""
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("audit_log")
    registry.register("invite")
    registry.register("user_workspace")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to the given workspace."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"memb-test-{_SLUG_COUNTER:05d}"


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext, InMemoryMailer, Throttle]]:
    """Yield ``(session, ctx, mailer, throttle)`` bound to a fresh workspace."""
    install_tenant_filter(db_session)

    slug = _next_slug()
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"Owner {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    # ``bootstrap_workspace`` writes the owner's ``user_workspace``
    # row alongside the owners-group seed; no extra materialisation
    # needed here.
    ctx = _ctx_for(ws.id, ws.slug, user.id)

    token = set_current(ctx)
    try:
        yield db_session, ctx, InMemoryMailer(), Throttle()
    finally:
        reset_current(token)


def _all_audit_for(session: Session, *, entity_id: str) -> list[AuditLog]:
    """Return every audit row for ``entity_id`` ordered by creation."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )


def _seed_passkey(session: Session, *, user_id: str, clock: FrozenClock) -> None:
    """Seed a minimal :class:`PasskeyCredential` for ``user_id``.

    :func:`membership.complete_invite` + the existing-user branch of
    :func:`membership.consume_invite_token` gate on passkey-presence
    (spec §03 "Additional users"). Tests for either flow must seed a
    credential to stand in for the real WebAuthn ceremony.
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
                created_at=clock.now(),
                last_used_at=None,
            )
        )
        session.flush()


def _invite_payload(
    ctx: WorkspaceContext, *, role: str = "worker"
) -> dict[str, object]:
    """Return a minimal invite payload for the caller's workspace."""
    return {
        "email": "alice@example.com",
        "display_name": "Alice Example",
        "grants": [
            {
                "scope_kind": "workspace",
                "scope_id": ctx.workspace_id,
                "grant_role": role,
            }
        ],
    }


# ---------------------------------------------------------------------------
# invite
# ---------------------------------------------------------------------------


class TestInvite:
    """``invite`` writes a pending row, mails the magic link, audits."""

    def test_invite_new_user_creates_user_and_invite_rows(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        payload = _invite_payload(ctx)
        outcome = membership.invite(
            session,
            ctx,
            email=payload["email"],  # type: ignore[arg-type]
            display_name=payload["display_name"],  # type: ignore[arg-type]
            grants=payload["grants"],  # type: ignore[arg-type]
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )

        assert outcome.user_created is True
        assert outcome.pending_email == canonicalise_email("alice@example.com")

        invite_row = session.get(Invite, outcome.id)
        assert invite_row is not None
        assert invite_row.state == "pending"
        assert invite_row.workspace_id == ctx.workspace_id
        assert invite_row.pending_email_lower == "alice@example.com"
        assert invite_row.invited_by_user_id == ctx.actor_id
        # Email plaintext is stored on the invite row for the accept
        # UX; the hash rides audit diffs only.
        assert invite_row.pending_email == "alice@example.com"
        assert invite_row.email_hash
        assert invite_row.email_hash != "alice@example.com"

        # A :class:`User` row was created for the invitee.
        user_row = session.scalar(
            select(User).where(User.email_lower == "alice@example.com")
        )
        assert user_row is not None
        assert user_row.id == outcome.user_id

        # One email was sent with the invite subject.
        assert len(mailer.sent) == 1
        msg = mailer.sent[0]
        assert msg.to == ("alice@example.com",)
        assert "Owner" in msg.subject
        assert ctx.workspace_slug in msg.subject
        assert "/auth/magic/" in msg.body_text

        rows = _all_audit_for(session, entity_id=outcome.id)
        assert [r.action for r in rows] == ["user.invited"]
        assert rows[0].diff["email_hash"] == invite_row.email_hash
        assert "alice@example.com" not in str(rows[0].diff)

    def test_invite_existing_user_reuses_user(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        # Seed the invitee first.
        existing = bootstrap_user(
            session,
            email="bob@example.com",
            display_name="Bob Existing",
            clock=FrozenClock(_PINNED),
        )
        outcome = membership.invite(
            session,
            ctx,
            email="bob@example.com",
            display_name="Bob Invited",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        assert outcome.user_created is False
        assert outcome.user_id == existing.id

    def test_invite_twice_refreshes_same_row(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        first = membership.invite(
            session,
            ctx,
            email="carol@example.com",
            display_name="Carol Old",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        second = membership.invite(
            session,
            ctx,
            email="carol@example.com",
            display_name="Carol New",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "manager",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        assert first.id == second.id  # Same row, refreshed
        invite_row = session.get(Invite, second.id)
        assert invite_row is not None
        assert invite_row.display_name == "Carol New"
        assert invite_row.grants_json[0]["grant_role"] == "manager"

    def test_invite_rejects_missing_email(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        with pytest.raises(membership.InviteBodyInvalid):
            membership.invite(
                session,
                ctx,
                email="",
                display_name="Nobody",
                grants=[
                    {
                        "scope_kind": "workspace",
                        "scope_id": ctx.workspace_id,
                        "grant_role": "worker",
                    }
                ],
                mailer=mailer,
                throttle=throttle,
                base_url=_BASE_URL,
                settings=_TEST_SETTINGS,
                inviter_display_name="Owner",
                workspace_name=ctx.workspace_slug,
            )

    def test_invite_rejects_cross_workspace_scope_id(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        with pytest.raises(membership.InviteBodyInvalid):
            membership.invite(
                session,
                ctx,
                email="dan@example.com",
                display_name="Dan",
                grants=[
                    {
                        "scope_kind": "workspace",
                        "scope_id": "not-this-workspace",
                        "grant_role": "worker",
                    }
                ],
                mailer=mailer,
                throttle=throttle,
                base_url=_BASE_URL,
                settings=_TEST_SETTINGS,
                inviter_display_name="Owner",
                workspace_name=ctx.workspace_slug,
            )

    def test_invite_rejects_bad_grant_role(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        with pytest.raises(membership.InviteBodyInvalid):
            membership.invite(
                session,
                ctx,
                email="eve@example.com",
                display_name="Eve",
                grants=[
                    {
                        "scope_kind": "workspace",
                        "scope_id": ctx.workspace_id,
                        "grant_role": "admin",  # not in v1 enum
                    }
                ],
                mailer=mailer,
                throttle=throttle,
                base_url=_BASE_URL,
                settings=_TEST_SETTINGS,
                inviter_display_name="Owner",
                workspace_name=ctx.workspace_slug,
            )

    def test_invite_audit_carries_email_hash_not_plaintext(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = membership.invite(
            session,
            ctx,
            email="PrivAte@Example.COM",
            display_name="Priv",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        rows = _all_audit_for(session, entity_id=outcome.id)
        assert rows, "invite audit row missing"
        diff_str = str(rows[0].diff)
        assert "private@example.com" not in diff_str.lower()
        assert "@" not in rows[0].diff.get("email_hash", "")


# ---------------------------------------------------------------------------
# accept flows
# ---------------------------------------------------------------------------


class TestAcceptNewUser:
    """New-user branch: consume_invite_token → complete_invite."""

    def test_new_user_acceptance_card(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = membership.invite(
            session,
            ctx,
            email="new@example.com",
            display_name="New Person",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        # Extract the signed token from the captured email body.
        body = mailer.sent[0].body_text
        token = _extract_token_from_body(body)

        # consume_invite_token: expects no active_user_id for a brand-
        # new invitee (no session exists yet).
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        assert acceptance.session.invite_id == outcome.id
        assert acceptance.session.email_lower == "new@example.com"
        assert acceptance.session.display_name == "New Person"

        # Stand-in for the real WebAuthn ceremony — :func:`complete_invite`
        # refuses to activate until a passkey row exists for the user.
        _seed_passkey(
            session, user_id=acceptance.session.user_id, clock=FrozenClock(_PINNED)
        )

        # complete_invite: activates the invite.
        workspace_id = membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )
        assert workspace_id == ctx.workspace_id

        invite_row = session.get(Invite, outcome.id)
        assert invite_row is not None
        assert invite_row.state == "accepted"
        assert invite_row.accepted_at is not None

        # Role grant landed.
        grants = session.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.user_id == outcome.user_id,
            )
        ).all()
        assert len(grants) == 1
        assert grants[0].grant_role == "worker"

        # Derived user_workspace row landed (TODO cd-yqm4).
        uw = session.get(UserWorkspace, (outcome.user_id, ctx.workspace_id))
        assert uw is not None

        # Audit: user.enrolled
        audit = _all_audit_for(session, entity_id=outcome.id)
        actions = [r.action for r in audit]
        assert "user.invited" in actions
        assert "user.enrolled" in actions

    def test_complete_rejects_already_accepted(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = membership.invite(
            session,
            ctx,
            email="double@example.com",
            display_name="Double",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        assert outcome.user_id is not None
        _seed_passkey(session, user_id=outcome.user_id, clock=FrozenClock(_PINNED))
        membership.complete_invite(
            session, invite_id=outcome.id, settings=_TEST_SETTINGS
        )
        with pytest.raises(membership.InviteAlreadyAccepted):
            membership.complete_invite(
                session, invite_id=outcome.id, settings=_TEST_SETTINGS
            )

    def test_complete_rejects_without_passkey(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """Auth gate: ``/invite/complete`` with an invite_id alone is a
        guessable path, so :func:`complete_invite` refuses unless the
        linked user holds a registered passkey."""
        session, ctx, mailer, throttle = env
        outcome = membership.invite(
            session,
            ctx,
            email="nocred@example.com",
            display_name="NoCred",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        # No passkey seeded — the guard must fire.
        with pytest.raises(membership.PasskeySessionRequired):
            membership.complete_invite(
                session, invite_id=outcome.id, settings=_TEST_SETTINGS
            )


class TestAcceptExistingUser:
    """Existing-user branch requires an active session."""

    def test_existing_user_with_session_returns_acceptance_card(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        # Seed an existing user + a registered passkey (the discriminator
        # between the new-user and existing-user accept branches) + an
        # active session so the acceptance card renders.
        clock = FrozenClock(_PINNED)
        invitee = bootstrap_user(
            session,
            email="existing@example.com",
            display_name="Existing",
            clock=clock,
        )
        _seed_passkey(session, user_id=invitee.id, clock=clock)
        with tenant_agnostic():
            session.add(
                SessionRow(
                    id=new_ulid(),
                    user_id=invitee.id,
                    workspace_id=None,
                    expires_at=_PINNED + timedelta(days=7),
                    last_seen_at=_PINNED,
                    ua_hash=None,
                    ip_hash=None,
                    created_at=_PINNED,
                )
            )
            session.flush()

        # Invite them.
        outcome = membership.invite(
            session,
            ctx,
            email="existing@example.com",
            display_name="Existing",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "client",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            active_user_id=invitee.id,
        )
        assert isinstance(acceptance, membership.ExistingUserAcceptance)
        assert acceptance.card.workspace_slug == ctx.workspace_slug
        assert len(acceptance.card.grants) == 1

        # Confirm → actually activate.
        invitee_ctx = _ctx_for(ctx.workspace_id, ctx.workspace_slug, invitee.id)
        workspace_id = membership.confirm_invite(
            session, invitee_ctx, invite_id=outcome.id
        )
        assert workspace_id == ctx.workspace_id

        invite_row = session.get(Invite, outcome.id)
        assert invite_row is not None
        assert invite_row.state == "accepted"

        audit = _all_audit_for(session, entity_id=outcome.id)
        actions = [r.action for r in audit]
        assert "user.grant_accepted" in actions

    def test_existing_user_no_session_raises_passkey_required(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        clock = FrozenClock(_PINNED)
        invitee = bootstrap_user(
            session,
            email="silent@example.com",
            display_name="Silent",
            clock=clock,
        )
        # Seed a passkey so consume_invite_token triggers the existing-
        # user branch. Without a passkey, an invitee would route through
        # the new-user branch instead (silent sign-in for a known-but-
        # never-authenticated user is not part of the spec flow).
        _seed_passkey(session, user_id=invitee.id, clock=clock)

        outcome = membership.invite(
            session,
            ctx,
            email="silent@example.com",
            display_name="Silent",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=_TEST_SETTINGS,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        del outcome
        token = _extract_token_from_body(mailer.sent[0].body_text)
        # Existing user branch fires: the invitee has an active
        # session row but ``active_user_id`` is None (caller claims
        # no signed-in user). The domain raises
        # :class:`PasskeySessionRequired`.
        with pytest.raises(membership.PasskeySessionRequired):
            membership.consume_invite_token(
                session,
                token=token,
                ip="127.0.0.1",
                throttle=throttle,
                settings=_TEST_SETTINGS,
                active_user_id=None,
            )


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


class TestRemoveMember:
    """``remove_member`` strips grants + group memberships + sessions."""

    def test_remove_happy_path(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _mailer, _throttle = env
        clock = FrozenClock(_PINNED)
        worker = bootstrap_user(
            session,
            email="worker@example.com",
            display_name="Worker",
            clock=clock,
        )
        # Mint a grant + a user_workspace row + a session.
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                user_id=worker.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=clock.now(),
                created_by_user_id=ctx.actor_id,
            )
        )
        with tenant_agnostic():
            session.add(
                UserWorkspace(
                    user_id=worker.id,
                    workspace_id=ctx.workspace_id,
                    source="workspace_grant",
                    added_at=clock.now(),
                )
            )
            session.add(
                SessionRow(
                    id=new_ulid(),
                    user_id=worker.id,
                    workspace_id=ctx.workspace_id,
                    expires_at=_PINNED + timedelta(days=7),
                    last_seen_at=_PINNED,
                    ua_hash=None,
                    ip_hash=None,
                    created_at=_PINNED,
                )
            )
        session.flush()

        membership.remove_member(session, ctx, user_id=worker.id)

        remaining_grants = session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == worker.id)
        ).all()
        assert remaining_grants == []
        uw = session.get(UserWorkspace, (worker.id, ctx.workspace_id))
        assert uw is None
        # Sessions for the workspace are revoked.
        with tenant_agnostic():
            remaining_sessions = session.scalars(
                select(SessionRow).where(
                    SessionRow.user_id == worker.id,
                    SessionRow.workspace_id == ctx.workspace_id,
                )
            ).all()
        assert remaining_sessions == []

        # Audit row lands with the deleted grant ids.
        audit = _all_audit_for(session, entity_id=worker.id)
        actions = [r.action for r in audit]
        assert "user.removed" in actions
        removed = next(r for r in audit if r.action == "user.removed")
        assert removed.diff["sessions_revoked"] >= 1
        # Email is not persisted in the remove audit; the user_id
        # is the forensic anchor.
        assert "email_hash" not in removed.diff
        # User row itself is not deleted — identity persists.
        assert session.get(User, worker.id) is not None

    def test_remove_last_owner_refused(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _, _ = env
        with pytest.raises(membership.LastOwnerMember):
            membership.remove_member(session, ctx, user_id=ctx.actor_id)

    def test_remove_non_member_raises(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _, _ = env
        stranger = bootstrap_user(
            session,
            email="stranger@example.com",
            display_name="Stranger",
            clock=FrozenClock(_PINNED),
        )
        with pytest.raises(membership.NotAMember):
            membership.remove_member(session, ctx, user_id=stranger.id)

    def test_remove_second_owner_succeeds(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _, _ = env
        clock = FrozenClock(_PINNED)
        # Add a second owner.
        second = bootstrap_user(
            session,
            email="second@example.com",
            display_name="Second",
            clock=clock,
        )
        with tenant_agnostic():
            session.add(
                UserWorkspace(
                    user_id=second.id,
                    workspace_id=ctx.workspace_id,
                    source="workspace_grant",
                    added_at=clock.now(),
                )
            )
            session.flush()
        owners_group = next(g for g in list_groups(session, ctx) if g.slug == "owners")
        add_member(session, ctx, group_id=owners_group.id, user_id=second.id)
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                user_id=second.id,
                grant_role="manager",
                scope_property_id=None,
                created_at=clock.now(),
                created_by_user_id=ctx.actor_id,
            )
        )
        session.flush()
        # Now remove the second owner — ok, since the first stays.
        membership.remove_member(session, ctx, user_id=second.id)
        remaining = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == owners_group.id,
            )
        ).all()
        assert len(remaining) == 1
        assert remaining[0].user_id == ctx.actor_id


# ---------------------------------------------------------------------------
# workspace switch
# ---------------------------------------------------------------------------


class TestSwitchWorkspace:
    """``switch_session_workspace`` moves Session.workspace_id atomically."""

    def test_switch_updates_session(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _, _ = env
        # Seed a session with workspace_id=None for the owner.
        session_row = SessionRow(
            id=new_ulid(),
            user_id=ctx.actor_id,
            workspace_id=None,
            expires_at=_PINNED + timedelta(days=7),
            last_seen_at=_PINNED,
            ua_hash=None,
            ip_hash=None,
            created_at=_PINNED,
        )
        with tenant_agnostic():
            session.add(session_row)
            session.flush()

        membership.switch_session_workspace(
            session,
            session_id=session_row.id,
            user_id=ctx.actor_id,
            workspace_id=ctx.workspace_id,
        )
        with tenant_agnostic():
            reloaded = session.get(SessionRow, session_row.id)
        assert reloaded is not None
        assert reloaded.workspace_id == ctx.workspace_id

    def test_switch_rejects_non_member(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _, _ = env
        with pytest.raises(membership.NotAMember):
            membership.switch_session_workspace(
                session,
                session_id="whatever",
                user_id=ctx.actor_id,
                workspace_id="not-a-workspace",
            )


# ---------------------------------------------------------------------------
# list_workspaces_for_user
# ---------------------------------------------------------------------------


class TestListWorkspacesForUser:
    """``list_workspaces_for_user`` aggregates across user_workspace rows."""

    def test_returns_current_workspace(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, _, _ = env
        memberships = membership.list_workspaces_for_user(session, user_id=ctx.actor_id)
        slugs = [m.workspace_slug for m in memberships]
        assert ctx.workspace_slug in slugs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_token_from_body(body: str) -> str:
    """Return the signed magic-link token embedded in an email body."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"no URL in body: {body!r}")


# Silence unused-import warnings on `write_member_remove_rejected_audit`
# — it's imported to verify the export remains public after the refactor.
assert write_member_remove_rejected_audit is not None
