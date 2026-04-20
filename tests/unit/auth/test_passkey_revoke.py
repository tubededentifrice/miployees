"""Unit tests for :func:`app.auth.passkey.revoke_passkey`.

Covers the cd-hiko shape:

* happy path drops the credential row, writes ``passkey.revoked``
  BEFORE ``session.invalidated``, and invalidates every active session
  for the user with cause ``"passkey_revoked"``;
* ownership: a credential belonging to another user collapses to
  :class:`PasskeyNotFound` (not a leaky 403);
* unknown credential id → :class:`PasskeyNotFound`;
* last-credential gate refuses the revoke and leaves state intact.

Mirrors the fixture shape of :mod:`tests.unit.auth.test_passkey_register`
so cross-test patterns stay consistent.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional passkeys" and
``docs/specs/15-security-privacy.md`` §"Shared-origin XSS containment"
for the spec surface.
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
from app.adapters.db.identity.models import PasskeyCredential
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.auth.passkey import (
    LastPasskeyCredential,
    PasskeyNotFound,
    revoke_passkey,
)
from app.auth.session import issue as session_issue
from app.auth.webauthn import bytes_to_base64url
from app.config import Settings
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures — mirror :mod:`tests.unit.auth.test_passkey_register`
# ---------------------------------------------------------------------------


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
def workspace(session: Session) -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug="revoke-test",
        name="Revoke Test",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


@pytest.fixture
def user_ctx(session: Session, workspace: Workspace) -> WorkspaceContext:
    """Bootstrap a user and return a ctx pinned to their id."""
    user = bootstrap_user(
        session,
        email="revoke@example.com",
        display_name="Revoke Tester",
        clock=FrozenClock(_PINNED),
    )
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000RVKA",
    )


@pytest.fixture
def settings() -> Settings:
    """Settings stub with a stable root key for session issue/validate."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-passkey-revoke-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


def _seed_credential(
    session: Session,
    *,
    user_id: str,
    credential_id: bytes,
    transports: str | None = "internal",
    label: str | None = None,
) -> PasskeyCredential:
    """Insert one :class:`PasskeyCredential` row bound to ``user_id``."""
    row = PasskeyCredential(
        id=credential_id,
        user_id=user_id,
        public_key=b"\xaa" * 64,
        sign_count=0,
        transports=transports,
        backup_eligible=False,
        label=label,
        created_at=_PINNED,
        last_used_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _seed_session(
    session: Session,
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue one session for ``user_id`` and return its id (sha256-hex PK)."""
    issued = session_issue(
        session,
        user_id=user_id,
        has_owner_grant=True,
        ua="ua",
        ip="ip",
        now=_PINNED,
        settings=settings,
    )
    return issued.session_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRevokePasskey:
    """Happy-path revoke: drops row, audits, invalidates sessions."""

    def test_drops_credential_row(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        cid = b"\x11" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)
        # Seed a second credential so the last-credential gate doesn't fire.
        _seed_credential(
            session,
            user_id=user_ctx.actor_id,
            credential_id=b"\x22" * 32,
        )

        revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )

        assert session.get(PasskeyCredential, cid) is None
        # The sibling credential stays.
        assert session.get(PasskeyCredential, b"\x22" * 32) is not None

    def test_returns_credential_id_b64url(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        cid = b"\x11" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=b"\x22" * 32)

        result = revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )
        assert result == bytes_to_base64url(cid)

    def test_emits_passkey_revoked_before_session_invalidated(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spec requires ``passkey.revoked`` to land BEFORE
        ``session.invalidated`` so the forensic trail reads in
        cause-then-effect order."""
        # Point the session module at the same settings so the pepper is
        # stable across seed + invalidate.
        from app.auth import session as session_module

        monkeypatch.setattr(
            session_module, "get_settings", lambda: settings, raising=False
        )

        cid = b"\x11" * 32
        _seed_credential(
            session, user_id=user_ctx.actor_id, credential_id=cid, label="work phone"
        )
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=b"\x22" * 32)
        _seed_session(session, user_id=user_ctx.actor_id, settings=settings)

        revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )

        # Two audits of interest — passkey.revoked and
        # session.invalidated. ULIDs in ``AuditLog.id`` are monotonic,
        # so the row ordered first by id is the one written first.
        audits = list(
            session.scalars(
                select(AuditLog)
                .where(AuditLog.action.in_(["passkey.revoked", "session.invalidated"]))
                .order_by(AuditLog.id)
            ).all()
        )
        actions = [a.action for a in audits]
        assert actions == ["passkey.revoked", "session.invalidated"], (
            f"audit order: {actions!r}"
        )

    def test_passkey_revoked_audit_diff_shape(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        cid = b"\x11" * 32
        _seed_credential(
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            transports="internal,hybrid",
            label="wife's iPad",
        )
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=b"\x22" * 32)

        revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )

        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "passkey.revoked")
        ).one()
        assert audit.entity_kind == "passkey_credential"
        assert audit.entity_id == bytes_to_base64url(cid)
        assert isinstance(audit.diff, dict)
        assert audit.diff["user_id"] == user_ctx.actor_id
        assert audit.diff["transports"] == "internal,hybrid"
        assert audit.diff["backup_eligible"] is False
        assert audit.diff["label"] == "wife's iPad"

    def test_invalidates_every_active_session(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.auth import session as session_module

        monkeypatch.setattr(
            session_module, "get_settings", lambda: settings, raising=False
        )

        cid = b"\x11" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=b"\x22" * 32)
        # Two live sessions for the same user — both should flip.
        for _ in range(2):
            _seed_session(session, user_id=user_ctx.actor_id, settings=settings)

        revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )

        rows = session.scalars(
            select(SessionRow).where(SessionRow.user_id == user_ctx.actor_id)
        ).all()
        assert len(rows) == 2
        for row in rows:
            assert row.invalidated_at is not None
            assert row.invalidation_cause == "passkey_revoked"

    def test_session_invalidated_audit_cause(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.auth import session as session_module

        monkeypatch.setattr(
            session_module, "get_settings", lambda: settings, raising=False
        )

        cid = b"\x11" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=b"\x22" * 32)
        _seed_session(session, user_id=user_ctx.actor_id, settings=settings)

        revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )

        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).one()
        assert isinstance(audit.diff, dict)
        assert audit.diff["cause"] == "passkey_revoked"
        # One session seeded, one invalidated.
        assert audit.diff["count"] == 1

    def test_no_sessions_still_emits_audits(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        """The audit trail still lands when the user has no live sessions.

        ``session.invalidated`` carries ``count=0``; ``passkey.revoked``
        lands unchanged.
        """
        cid = b"\x11" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=b"\x22" * 32)

        revoke_passkey(
            user_ctx,
            session,
            user_id=user_ctx.actor_id,
            credential_id=cid,
            now=_PINNED,
        )

        actions = list(
            session.scalars(
                select(AuditLog.action)
                .where(AuditLog.action.in_(["passkey.revoked", "session.invalidated"]))
                .order_by(AuditLog.id)
            ).all()
        )
        assert actions == ["passkey.revoked", "session.invalidated"]
        invalidate_audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).one()
        assert isinstance(invalidate_audit.diff, dict)
        assert invalidate_audit.diff["count"] == 0


# ---------------------------------------------------------------------------
# Ownership / not-found
# ---------------------------------------------------------------------------


class TestRevokePasskeyOwnership:
    """Ownership gate — wrong owner and unknown id collapse to one type."""

    def test_unknown_credential_raises_not_found(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        with pytest.raises(PasskeyNotFound):
            revoke_passkey(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                credential_id=b"\xff" * 32,
                now=_PINNED,
            )

    def test_credential_owned_by_another_user_raises_not_found(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        """Another user's credential is indistinguishable from "unknown"."""
        other = bootstrap_user(
            session,
            email="stranger@example.com",
            display_name="Stranger",
            clock=FrozenClock(_PINNED),
        )
        cid = b"\x33" * 32
        _seed_credential(session, user_id=other.id, credential_id=cid)

        with pytest.raises(PasskeyNotFound):
            revoke_passkey(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                credential_id=cid,
                now=_PINNED,
            )
        # The stranger's credential stays intact — no cross-user blast.
        assert session.get(PasskeyCredential, cid) is not None

    def test_wrong_owner_writes_no_audit(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        """A rejected revoke must not leave a forensic trail of a partial op."""
        other = bootstrap_user(
            session,
            email="other@example.com",
            display_name="Other",
            clock=FrozenClock(_PINNED),
        )
        cid = b"\x44" * 32
        _seed_credential(session, user_id=other.id, credential_id=cid)

        with pytest.raises(PasskeyNotFound):
            revoke_passkey(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                credential_id=cid,
                now=_PINNED,
            )
        # Nothing was committed.
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.action.in_(["passkey.revoked", "session.invalidated"])
            )
        ).all()
        assert list(audits) == []


# ---------------------------------------------------------------------------
# Last-credential gate
# ---------------------------------------------------------------------------


class TestRevokePasskeyLastCredential:
    """Refuse to revoke the user's only remaining passkey."""

    def test_last_credential_raises_and_leaves_row_intact(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        cid = b"\x55" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)

        with pytest.raises(LastPasskeyCredential):
            revoke_passkey(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                credential_id=cid,
                now=_PINNED,
            )
        # Row still there — no destructive write lands.
        assert session.get(PasskeyCredential, cid) is not None

    def test_last_credential_writes_no_audit(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
    ) -> None:
        cid = b"\x66" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)

        with pytest.raises(LastPasskeyCredential):
            revoke_passkey(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                credential_id=cid,
                now=_PINNED,
            )
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.action.in_(["passkey.revoked", "session.invalidated"])
            )
        ).all()
        assert list(audits) == []

    def test_last_credential_does_not_invalidate_sessions(
        self,
        session: Session,
        user_ctx: WorkspaceContext,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refused revoke must not invalidate the user's live session."""
        from app.auth import session as session_module

        monkeypatch.setattr(
            session_module, "get_settings", lambda: settings, raising=False
        )

        cid = b"\x77" * 32
        _seed_credential(session, user_id=user_ctx.actor_id, credential_id=cid)
        _seed_session(session, user_id=user_ctx.actor_id, settings=settings)

        with pytest.raises(LastPasskeyCredential):
            revoke_passkey(
                user_ctx,
                session,
                user_id=user_ctx.actor_id,
                credential_id=cid,
                now=_PINNED,
            )
        # Session still live.
        rows = session.scalars(
            select(SessionRow).where(SessionRow.user_id == user_ctx.actor_id)
        ).all()
        for row in rows:
            assert row.invalidated_at is None
            assert row.invalidation_cause is None
