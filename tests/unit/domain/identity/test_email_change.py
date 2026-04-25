"""Unit tests for :mod:`app.domain.identity.email_change` (cd-9slq).

Covers the cd-9slq outbox invariant on the email-change domain
service: ``request_change`` and ``verify_change`` queue every SMTP
send onto the caller-supplied :class:`PendingDispatch` so a commit
failure short-circuits the deliveries. Mirrors
:class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering`
at the email-change layer.

Router-level coverage of error envelopes lives in
``tests/unit/api/v1/auth/test_email_change.py``; this file owns the
domain-service contract directly so the outbox seam is locked
independently of the HTTP wiring.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change"
and ``docs/specs/15-security-privacy.md`` §"Self-service lost-device
& email-change abuse mitigations".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import EmailChangePending, MagicLinkNonce, User
from app.adapters.db.session import make_engine
from app.auth._throttle import Throttle
from app.auth.magic_link import PendingDispatch
from app.config import Settings
from app.domain.identity import email_change
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-root-key-email-change-outbox"),
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
    with factory() as s:
        yield s


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def user(session: Session) -> User:
    return bootstrap_user(
        session,
        email="alice@example.com",
        display_name="Alice",
    )


class TestRequestChangeOutboxOrdering:
    """cd-9slq: ``request_change`` defers SMTP sends until commit.

    Mirrors :class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering`.
    Production callers (``POST /me/email/change_request``) sequence
    ``with UoW: request_change(... dispatch=...) → commit →
    dispatch.deliver()``; a commit failure short-circuits both sends
    (the magic-link template to the new address + the informational
    notice to the old one), so no working email-change token reaches
    the new inbox without the matching :class:`EmailChangePending`
    row + nonce on disk.
    """

    def test_dispatch_collected_does_not_send_email_until_deliver(
        self,
        session: Session,
        user: User,
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        dispatch = PendingDispatch()
        outcome = email_change.request_change(
            session,
            user=user,
            new_email="alice.new@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url=_BASE_URL,
            throttle=throttle,
            now=_PINNED,
            settings=settings,
            dispatch=dispatch,
        )
        # Pending row + magic-link nonce queued on the session.
        pending_row = session.get(EmailChangePending, outcome.pending_id)
        assert pending_row is not None
        assert pending_row.new_email_lower == "alice.new@example.com"
        nonces = session.scalars(select(MagicLinkNonce)).all()
        assert len(nonces) == 1
        # Mailer untouched until ``dispatch.deliver()`` fires.
        assert mailer.sent == [], f"mailer fired before deliver(): {mailer.sent!r}"
        dispatch.deliver()
        # Two sends queued: the magic-link template to the new
        # address + the notice to the old address.
        assert len(mailer.sent) == 2

    def test_commit_failure_before_deliver_does_not_send_email(
        self,
        session: Session,
        user: User,
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """The cd-t2jz repro on the email-change request path: commit
        fails → no email goes out.
        """
        dispatch = PendingDispatch()
        original_commit = session.commit

        def _failing_commit() -> None:
            session.rollback()
            raise RuntimeError("simulated commit failure")

        session.commit = _failing_commit  # type: ignore[method-assign]
        try:
            email_change.request_change(
                session,
                user=user,
                new_email="alice.new@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
                dispatch=dispatch,
            )
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                session.commit()
        finally:
            session.commit = original_commit  # type: ignore[method-assign]

        # Mailer was never invoked — commit failure short-circuits
        # ``dispatch.deliver()`` per cd-9slq.
        assert mailer.sent == [], (
            f"mailer was invoked despite commit failure: {mailer.sent!r}"
        )
        # The rolled-back pending row + nonce + audit are gone.
        assert session.scalars(select(EmailChangePending)).all() == []
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "email.change_requested")
            ).all()
            == []
        )


class TestVerifyChangeOutboxOrdering:
    """cd-9slq: ``verify_change`` defers SMTP sends until commit.

    Production callers (``POST /auth/email/verify``) sequence ``with
    UoW: verify_change(... dispatch=...) → commit →
    dispatch.deliver()``; a commit failure short-circuits both sends
    (the post-swap confirmation to the new address + the revert link
    to the old) so no working revert token reaches the old mailbox
    without the matching revert nonce + verified pending row durable
    on disk.
    """

    def _seed_pending(
        self,
        session: Session,
        *,
        user: User,
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> str:
        """Drive ``request_change`` end-to-end and return the
        magic-link token from the rendered URL.

        The verify step needs a real signed token; using the same
        mint-and-deliver pipeline matches what the router does and
        keeps this test on the production path.
        """
        dispatch = PendingDispatch()
        email_change.request_change(
            session,
            user=user,
            new_email="alice.new@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url=_BASE_URL,
            throttle=throttle,
            now=_PINNED,
            settings=settings,
            dispatch=dispatch,
        )
        session.commit()
        dispatch.deliver()
        # Two sends: the magic-link mail comes first.
        assert len(mailer.sent) == 2
        # Find the magic-link token in whichever message carries
        # ``/auth/magic/<token>`` on its own line.
        for msg in mailer.sent:
            for line in msg.body_text.splitlines():
                stripped = line.strip()
                if "/auth/magic/" in stripped:
                    return stripped.rsplit("/", 1)[-1]
        raise AssertionError(
            "no magic-link URL found in change_request rendered bodies"
        )

    def test_commit_failure_before_deliver_does_not_send_email(
        self,
        session: Session,
        user: User,
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """The cd-t2jz repro on the email-change verify path: commit
        fails → no email goes out.
        """
        token = self._seed_pending(
            session,
            user=user,
            mailer=mailer,
            throttle=throttle,
            settings=settings,
        )
        # Reset the recording so we observe only the verify-side
        # sends (the change_request seed already mailed two
        # messages above).
        mailer.sent.clear()

        dispatch = PendingDispatch()
        original_commit = session.commit

        def _failing_commit() -> None:
            session.rollback()
            raise RuntimeError("simulated commit failure")

        session.commit = _failing_commit  # type: ignore[method-assign]
        try:
            email_change.verify_change(
                session,
                token=token,
                session_user_id=user.id,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
                dispatch=dispatch,
            )
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                session.commit()
        finally:
            session.commit = original_commit  # type: ignore[method-assign]

        # Confirmation + revert-link sends were never invoked —
        # commit failure short-circuits ``dispatch.deliver()`` per
        # cd-9slq, so no working revert token reaches the old
        # mailbox without the matching revert nonce on disk.
        assert mailer.sent == [], (
            f"verify mailer was invoked despite commit failure: {mailer.sent!r}"
        )
