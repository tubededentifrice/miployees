"""Unit tests for :func:`app.auth.session.invalidate_for_user` and
:func:`app.auth.session.invalidate_for_credential`.

Non-destructive session invalidation (cd-geqp, §15 "Cookies" /
"Passkey specifics"). Covers:

* ``invalidate_for_user`` marks every active session for the user as
  ``invalidated_at = now`` + ``invalidation_cause = cause``;
* rows stay in the table (vs. :func:`revoke` which deletes);
* ``validate`` after invalidate raises :class:`SessionInvalid`;
* ``invalidate_for_credential`` scopes the invalidate to the
  credential's owner;
* the ``except_session_id`` preserves the caller's own session
  (parity with :func:`revoke_all_for_user`);
* a single ``session.invalidated`` audit row lands per call with
  ``cause`` in the diff;
* ``revoke`` remains destructive — distinct semantics;
* edge cases: user with no sessions, already-invalidated sessions
  (re-flag), mixed user rows (cross-user isolation), missing
  credential id.

See ``docs/specs/15-security-privacy.md`` §"Cookies",
§"Passkey specifics" and the cd-geqp task brief.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import PasskeyCredential
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.session import make_engine
from app.auth.session import (
    SessionInvalid,
    invalidate_for_credential,
    invalidate_for_user,
    issue,
    revoke,
    revoke_all_for_user,
    validate,
)
from app.config import Settings
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise SQLite-read datetimes to aware UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-session-invalidate-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
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
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


# ---------------------------------------------------------------------------
# ``invalidate_for_user``
# ---------------------------------------------------------------------------


class TestInvalidateForUser:
    """Marks every active session for a user as invalidated; keeps rows."""

    def test_marks_every_session_invalidated_returns_count(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="i@example.com", display_name="I")
        for _ in range(3):
            issue(
                db_session,
                user_id=user.id,
                has_owner_grant=False,
                ua="ua",
                ip="ip",
                now=_PINNED,
                settings=settings,
            )
        count = invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=_PINNED,
        )
        assert count == 3
        # Rows still exist — invalidation is non-destructive.
        rows = db_session.scalars(select(SessionRow)).all()
        assert len(rows) == 3
        for row in rows:
            assert row.invalidated_at is not None
            assert _as_utc(row.invalidated_at) == _PINNED
            assert row.invalidation_cause == "passkey_registered"

    def test_validate_after_invalidate_raises_invalid(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="v@example.com", display_name="V")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="recovery_consumed",
            now=_PINNED,
        )
        with pytest.raises(SessionInvalid, match="invalidated"):
            validate(
                db_session,
                cookie_value=result.cookie_value,
                now=_PINNED + timedelta(minutes=1),
                settings=settings,
            )

    def test_audit_row_carries_cause(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="a@example.com", display_name="A")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["cause"] == "passkey_registered"
        assert audits[0].diff["user_id"] == user.id
        assert audits[0].diff["count"] == 1

    def test_single_audit_row_regardless_of_count(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="s@example.com", display_name="S")
        for _ in range(4):
            issue(
                db_session,
                user_id=user.id,
                has_owner_grant=False,
                ua="ua",
                ip="ip",
                now=_PINNED,
                settings=settings,
            )
        invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["count"] == 4

    def test_except_session_is_preserved(
        self, db_session: Session, settings: Settings
    ) -> None:
        """``except_session_id`` keeps the caller's session live."""
        user = bootstrap_user(db_session, email="ex@example.com", display_name="Ex")
        keep = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        for _ in range(2):
            issue(
                db_session,
                user_id=user.id,
                has_owner_grant=False,
                ua="ua",
                ip="ip",
                now=_PINNED,
                settings=settings,
            )
        count = invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            except_session_id=keep.session_id,
            now=_PINNED,
        )
        assert count == 2
        kept_row = db_session.get(SessionRow, keep.session_id)
        assert kept_row is not None
        assert kept_row.invalidated_at is None
        assert kept_row.invalidation_cause is None

    def test_other_users_sessions_untouched(
        self, db_session: Session, settings: Settings
    ) -> None:
        alice = bootstrap_user(db_session, email="a@example.com", display_name="A")
        bob = bootstrap_user(db_session, email="b@example.com", display_name="B")
        a_issue = issue(
            db_session,
            user_id=alice.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        b_issue = issue(
            db_session,
            user_id=bob.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        invalidate_for_user(
            db_session,
            user_id=alice.id,
            cause="passkey_registered",
            now=_PINNED,
        )
        a_row = db_session.get(SessionRow, a_issue.session_id)
        b_row = db_session.get(SessionRow, b_issue.session_id)
        assert a_row is not None
        assert b_row is not None
        assert a_row.invalidated_at is not None
        assert b_row.invalidated_at is None

    def test_zero_sessions_still_audits(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="z@example.com", display_name="Z")
        count = invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=_PINNED,
        )
        assert count == 0
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["count"] == 0

    def test_already_invalidated_session_not_re_flagged(
        self, db_session: Session, settings: Settings
    ) -> None:
        """A second invalidate only picks up still-active rows."""
        user = bootstrap_user(db_session, email="re@example.com", display_name="Re")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        first_now = _PINNED
        invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=first_now,
        )
        # Second call — the row is already invalidated, so count=0 and
        # the stored cause / at don't move.
        second_now = _PINNED + timedelta(hours=1)
        count = invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="recovery_consumed",
            now=second_now,
        )
        assert count == 0
        row = db_session.scalars(select(SessionRow)).one()
        assert row.invalidated_at is not None
        assert _as_utc(row.invalidated_at) == first_now
        assert row.invalidation_cause == "passkey_registered"

    def test_expired_session_not_picked_up(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Already-expired (idle timeout) rows aren't re-flagged."""
        user = bootstrap_user(db_session, email="ex2@example.com", display_name="Ex2")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        # Call invalidate_for_user at a moment past the idle TTL
        # (31 days) — the row still exists (not deleted) but is already
        # past ``expires_at``, so the active filter skips it.
        future = _PINNED + timedelta(days=31)
        count = invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=future,
        )
        assert count == 0
        row = db_session.scalars(select(SessionRow)).one()
        assert row.invalidated_at is None


# ---------------------------------------------------------------------------
# ``invalidate_for_credential``
# ---------------------------------------------------------------------------


def _seed_credential(
    db_session: Session, *, user_id: str, credential_id: bytes
) -> None:
    """Insert a minimal ``passkey_credential`` row bound to ``user_id``."""
    row = PasskeyCredential(
        id=credential_id,
        user_id=user_id,
        public_key=b"\x00" * 32,
        sign_count=0,
        transports=None,
        backup_eligible=False,
        label=None,
        created_at=_PINNED,
        last_used_at=None,
    )
    db_session.add(row)
    db_session.flush()


class TestInvalidateForCredential:
    """Scopes the invalidate to the credential owner; §15 clone-detect hook."""

    def test_invalidates_owner_sessions_only(
        self, db_session: Session, settings: Settings
    ) -> None:
        alice = bootstrap_user(db_session, email="a@example.com", display_name="A")
        bob = bootstrap_user(db_session, email="b@example.com", display_name="B")
        _seed_credential(db_session, user_id=alice.id, credential_id=b"alice-cred-01")
        a_issue = issue(
            db_session,
            user_id=alice.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        b_issue = issue(
            db_session,
            user_id=bob.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )

        count = invalidate_for_credential(
            db_session,
            credential_id=b"alice-cred-01",
            cause="clone_detected",
            now=_PINNED,
        )
        assert count == 1
        a_row = db_session.get(SessionRow, a_issue.session_id)
        b_row = db_session.get(SessionRow, b_issue.session_id)
        assert a_row is not None
        assert b_row is not None
        assert a_row.invalidated_at is not None
        assert a_row.invalidation_cause == "clone_detected"
        assert b_row.invalidated_at is None

    def test_audit_row_shapes_match_invalidate_for_user(
        self, db_session: Session, settings: Settings
    ) -> None:
        alice = bootstrap_user(db_session, email="a2@example.com", display_name="A")
        _seed_credential(db_session, user_id=alice.id, credential_id=b"cred-02")
        issue(
            db_session,
            user_id=alice.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        invalidate_for_credential(
            db_session,
            credential_id=b"cred-02",
            cause="clone_detected",
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["cause"] == "clone_detected"
        assert audits[0].diff["user_id"] == alice.id
        assert audits[0].diff["count"] == 1

    def test_missing_credential_still_audits(self, db_session: Session) -> None:
        """A credential that doesn't exist → 0-count audit row lands."""
        count = invalidate_for_credential(
            db_session,
            credential_id=b"nonexistent",
            cause="clone_detected",
            now=_PINNED,
        )
        assert count == 0
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.invalidated")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["note"] == "credential_not_found"
        assert audits[0].diff["count"] == 0


# ---------------------------------------------------------------------------
# ``revoke`` remains destructive — distinct from invalidate
# ---------------------------------------------------------------------------


class TestRevokeStaysDestructive:
    """Semantic check: invalidate keeps rows; revoke still deletes them."""

    def test_revoke_deletes_row(self, db_session: Session, settings: Settings) -> None:
        user = bootstrap_user(db_session, email="rd@example.com", display_name="RD")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        revoke(db_session, session_id=result.session_id, now=_PINNED)
        rows = db_session.scalars(select(SessionRow)).all()
        assert rows == []

    def test_invalidate_keeps_row(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="ik@example.com", display_name="IK")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        invalidate_for_user(
            db_session,
            user_id=user.id,
            cause="passkey_registered",
            now=_PINNED,
        )
        rows = db_session.scalars(select(SessionRow)).all()
        assert len(rows) == 1

    def test_revoke_all_for_user_still_deletes(
        self, db_session: Session, settings: Settings
    ) -> None:
        """``revoke_all_for_user`` preserves its pre-cd-geqp destructive shape."""
        user = bootstrap_user(db_session, email="rav@example.com", display_name="RAV")
        for _ in range(2):
            issue(
                db_session,
                user_id=user.id,
                has_owner_grant=False,
                ua="ua",
                ip="ip",
                now=_PINNED,
                settings=settings,
            )
        revoke_all_for_user(db_session, user_id=user.id, now=_PINNED)
        rows = db_session.scalars(select(SessionRow)).all()
        assert rows == []
