"""Unit tests for :func:`app.domain.identity.membership.invite`.

Focused on the §15 enumeration-guard sibling — ``invite`` is
manager-gated (so it's not an enumeration guard per se) but a mailer
outage must not fail the write: the :class:`Invite` row and the
``user.invited`` audit row must commit so an operator can re-issue
the invite from the UI once SMTP recovers. Mirrors the shape of
:class:`tests.unit.auth.test_recovery.TestRequestRecoveryEnumerationGuard
.test_hit_branch_swallows_mail_delivery_error`.

See ``docs/specs/15-security-privacy.md`` §"Rate limiting and abuse
controls" and ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import Invite, MagicLinkNonce
from app.adapters.db.session import make_engine
from app.adapters.mail.ports import MailDeliveryError
from app.auth._throttle import Throttle
from app.auth.magic_link import PendingDispatch
from app.config import Settings
from app.domain.identity import membership
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _ExplodingMailer:
    """:class:`Mailer` double that raises a pre-canned exception on send.

    Drives the §15 enumeration-guard sibling test — mirrors the
    fixture of the same name in :mod:`tests.unit.auth.test_recovery`
    so the shape stays consistent across the three auth surfaces
    that swallow :class:`MailDeliveryError` uniformly.
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-root-key-for-invite-guard"),
        public_url=_BASE_URL,
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
    # ``invite`` touches workspace-scoped tables (invite, audit_log,
    # permission_group_member, role_grant); the ORM tenant filter
    # needs to be installed so the SELECTs it runs don't trip the
    # "no workspace context" guard. Matches the integration test's
    # ``env`` fixture.
    install_tenant_filter(factory)
    # Register every workspace-scoped table the invite path reads
    # or writes — the ORM filter keys off this registry.
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("audit_log")
    registry.register("invite")
    registry.register("user_workspace")
    with factory() as s:
        yield s


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def env(
    session: Session,
) -> Iterator[tuple[Session, WorkspaceContext]]:
    """Yield ``(session, ctx)`` bound to a fresh workspace + owner.

    Seeds the minimum state ``invite`` needs: a workspace with a live
    owner membership plus the four system permission groups (so the
    group-membership validator has rows to see), and a pinned
    :class:`WorkspaceContext` scoped to that workspace.
    """
    clock = FrozenClock(_PINNED)
    owner = bootstrap_user(
        session,
        email="owner@example.com",
        display_name="Owner",
        clock=clock,
    )
    ws = bootstrap_workspace(
        session,
        slug="invite-guard",
        name="Invite Guard",
        owner_user_id=owner.id,
        clock=clock,
    )
    ctx = WorkspaceContext(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLB",
    )
    token = set_current(ctx)
    try:
        yield session, ctx
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Enumeration-guard sibling: mailer outages must not fail the write
# ---------------------------------------------------------------------------


class TestInviteMailDeliveryGuard:
    """§15 sibling: a mailer outage must not fail ``users.invite``.

    ``invite`` is manager-gated so the enumeration-leak shape from
    ``recovery`` / ``magic-link`` / ``signup`` does not apply
    directly. The intent is nonetheless the same: an SMTP outage must
    not lose the :class:`Invite` row or the ``user.invited`` audit
    trail. Without this guard the caller sees a 500, the invite row
    rolls back with it, and a re-issue from the UI has no row to
    refresh — forcing a new signed URL with no forensic link to the
    first attempt.
    """

    def test_invite_swallows_mail_delivery_error(
        self,
        env: tuple[Session, WorkspaceContext],
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        session, ctx = env
        failing_mailer = _ExplodingMailer(MailDeliveryError("relay down"))
        # Must NOT raise — :func:`invite` catches
        # :class:`MailDeliveryError` so an operator can re-issue the
        # mail from the invite UI once SMTP recovers.
        outcome = membership.invite(
            session,
            ctx,
            email="invitee@example.com",
            display_name="Invitee",
            grants=[
                {
                    "scope_kind": "workspace",
                    "scope_id": ctx.workspace_id,
                    "grant_role": "worker",
                }
            ],
            mailer=failing_mailer,
            throttle=throttle,
            base_url=_BASE_URL,
            settings=settings,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
        )
        # The :class:`Invite` row committed so an operator can
        # re-issue the mail straight from this row.
        invite_row = session.get(Invite, outcome.id)
        assert invite_row is not None
        assert invite_row.state == "pending"
        assert invite_row.pending_email_lower == "invitee@example.com"
        # The ``user.invited`` audit row committed too — forensic
        # trail intact despite the mailer outage.
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "user.invited")
        ).all()
        assert len(audits) == 1
        diff = audits[0].diff
        assert isinstance(diff, dict)
        # Plaintext never leaks through on the rollback-free path.
        assert "invitee@example.com" not in str(diff)


class TestInviteOutboxOrdering:
    """cd-9slq: SMTP send must run *after* invite + nonce + audit are
    durable.

    Mirrors :class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering`
    at the membership-domain layer. Production callers (the
    ``POST /users/invite`` HTTP router) sequence ``with UoW: invite()
    → commit → dispatch.deliver()``; a commit failure short-circuits
    the invite-flavoured SMTP send so no working ``grant_invite``
    token reaches the inbox without the matching :class:`Invite`
    row durable on disk.
    """

    def test_dispatch_collected_does_not_send_email_until_deliver(
        self,
        env: tuple[Session, WorkspaceContext],
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """``invite`` queues writes + the deferred send onto the
        :class:`PendingDispatch`; the recording mailer stays
        untouched until the caller invokes ``dispatch.deliver()``.
        """
        session, ctx = env
        mailer = InMemoryMailer()
        dispatch = PendingDispatch()
        outcome = membership.invite(
            session,
            ctx,
            email="lazy@example.com",
            display_name="Lazy",
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
            settings=settings,
            inviter_display_name="Owner",
            workspace_name=ctx.workspace_slug,
            dispatch=dispatch,
        )
        # Invite + magic-link nonce queued on the session.
        assert session.get(Invite, outcome.id) is not None
        nonces = session.scalars(select(MagicLinkNonce)).all()
        assert len(nonces) == 1
        # Mailer untouched until ``dispatch.deliver()`` fires.
        assert mailer.sent == [], f"mailer fired before deliver(): {mailer.sent!r}"
        dispatch.deliver()
        assert len(mailer.sent) == 1

    def test_commit_failure_before_deliver_does_not_send_email(
        self,
        env: tuple[Session, WorkspaceContext],
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """The cd-t2jz repro on the membership domain: commit fails
        → no email goes out.
        """
        session, ctx = env
        mailer = InMemoryMailer()
        dispatch = PendingDispatch()
        original_commit = session.commit

        def _failing_commit() -> None:
            session.rollback()
            raise RuntimeError("simulated commit failure")

        session.commit = _failing_commit  # type: ignore[method-assign]
        try:
            membership.invite(
                session,
                ctx,
                email="outbox@example.com",
                display_name="Outbox",
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
                settings=settings,
                inviter_display_name="Owner",
                workspace_name=ctx.workspace_slug,
                dispatch=dispatch,
            )
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                session.commit()
        finally:
            session.commit = original_commit  # type: ignore[method-assign]

        # Mailer never invoked — commit failure short-circuits
        # ``dispatch.deliver()`` per cd-9slq.
        assert mailer.sent == [], (
            f"mailer was invoked despite commit failure: {mailer.sent!r}"
        )
        # The rolled-back invite + nonce + audit are gone.
        assert session.scalars(select(Invite)).all() == []
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "user.invited")
            ).all()
            == []
        )
