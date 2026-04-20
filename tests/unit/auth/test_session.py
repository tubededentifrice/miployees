"""Unit tests for :mod:`app.auth.session` and :mod:`app.auth.csrf`.

Covers both domain services + the cookie-builder + CSRF middleware,
against an in-memory SQLite engine with :class:`Base.metadata` schema.

Matrix (cd-cyq acceptance + spec §03 / §15):

* ``issue`` — happy path, owner vs non-owner TTL, UA / IP hashed,
  audit row, opaque cookie-value shape.
* ``validate`` — happy path returns user_id, expired raises,
  unknown raises, sliding-refresh gates (before half-life vs after),
  ``last_seen_at`` bumped on every call.
* ``revoke`` — row deleted, audit row lands, idempotent miss.
* ``revoke_all_for_user`` — every row gone, count returned, `except`
  session preserved, single audit row per call regardless of count.
* ``build_session_cookie`` — exact flag string, ``Domain`` absent,
  ``__Host-`` prefix enforced with ``secure=True``, ``secure=False``
  rejected.
* ``CSRFMiddleware`` — GET skip, POST with matching pair → 200, POST
  mismatch → 403, POST missing header → 403, cookie re-minted on
  every response.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/15-security-privacy.md`` §"Cookies".
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
from app.adapters.db.base import Base
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.session import make_engine
from app.auth._hashing import hash_with_pepper
from app.auth.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
)
from app.auth.keys import derive_subkey
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SessionExpired,
    SessionInvalid,
    build_session_cookie,
    hash_cookie_value,
    issue,
    revoke,
    revoke_all_for_user,
    validate,
)
from app.config import Settings
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetime read back from SQLite to aware-UTC.

    SQLite stores ``DateTime(timezone=True)`` as a naive ISO string
    and reads it back without tzinfo. Every downstream comparison
    against an aware datetime needs to bridge that gap. Postgres
    round-trips tzinfo losslessly, so this is a no-op there.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Minimal :class:`Settings` with just the keys the service reads."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-session-root-key"),
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
# ``issue``
# ---------------------------------------------------------------------------


class TestIssue:
    """``issue`` inserts a row, returns an opaque cookie, audits."""

    def test_happy_path_inserts_row(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="h@example.com", display_name="H")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Mozilla/5.0",
            ip="198.51.100.1",
            now=_PINNED,
            settings=settings,
        )

        rows = db_session.scalars(select(SessionRow)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == result.session_id
        assert row.user_id == user.id
        assert row.workspace_id is None
        # SQLite drops tzinfo on ``DateTime(timezone=True)`` read; the
        # naive + UTC-aware values compare equal under ``astimezone``.
        assert _as_utc(row.created_at) == _PINNED
        assert _as_utc(row.last_seen_at) == _PINNED

    def test_cookie_value_is_opaque_urlsafe(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Cookie value is at least 32 chars urlsafe, no whitespace."""
        user = bootstrap_user(db_session, email="c@example.com", display_name="C")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        assert len(result.cookie_value) >= 32
        # token_urlsafe uses [A-Za-z0-9_-] (RFC 4648 base64url).
        assert all(ch.isalnum() or ch in ("_", "-") for ch in result.cookie_value)
        # Distinct from the row id — the row id is the sha256-hex.
        assert result.cookie_value != result.session_id

    def test_session_id_is_sha256_hex_of_cookie(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="x@example.com", display_name="X")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        assert result.session_id == hash_cookie_value(result.cookie_value)
        assert len(result.session_id) == 64  # sha256 hex digest.
        assert all(ch in "0123456789abcdef" for ch in result.session_id)

    def test_owner_ttl_is_seven_days(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="o@example.com", display_name="O")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=True,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        assert result.expires_at - _PINNED == timedelta(days=7)

    def test_non_owner_ttl_is_thirty_days(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="w@example.com", display_name="W")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        assert result.expires_at - _PINNED == timedelta(days=30)

    def test_ua_and_ip_are_peppered_hashes(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="p@example.com", display_name="P")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Mozilla/Firefox",
            ip="203.0.113.9",
            now=_PINNED,
            settings=settings,
        )
        pepper = derive_subkey(settings.root_key, purpose="session-hash")
        row = db_session.scalars(select(SessionRow)).one()
        assert row.ua_hash == hash_with_pepper("Mozilla/Firefox", pepper)
        assert row.ip_hash == hash_with_pepper("203.0.113.9", pepper)
        # Plaintext must not appear in the hashed columns.
        assert row.ua_hash is not None
        assert "Mozilla" not in row.ua_hash
        assert row.ip_hash is not None
        assert "203." not in row.ip_hash

    def test_workspace_id_is_stored_when_supplied(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="ws@example.com", display_name="WS")
        # Seed a workspace FK target — the Session model cascades on its
        # parent's delete, so the FK must point at a real row.
        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic

        ws_id = "01HWA00000000000000000WSPA"
        with tenant_agnostic():
            db_session.add(
                Workspace(
                    id=ws_id,
                    slug="ws-unit",
                    name="Unit WS",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()

        issue(
            db_session,
            user_id=user.id,
            workspace_id=ws_id,
            has_owner_grant=True,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert row.workspace_id == ws_id

    def test_audit_row_created_with_hashes(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="a@example.com", display_name="A")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=True,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        audit_rows = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.created")
        ).all()
        assert len(audit_rows) == 1
        row = audit_rows[0]
        assert row.entity_kind == "session"
        assert row.entity_id == result.session_id
        assert isinstance(row.diff, dict)
        assert row.diff["user_id"] == user.id
        assert row.diff["has_owner_grant"] is True
        assert row.diff["ttl_seconds"] == int(timedelta(days=7).total_seconds())
        # No plaintext in audit diff.
        assert "ua_hash" in row.diff
        assert "ip_hash" in row.diff


# ---------------------------------------------------------------------------
# ``validate``
# ---------------------------------------------------------------------------


class TestValidate:
    """``validate`` returns user_id on success; raises typed errors otherwise."""

    def test_happy_path_returns_user_id(
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
        resolved = validate(
            db_session,
            cookie_value=result.cookie_value,
            now=_PINNED + timedelta(hours=1),
            settings=settings,
        )
        assert resolved == user.id

    def test_unknown_cookie_raises_invalid(
        self, db_session: Session, settings: Settings
    ) -> None:
        with pytest.raises(SessionInvalid):
            validate(
                db_session,
                cookie_value="this-cookie-was-never-issued",
                now=_PINNED,
                settings=settings,
            )

    def test_expired_raises_expired(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="e@example.com", display_name="E")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        # 31 days later — past the 30-day non-owner lifetime.
        with pytest.raises(SessionExpired):
            validate(
                db_session,
                cookie_value=result.cookie_value,
                now=_PINNED + timedelta(days=31),
                settings=settings,
            )

    def test_last_seen_at_bumped_on_every_call(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="ls@example.com", display_name="LS")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        later = _PINNED + timedelta(hours=1)
        validate(
            db_session,
            cookie_value=result.cookie_value,
            now=later,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert _as_utc(row.last_seen_at) == later

    def test_sliding_refresh_not_fired_before_halflife(
        self, db_session: Session, settings: Settings
    ) -> None:
        """A validate call before the halflife mark does NOT extend expires_at."""
        user = bootstrap_user(db_session, email="b4@example.com", display_name="B4")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=True,  # 7-day lifetime
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        original_expires = result.expires_at
        # 2 days in — well before the 3.5-day halflife.
        validate(
            db_session,
            cookie_value=result.cookie_value,
            now=_PINNED + timedelta(days=2),
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert _as_utc(row.expires_at) == original_expires
        # No refresh audit row.
        refresh_rows = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.refreshed")
        ).all()
        assert refresh_rows == []

    def test_sliding_refresh_fired_past_halflife(
        self, db_session: Session, settings: Settings
    ) -> None:
        """A validate call past the halflife mark extends expires_at."""
        user = bootstrap_user(db_session, email="af@example.com", display_name="AF")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=True,  # 7-day lifetime
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        # 5 days in — past the 3.5-day halflife.
        later = _PINNED + timedelta(days=5)
        validate(
            db_session,
            cookie_value=result.cookie_value,
            now=later,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        # New expires_at == now + original_ttl (7 days from later).
        assert _as_utc(row.expires_at) == later + timedelta(days=7)
        # Refresh audit row landed.
        refresh_rows = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.refreshed")
        ).all()
        assert len(refresh_rows) == 1

    def test_sliding_refresh_not_fired_at_exact_halflife(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Boundary: at exactly ttl/2 elapsed, refresh does not fire (strict >)."""
        user = bootstrap_user(db_session, email="eq@example.com", display_name="EQ")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=True,  # 7-day lifetime
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        original_expires = result.expires_at
        validate(
            db_session,
            cookie_value=result.cookie_value,
            now=_PINNED + timedelta(days=3, hours=12),  # exact halflife
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert _as_utc(row.expires_at) == original_expires


# ---------------------------------------------------------------------------
# Absolute expiry (§15 "Cookies") — hard 90-day cap
# ---------------------------------------------------------------------------


class TestAbsoluteExpiry:
    """``issue`` stamps a 90-day absolute cap; ``validate`` enforces it."""

    def test_absolute_expires_at_is_ninety_days(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="abs@example.com", display_name="Abs")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert row.absolute_expires_at is not None
        assert _as_utc(row.absolute_expires_at) == _PINNED + timedelta(days=90)

    def test_validate_raises_expired_past_absolute_cap(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Past 90d cap, validate raises SessionExpired regardless of idle TTL."""
        user = bootstrap_user(db_session, email="cap@example.com", display_name="Cap")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        # 91 days in — past the 90d absolute cap. The idle TTL is only
        # 30d, so idle would ALSO have fired; we force-update
        # expires_at forward to isolate the absolute gate.
        row = db_session.scalars(select(SessionRow)).one()
        row.expires_at = _PINNED + timedelta(days=200)
        db_session.flush()
        with pytest.raises(SessionExpired, match="absolute cap"):
            validate(
                db_session,
                cookie_value=result.cookie_value,
                now=_PINNED + timedelta(days=91),
                settings=settings,
            )

    def test_sliding_refresh_clipped_to_absolute_cap(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Refresh cannot push ``expires_at`` past the absolute cap.

        The natural idle TTLs (7d / 30d) never push past the 90d cap
        on a single refresh, so we simulate a long-lived session by
        manually extending ``expires_at`` to day 88 and calling
        ``validate`` at day 85 — refresh would want day 85 + 7 = day 92
        but must clip to day 90.
        """
        user = bootstrap_user(db_session, email="cl@example.com", display_name="Cl")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=True,  # 7-day idle TTL
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        # Simulate a session that has been rolling-refreshed up to day 88
        # (still within the 90-day absolute cap).
        row.expires_at = _PINNED + timedelta(days=88)
        db_session.flush()

        # At day 85, past the half-life of the 7-day idle window,
        # refresh wants expires_at = day 85 + 7 = day 92 > cap.
        later = _PINNED + timedelta(days=85)
        validate(
            db_session,
            cookie_value=result.cookie_value,
            now=later,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert _as_utc(row.expires_at) == _PINNED + timedelta(days=90)
        # Absolute cap didn't move.
        assert row.absolute_expires_at is not None
        assert _as_utc(row.absolute_expires_at) == _PINNED + timedelta(days=90)

    def test_pre_hardening_row_without_cap_still_validates(
        self, db_session: Session, settings: Settings
    ) -> None:
        """A row with ``absolute_expires_at = NULL`` skips the cap gate."""
        user = bootstrap_user(db_session, email="pre@example.com", display_name="Pre")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        # Simulate a pre-hardening row by nulling the column.
        row = db_session.scalars(select(SessionRow)).one()
        row.absolute_expires_at = None
        db_session.flush()
        # Should still succeed on the idle gate.
        resolved = validate(
            db_session,
            cookie_value=result.cookie_value,
            now=_PINNED + timedelta(hours=1),
            settings=settings,
        )
        assert resolved == user.id


# ---------------------------------------------------------------------------
# Fingerprint (§15 "Shared-origin XSS containment")
# ---------------------------------------------------------------------------


class TestFingerprint:
    """``issue`` stamps a fingerprint; ``validate`` audits + raises on mismatch."""

    def test_fingerprint_stamped_on_issue(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="fp@example.com", display_name="FP")
        issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Mozilla/5.0",
            ip="1.2.3.4",
            accept_language="en-US,en;q=0.9",
            now=_PINNED,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        assert row.fingerprint_hash is not None
        assert len(row.fingerprint_hash) == 64  # sha256 hex
        # Plaintext never appears.
        assert "Mozilla" not in row.fingerprint_hash
        assert "en-US" not in row.fingerprint_hash

    def test_fingerprint_mismatch_raises_invalid(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="fm@example.com", display_name="FM")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Mozilla/5.0",
            ip="1.2.3.4",
            accept_language="en-US",
            now=_PINNED,
            settings=settings,
        )
        with pytest.raises(SessionInvalid, match="fingerprint"):
            validate(
                db_session,
                cookie_value=result.cookie_value,
                ua="Chrome/100",  # different UA
                accept_language="en-US",
                now=_PINNED + timedelta(hours=1),
                settings=settings,
            )

    def test_fingerprint_match_validates(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="fh@example.com", display_name="FH")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Firefox/120",
            ip="1.2.3.4",
            accept_language="fr-FR",
            now=_PINNED,
            settings=settings,
        )
        resolved = validate(
            db_session,
            cookie_value=result.cookie_value,
            ua="Firefox/120",
            accept_language="fr-FR",
            now=_PINNED + timedelta(hours=1),
            settings=settings,
        )
        assert resolved == user.id

    def test_fingerprint_mismatch_writes_audit(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="fa@example.com", display_name="FA")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Safari/17",
            ip="1.2.3.4",
            accept_language="en-GB",
            now=_PINNED,
            settings=settings,
        )
        with pytest.raises(SessionInvalid):
            validate(
                db_session,
                cookie_value=result.cookie_value,
                ua="Edge/120",
                accept_language="en-GB",
                now=_PINNED + timedelta(hours=1),
                settings=settings,
            )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.fingerprint_mismatch")
        ).all()
        assert len(audits) == 1
        assert audits[0].entity_id == result.session_id

    def test_legacy_validate_without_headers_skips_fingerprint_gate(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Caller that doesn't pass ``ua`` / ``accept_language`` skips the gate.

        Rollout-safety escape: the tenancy middleware currently calls
        ``validate(cookie_value=...)`` without forwarding headers.
        Until cd-geqp's follow-up wires the headers through, that path
        must keep working — the gate is additive for opt-in callers.
        """
        user = bootstrap_user(db_session, email="lg@example.com", display_name="LG")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="Mozilla/5.0",
            ip="1.2.3.4",
            accept_language="en-US",
            now=_PINNED,
            settings=settings,
        )
        resolved = validate(
            db_session,
            cookie_value=result.cookie_value,
            now=_PINNED + timedelta(hours=1),
            settings=settings,
        )
        assert resolved == user.id

    def test_pre_hardening_row_with_null_fingerprint_validates(
        self, db_session: Session, settings: Settings
    ) -> None:
        """A row with ``fingerprint_hash = NULL`` skips the gate."""
        user = bootstrap_user(db_session, email="ph@example.com", display_name="PH")
        result = issue(
            db_session,
            user_id=user.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        row = db_session.scalars(select(SessionRow)).one()
        row.fingerprint_hash = None
        db_session.flush()
        # Caller now passes headers; gate still skips because row is null.
        resolved = validate(
            db_session,
            cookie_value=result.cookie_value,
            ua="different",
            accept_language="zz-ZZ",
            now=_PINNED + timedelta(hours=1),
            settings=settings,
        )
        assert resolved == user.id


# ---------------------------------------------------------------------------
# ``revoke``
# ---------------------------------------------------------------------------


class TestRevoke:
    """``revoke`` deletes the row + audits; idempotent on missing."""

    def test_deletes_row_and_audits(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="r@example.com", display_name="R")
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
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.revoked")
        ).all()
        assert len(audits) == 1
        assert audits[0].entity_id == result.session_id
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["existed"] is True

    def test_idempotent_on_missing_row(
        self, db_session: Session, settings: Settings
    ) -> None:
        """A second revoke is a no-op on the row but still audits."""
        user = bootstrap_user(db_session, email="r2@example.com", display_name="R2")
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
        revoke(db_session, session_id=result.session_id, now=_PINNED)
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.revoked")
        ).all()
        assert len(audits) == 2
        # The second audit records ``existed=False``.
        assert isinstance(audits[0].diff, dict)
        assert isinstance(audits[1].diff, dict)
        existed_flags = {
            audits[0].diff["existed"],
            audits[1].diff["existed"],
        }
        assert existed_flags == {True, False}

    def test_revoke_then_validate_raises_invalid(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="rv@example.com", display_name="RV")
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
        with pytest.raises(SessionInvalid):
            validate(
                db_session,
                cookie_value=result.cookie_value,
                now=_PINNED,
                settings=settings,
            )


# ---------------------------------------------------------------------------
# ``revoke_all_for_user``
# ---------------------------------------------------------------------------


class TestRevokeAllForUser:
    """``revoke_all_for_user`` wipes every row for a user; honours ``except``."""

    def test_revokes_every_session_returns_count(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="ra@example.com", display_name="RA")
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
        count = revoke_all_for_user(db_session, user_id=user.id, now=_PINNED)
        assert count == 3
        rows = db_session.scalars(select(SessionRow)).all()
        assert rows == []

    def test_except_session_is_preserved(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="ex@example.com", display_name="EX")
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
        count = revoke_all_for_user(
            db_session,
            user_id=user.id,
            except_session_id=keep.session_id,
            now=_PINNED,
        )
        assert count == 2
        rows = db_session.scalars(select(SessionRow)).all()
        assert len(rows) == 1
        assert rows[0].id == keep.session_id

    def test_single_audit_row_regardless_of_count(
        self, db_session: Session, settings: Settings
    ) -> None:
        user = bootstrap_user(db_session, email="ra2@example.com", display_name="RA2")
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
        revoke_all_for_user(db_session, user_id=user.id, now=_PINNED)
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.revoked_all")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["count"] == 4
        assert audits[0].diff["user_id"] == user.id

    def test_revokes_only_target_users_sessions(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Other users' sessions are untouched."""
        alice = bootstrap_user(db_session, email="a@example.com", display_name="A")
        bob = bootstrap_user(db_session, email="b@example.com", display_name="B")
        issue(
            db_session,
            user_id=alice.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        issue(
            db_session,
            user_id=bob.id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        revoke_all_for_user(db_session, user_id=alice.id, now=_PINNED)
        rows = db_session.scalars(select(SessionRow)).all()
        assert len(rows) == 1
        assert rows[0].user_id == bob.id

    def test_noop_for_user_with_no_sessions(
        self, db_session: Session, settings: Settings
    ) -> None:
        """Zero live sessions → count 0, audit still lands."""
        user = bootstrap_user(db_session, email="no@example.com", display_name="No")
        count = revoke_all_for_user(db_session, user_id=user.id, now=_PINNED)
        assert count == 0
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "session.revoked_all")
        ).all()
        assert len(audits) == 1


# ---------------------------------------------------------------------------
# ``build_session_cookie``
# ---------------------------------------------------------------------------


class TestBuildSessionCookie:
    """``build_session_cookie`` returns a spec-compliant ``Set-Cookie`` line."""

    def test_flags_exactly_per_spec(self) -> None:
        expires = _PINNED + timedelta(days=7)
        header = build_session_cookie("token-value", expires)
        assert header.startswith(f"{SESSION_COOKIE_NAME}=token-value;")
        assert "; Secure;" in header
        assert "; HttpOnly;" in header
        assert "; SameSite=Lax;" in header
        assert "; Path=/;" in header

    def test_no_domain_attribute_emitted(self) -> None:
        """``__Host-`` forbids ``Domain``; the builder never emits one."""
        expires = _PINNED + timedelta(days=7)
        header = build_session_cookie("token-value", expires)
        # Case-insensitive search for ``Domain=`` anywhere.
        assert "Domain=" not in header
        assert "domain=" not in header

    def test_secure_false_falls_back_to_dev_cookie(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``secure=False`` drops the ``__Host-`` prefix + Secure attr."""
        from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME

        expires = _PINNED + timedelta(days=7)
        with caplog.at_level("WARNING", logger="app.auth.session_cookie"):
            header = build_session_cookie("t", expires, secure=False)
        # No __Host- prefix in dev fallback.
        assert header.startswith(f"{DEV_SESSION_COOKIE_NAME}=t")
        assert SESSION_COOKIE_NAME not in header
        # No Secure attribute in dev fallback.
        assert "Secure" not in header.split("; ")
        # Warning must fire so the operator can't miss the plaintext-wire shape.
        assert any("secure=False" in r.message for r in caplog.records)

    def test_domain_attribute_always_rejected(self) -> None:
        """Supplying ``domain=...`` is forbidden under both prefix shapes."""
        expires = _PINNED + timedelta(days=7)
        with pytest.raises(ValueError, match="Domain"):
            build_session_cookie("t", expires, domain="example.com")

    def test_non_root_path_rejected_under_host_prefix(self) -> None:
        """``__Host-`` requires ``Path=/``; a non-default path is refused."""
        expires = _PINNED + timedelta(days=7)
        with pytest.raises(ValueError, match="Path=/"):
            build_session_cookie("t", expires, path="/admin")

    def test_naive_datetime_rejected(self) -> None:
        expires_naive = datetime(2026, 5, 1, 0, 0, 0)
        with pytest.raises(ValueError, match="aware"):
            build_session_cookie("t", expires_naive)

    def test_expires_rendered_as_imf_fixdate(self) -> None:
        """Expires attribute uses RFC 7231 IMF-fixdate with GMT suffix."""
        expires = datetime(2026, 5, 1, 12, 34, 56, tzinfo=UTC)
        header = build_session_cookie("t", expires)
        assert "Expires=Fri, 01 May 2026 12:34:56 GMT" in header

    def test_max_age_present(self) -> None:
        expires = _PINNED + timedelta(days=30)
        header = build_session_cookie("t", expires)
        # Max-Age should be a non-empty integer.
        for attr in header.split("; "):
            if attr.startswith("Max-Age="):
                assert attr[len("Max-Age=") :].isdigit()
                break
        else:  # pragma: no cover - defensive
            pytest.fail("Max-Age attribute missing")


# ---------------------------------------------------------------------------
# CSRF middleware — unit tests via a minimal FastAPI TestClient
# ---------------------------------------------------------------------------


def _make_csrf_app() -> FastAPI:
    """Return a FastAPI app with a single GET + POST route under CSRF."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/scoped/ok")
    def _get() -> dict[str, str]:
        return {"ok": "get"}

    @app.post("/scoped/write")
    def _post() -> dict[str, str]:
        return {"ok": "post"}

    return app


class TestCSRFMiddleware:
    """``CSRFMiddleware`` enforces the double-submit pair."""

    def test_get_skips_check(self) -> None:
        client = TestClient(_make_csrf_app(), base_url="https://testserver")
        r = client.get("/scoped/ok")
        assert r.status_code == 200
        # Cookie was minted on the response.
        assert CSRF_COOKIE_NAME in r.cookies

    def test_post_with_matching_pair_passes(self) -> None:
        client = TestClient(_make_csrf_app(), base_url="https://testserver")
        # Prime the cookie via a GET first — the client jar carries it.
        r0 = client.get("/scoped/ok")
        token = r0.cookies[CSRF_COOKIE_NAME]
        r = client.post(
            "/scoped/write",
            headers={CSRF_HEADER_NAME: token},
        )
        assert r.status_code == 200, r.text

    def test_post_with_mismatched_header_is_forbidden(self) -> None:
        client = TestClient(_make_csrf_app(), base_url="https://testserver")
        client.get("/scoped/ok")  # Populate the jar.
        r = client.post(
            "/scoped/write",
            headers={CSRF_HEADER_NAME: "tampered-value"},
        )
        assert r.status_code == 403
        assert r.json() == {"detail": "csrf_mismatch"}

    def test_post_without_header_is_forbidden(self) -> None:
        client = TestClient(_make_csrf_app(), base_url="https://testserver")
        client.get("/scoped/ok")  # Populate the jar.
        r = client.post("/scoped/write")
        assert r.status_code == 403

    def test_post_without_cookie_is_forbidden(self) -> None:
        client = TestClient(_make_csrf_app(), base_url="https://testserver")
        # No priming GET — the jar is empty. We still send a header
        # so the test exercises the "header present, cookie absent"
        # branch rather than the "neither" branch.
        r = client.post(
            "/scoped/write",
            headers={CSRF_HEADER_NAME: "some-token"},
        )
        assert r.status_code == 403

    def test_skip_path_bypasses_check(self) -> None:
        """``/healthz`` and other SKIP_PATHS never require CSRF pairs."""
        app = FastAPI()
        app.add_middleware(CSRFMiddleware)

        @app.post("/healthz")
        def _healthz() -> dict[str, str]:
            return {"ok": "healthz"}

        client = TestClient(app, base_url="https://testserver")
        r = client.post("/healthz")
        assert r.status_code == 200

    def test_cookie_refreshed_on_every_response(self) -> None:
        client = TestClient(_make_csrf_app(), base_url="https://testserver")
        r1 = client.get("/scoped/ok")
        t1 = r1.cookies[CSRF_COOKIE_NAME]
        r2 = client.get("/scoped/ok")
        t2 = r2.cookies[CSRF_COOKIE_NAME]
        # Fresh random each response — the probability of collision is
        # ~2^-192 per pair.
        assert t1 != t2
