"""Integration test for :mod:`app.auth.magic_link` — concurrent consume.

Spawns two threads both trying to redeem the same token against a
real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``). Exactly one must win; the other must
raise :class:`AlreadyConsumed`.

The single-use guarantee hinges on the conditional ``UPDATE …
WHERE consumed_at IS NULL`` in :func:`app.auth.magic_link.consume_link`:
SQLite's transaction serialisation and Postgres' row-level lock
(implicit under READ COMMITTED on the filtered update) both cause
the losing consumer to see ``rowcount == 0`` and raise.

See cd-4zz acceptance criteria and ``docs/specs/03-auth-and-tokens.md``
§"Magic link format".
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import MagicLinkNonce
from app.auth.magic_link import (
    AlreadyConsumed,
    MagicLinkOutcome,
    Throttle,
    consume_link,
    request_link,
)
from app.config import Settings

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@dataclass
class _RecordingMailer:
    sent: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)

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
        self.sent.append((tuple(to), subject, body_text))
        return "test-message-id"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-root-key"),
        public_url="https://crew.day",
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


def _extract_token(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError("no URL in body")


def _factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def isolated_engine(db_url: str) -> Iterator[Engine]:
    """Engine dedicated to the concurrent-consume test.

    The session-scoped ``engine`` fixture is paired with the
    ``db_session`` savepoint-rollback pattern: a ``session.commit()``
    inside a test commits only to the savepoint, not to the outer
    connection, so sibling worker threads opening their own
    connections through the same engine never observe the row.

    Concurrent consume needs a committed row that's visible to
    multiple threads, so we build a dedicated engine for this test,
    clean up the row by hand at the end, and leave the savepoint
    fixture out of the path entirely.
    """
    from app.adapters.db.session import make_engine

    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


class TestConcurrentConsume:
    """Two threads, one token, exactly one winner."""

    def test_exactly_one_winner(
        self,
        isolated_engine: Engine,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Both threads call :func:`consume_link`; one gets a
        :class:`MagicLinkOutcome`, the other :class:`AlreadyConsumed`.
        """
        mailer = _RecordingMailer()
        factory = _factory(isolated_engine)
        # Mint the nonce in a committed transaction so both worker
        # threads see the row.
        with factory() as s:
            # cd-9i7z: ``request_link`` returns a deferred-send
            # pending. Production routers commit before delivering;
            # we mirror that here so the recording mailer captures
            # the body only after the nonce row is durable.
            pending = request_link(
                s,
                email="concurrent@example.com",
                purpose="signup_verify",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
            s.commit()
            assert pending is not None
            pending.deliver()
            minted_jti = s.scalars(select(MagicLinkNonce.jti)).one()
        token = _extract_token(mailer.sent[0][2])

        outcomes: list[MagicLinkOutcome] = []
        errors: list[Exception] = []
        start = threading.Barrier(2)

        def _consume() -> None:
            try:
                with factory() as s:
                    start.wait()
                    try:
                        outcome = consume_link(
                            s,
                            token=token,
                            expected_purpose="signup_verify",
                            ip="127.0.0.1",
                            now=_PINNED + timedelta(minutes=1),
                            throttle=throttle,
                            settings=settings,
                        )
                        s.commit()
                        outcomes.append(outcome)
                    except AlreadyConsumed as exc:
                        s.rollback()
                        errors.append(exc)
            except Exception as exc:  # pragma: no cover - test-harness path
                errors.append(exc)

        t1 = threading.Thread(target=_consume)
        t2 = threading.Thread(target=_consume)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        try:
            # Exactly one thread got an outcome; the other got 409.
            assert len(outcomes) == 1, (
                f"expected 1 winner, got {len(outcomes)} (errors={errors})"
            )
            assert any(isinstance(e, AlreadyConsumed) for e in errors), errors
        finally:
            # Manual cleanup — drop the committed row so the next
            # test in this module sees a clean table.
            with factory() as s:
                row = s.get(MagicLinkNonce, minted_jti)
                if row is not None:
                    s.delete(row)
                # Audit rows from the request land in a committed
                # state too; clear them so test_table_exists etc.
                # below remain honest about what they observe.
                from app.adapters.db.audit.models import AuditLog

                for audit in s.scalars(select(AuditLog)).all():
                    s.delete(audit)
                s.commit()


class TestMigrationShape:
    """The ``magic_link_nonce`` migration landed with the expected shape."""

    def test_table_exists(
        self,
        db_session: Session,
    ) -> None:
        """A query against the model class works iff the table exists."""
        rows = db_session.scalars(select(MagicLinkNonce)).all()
        # No rows after setup; we just want to confirm the SQL executes.
        assert rows == []


class TestRequestConsumeRoundTrip:
    """End-to-end round trip against a real DB on both backends."""

    def test_round_trip(
        self,
        db_session: Session,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        mailer = _RecordingMailer()
        # cd-9i7z: ``request_link`` returns a deferred-send pending.
        pending = request_link(
            db_session,
            email="rt@example.com",
            purpose="signup_verify",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        db_session.flush()
        assert pending is not None
        pending.deliver()
        token = _extract_token(mailer.sent[0][2])

        outcome = consume_link(
            db_session,
            token=token,
            expected_purpose="signup_verify",
            ip="127.0.0.1",
            now=_PINNED + timedelta(minutes=1),
            throttle=throttle,
            settings=settings,
        )
        assert outcome.purpose == "signup_verify"

        # Replay raises.
        with pytest.raises(AlreadyConsumed):
            consume_link(
                db_session,
                token=token,
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=2),
                throttle=throttle,
                settings=settings,
            )
