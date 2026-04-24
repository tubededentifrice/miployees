"""Unit tests for :func:`app.audit.write_audit`.

The writer is a tiny pure-Python factory over a SQLAlchemy session:
it constructs an :class:`~app.adapters.db.audit.models.AuditLog` row,
calls ``session.add``, and returns. These tests exercise the
construction surface against a throwaway in-memory engine — no
migration, no tenancy filter — so they stay fast and focus on the
field-copy + clock behaviour.

Integration coverage (transaction boundaries, tenant-filter
raising on unscoped reads, index presence) lives in
``tests/integration/test_audit_writer.py``.

See ``docs/specs/02-domain-model.md`` §"audit_log" and
``docs/specs/01-architecture.md`` §"Key runtime invariants" #3.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.audit import write_audit
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock

_CTX = WorkspaceContext(
    workspace_id="01HWA00000000000000000WSPA",
    workspace_slug="workspace-a",
    actor_id="01HWA00000000000000000USRA",
    actor_kind="user",
    actor_grant_role="manager",
    actor_was_owner_member=True,
    audit_correlation_id="01HWA00000000000000000CRLA",
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``.

    We deliberately do NOT run alembic here: the writer is tested
    purely against the ORM. Integration-level transaction semantics
    are covered in the sibling integration module.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed here."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


class TestFieldCopy:
    """Every :class:`WorkspaceContext` field lands verbatim on the row."""

    def test_copies_workspace_id(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
        )
        assert row.workspace_id == _CTX.workspace_id

    def test_copies_actor_identity(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
        )
        assert row.actor_id == _CTX.actor_id
        assert row.actor_kind == _CTX.actor_kind
        assert row.actor_grant_role == _CTX.actor_grant_role
        assert row.actor_was_owner_member is _CTX.actor_was_owner_member

    def test_copies_correlation_id(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
        )
        assert row.correlation_id == _CTX.audit_correlation_id

    def test_copies_entity_and_action(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="stay",
            entity_id="01HWASTAY",
            action="completed",
        )
        assert row.entity_kind == "stay"
        assert row.entity_id == "01HWASTAY"
        assert row.action == "completed"


class TestDiffDefaulting:
    """``diff=None`` is persisted as ``{}`` so the NOT NULL contract holds."""

    def test_none_becomes_empty_dict(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="deleted",
            diff=None,
        )
        assert row.diff == {}

    def test_mapping_is_preserved(self, session: Session) -> None:
        payload = {"before": {"title": "old"}, "after": {"title": "new"}}
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="updated",
            diff=payload,
        )
        assert row.diff == payload

    def test_sequence_is_preserved(self, session: Session) -> None:
        """A sequence ``diff`` (bulk change events) lands verbatim."""
        payload = [{"id": "1", "op": "archive"}, {"id": "2", "op": "archive"}]
        row = write_audit(
            session,
            _CTX,
            entity_kind="task_batch",
            entity_id="01HWABATCH",
            action="archived",
            diff=payload,
        )
        assert row.diff == payload

    def test_omitted_defaults_to_empty_dict(self, session: Session) -> None:
        """Callers that don't pass ``diff`` at all still get ``{}`` persisted."""
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="archived",
        )
        assert row.diff == {}


class TestRedaction:
    """PII in ``diff`` must never reach persistence.

    The writer funnels ``diff`` through :func:`app.util.redact.redact`
    before calling :meth:`Session.add`, so the on-disk
    ``AuditLog.diff`` column holds only the scrubbed form. This is a
    §15 invariant (``docs/specs/15-security-privacy.md`` §"Audit
    log") — logs and audit rows share one redaction rule set.
    """

    def test_email_in_diff_is_redacted(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="contact",
            entity_id="01HWACON",
            action="updated",
            diff={
                "before": {"email": "old@x.com"},
                "after": {"email": "jean@example.com"},
            },
        )
        # ``email`` is not in the sensitive-key set (it's often a
        # legitimate audit field), but the free-text regex pass still
        # scrubs the address value.
        assert row.diff is not None
        diff = row.diff
        assert isinstance(diff, dict)
        before = diff["before"]
        after = diff["after"]
        assert isinstance(before, dict)
        assert isinstance(after, dict)
        assert before["email"] == "<redacted:email>"
        assert after["email"] == "<redacted:email>"

    def test_iban_and_pan_in_diff_are_redacted(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="payout_destination",
            entity_id="01HWAPAY",
            action="updated",
            diff={
                "note": (
                    "switch IBAN FR1420041010050500013M02606 and card "
                    "4242424242424242 to new account"
                ),
            },
        )
        assert row.diff is not None
        assert isinstance(row.diff, dict)
        note = row.diff["note"]
        assert isinstance(note, str)
        assert "FR1420041010050500013M02606" not in note
        assert "4242424242424242" not in note
        assert "<redacted:iban>" in note
        assert "<redacted:pan>" in note

    def test_sensitive_key_in_diff_is_redacted(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="user",
            entity_id="01HWAUSR",
            action="credentials_updated",
            diff={"password": "hunter2-plaintext", "session_id": "sess-abcdef"},
        )
        assert row.diff is not None
        assert isinstance(row.diff, dict)
        assert "hunter2-plaintext" not in str(row.diff)
        assert "sess-abcdef" not in str(row.diff)

    def test_list_diff_is_redacted_elementwise(self, session: Session) -> None:
        row = write_audit(
            session,
            _CTX,
            entity_kind="task_batch",
            entity_id="01HWABATCH",
            action="archived",
            diff=[
                {"id": "1", "reason": "contact jean@example.com"},
                {"id": "2", "reason": "no-op"},
            ],
        )
        assert row.diff is not None
        assert isinstance(row.diff, list)
        first = row.diff[0]
        assert isinstance(first, dict)
        assert "<redacted:email>" in first["reason"]


class TestClock:
    """The writer uses ``SystemClock`` by default and accepts overrides."""

    def test_frozen_clock_pins_created_at(self, session: Session) -> None:
        pinned = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
        clock = FrozenClock(pinned)
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
            clock=clock,
        )
        assert row.created_at == pinned

    def test_default_clock_is_utc_aware(self, session: Session) -> None:
        """Without an override, ``created_at`` is aware and UTC."""
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
        )
        assert row.created_at.tzinfo is not None
        # Normalise to compare offsets: aware UTC compares equal to UTC.
        assert row.created_at.utcoffset() == UTC.utcoffset(row.created_at)


class TestUlid:
    """Every row carries a unique monotonic ULID id."""

    def test_rapid_calls_produce_distinct_ids(self, session: Session) -> None:
        """``new_ulid`` is monotonic — two rapid calls must differ.

        Regression guard: a single-millisecond burst used to collide
        before the monotonic tail was added to ``app.util.ulid``.
        """
        ids: set[str] = set()
        for _ in range(50):
            row = write_audit(
                session,
                _CTX,
                entity_kind="task",
                entity_id="01HWATASK",
                action="created",
            )
            ids.add(row.id)
        assert len(ids) == 50


class TestSessionSurface:
    """The writer adds to the session; the caller controls transactions."""

    def test_row_is_registered_on_session(self, session: Session) -> None:
        """Before any flush the row is tracked in ``session.new``."""
        row = write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
        )
        # ``session.new`` is the set of pending inserts.
        assert row in session.new

    def test_writer_does_not_commit(self, session: Session) -> None:
        """The writer MUST NOT call ``session.commit()``.

        A rollback right after ``write_audit`` must therefore discard
        the row — any stray commit inside the writer would invert
        the Unit-of-Work contract in §01 #3.
        """
        write_audit(
            session,
            _CTX,
            entity_kind="task",
            entity_id="01HWATASK",
            action="created",
        )
        session.rollback()
        # Rollback discards the pending row; a follow-up query against
        # the fresh transaction sees an empty table.
        rows = session.scalars(select(AuditLog)).all()
        assert rows == []
