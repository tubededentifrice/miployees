"""Unit tests for :mod:`app.worker.tasks.overdue` (cd-hurw).

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_completion.py``: fresh engine per test, load
every sibling ``models`` module onto the shared metadata, run
``create_all``, drive the service with :class:`FrozenClock` and a
private :class:`EventBus` so subscriptions don't leak between tests.

Covers:

* :class:`OverdueReport` shape: every documented field is populated;
  empty ticks return zeros + empty collections.
* Happy path: pending task with ``ends_at + grace < now`` flips to
  ``state='overdue'``, stamps ``overdue_since=now``, fires
  :class:`TaskOverdue` with ``slipped_minutes`` math.
* Grace window: a task whose slip is below grace is left untouched.
* Idempotency: a second tick over the same data set fires no new
  events and writes no new ``occurrence.state`` flips.
* Manual-transition safety: a manual ``complete`` between SELECT and
  UPDATE is preserved (the ``state IN (...)`` WHERE-clause guard).
* Per-property breakdown: rows from two properties produce
  per-property counts; personal / workspace-scoped rows bucket
  under the empty string key.
* Workspace-scoped: a tick on workspace A does not touch workspace B.
* :func:`detect_overdue` rejects naive ``now`` and non-positive
  ``grace_minutes``.
* :func:`resolve_overdue_grace_minutes` returns the workspace
  setting when present + a positive int; falls back to the default
  for missing / non-int / non-positive values.
* Completion-side cleanup (sister context): :func:`revert_overdue`
  clears ``overdue_since``; :func:`complete` / :func:`skip` /
  :func:`cancel` / :func:`start` clear it on transition.
* :class:`TaskPayload` projection: when ``overdue_since`` is set on
  the view, ``overdue=True`` regardless of the time anchor.

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine"
("overdue is soft, never terminal; manual transitions clear
``overdue_since``").
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.api.v1.tasks import TaskPayload, _compute_overdue
from app.domain.tasks.completion import (
    cancel,
    complete,
    revert_overdue,
    skip,
    start,
)
from app.domain.tasks.oneoff import TaskView
from app.events.bus import EventBus
from app.events.types import TaskOverdue
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.overdue import (
    DEFAULT_OVERDUE_GRACE_MINUTES,
    DEFAULT_OVERDUE_TICK_SECONDS,
    SETTINGS_KEY_OVERDUE_GRACE_MINUTES,
    SETTINGS_KEY_OVERDUE_TICK_SECONDS,
    OverdueReport,
    detect_overdue,
    resolve_overdue_grace_minutes,
    resolve_overdue_tick_seconds,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _strip_tz(value: datetime) -> datetime:
    """Drop ``tzinfo`` for cross-backend equality assertions.

    SQLite strips tzinfo off ``DateTime(timezone=True)`` columns on
    round-trip; PostgreSQL preserves it. Compare wall-clocks so the
    same assertion works on both backends.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fixtures + bootstrap
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
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
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _ctx(
    workspace_id: str,
    *,
    slug: str = "ws",
    role: str = "manager",
    owner: bool = True,
    actor_id: str | None = None,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id if actor_id is not None else new_ulid(),
        actor_kind="user",
        actor_grant_role=role,  # type: ignore[arg-type]
        actor_was_owner_member=owner,
        audit_correlation_id=new_ulid(),
    )


def _bootstrap_workspace(
    session: Session, *, slug: str = "ws", settings_json: dict[str, Any] | None = None
) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json=settings_json if settings_json is not None else {},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    session.add(
        Property(
            id=pid,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return pid


def _bootstrap_user(session: Session) -> str:
    """Insert a minimal user row with a fresh id."""
    uid = new_ulid()
    from app.adapters.db.identity.models import User

    session.add(
        User(
            id=uid,
            email=f"{uid}@example.com",
            email_lower=f"{uid}@example.com".lower(),
            display_name=uid,
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    session.flush()
    return uid


def _bootstrap_occurrence(
    session: Session,
    *,
    workspace_id: str,
    property_id: str | None,
    state: str = "pending",
    ends_at: datetime | None = None,
    starts_at: datetime | None = None,
    assignee_user_id: str | None = None,
) -> str:
    """Insert one ``occurrence`` row with deterministic defaults.

    Defaults to ``starts_at = _PINNED - 2h`` and ``ends_at = _PINNED -
    1h`` so the row is **already past** the pinned ``now`` clock — the
    sweeper cases override ``ends_at`` per-test to dial in slip vs.
    grace boundaries.
    """
    ends = ends_at if ends_at is not None else _PINNED - timedelta(hours=1)
    starts = starts_at if starts_at is not None else ends - timedelta(hours=1)
    oid = new_ulid()
    session.add(
        Occurrence(
            id=oid,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=assignee_user_id,
            starts_at=starts,
            ends_at=ends,
            scheduled_for_local="2026-04-19T10:00",
            originally_scheduled_for="2026-04-19T10:00",
            state=state,
            cancellation_reason=None,
            title="Pool clean",
            description_md="",
            priority="normal",
            photo_evidence="disabled",
            duration_minutes=60,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return oid


def _record(bus: EventBus) -> list[TaskOverdue]:
    captured: list[TaskOverdue] = []
    bus.subscribe(TaskOverdue)(captured.append)
    return captured


# ---------------------------------------------------------------------------
# OverdueReport shape
# ---------------------------------------------------------------------------


class TestOverdueReportShape:
    """Every documented field is populated; empty ticks return zeros."""

    def test_overdue_report_shape(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        # No tasks at all — sweeper finds nothing.
        report = detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        assert isinstance(report, OverdueReport)
        assert report.flipped_count == 0
        assert report.skipped_already_overdue == 0
        assert report.skipped_manual_transition == 0
        assert report.per_property_breakdown == {}
        assert report.flipped_task_ids == ()
        assert report.tick_started_at == _PINNED
        assert report.tick_ended_at == _PINNED


# ---------------------------------------------------------------------------
# Happy path: flip + event payload
# ---------------------------------------------------------------------------


class TestFlipsPendingPastEndsAtPlusGrace:
    """A pending task with ``ends_at + grace < now`` is flipped + emitted."""

    def test_flips_pending_with_past_ends_at_plus_grace(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        # ``ends_at`` is 30 minutes ago; grace is 15 minutes; cutoff is
        # 15 minutes ago; ``ends_at < cutoff`` ⇒ overdue.
        ends = _PINNED - timedelta(minutes=30)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
            assignee_user_id=worker,
        )
        captured = _record(bus)

        report = detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        assert report.flipped_count == 1
        assert report.flipped_task_ids == (oid,)
        assert report.per_property_breakdown == {prop: 1}

        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "overdue"
        # SQLite drops tzinfo on round-trip; compare the wall-clock.
        assert row.overdue_since is not None
        assert _strip_tz(row.overdue_since) == _strip_tz(_PINNED)

        assert len(captured) == 1
        event = captured[0]
        assert event.task_id == oid
        assert event.workspace_id == ws
        assert event.assigned_user_id == worker
        assert event.overdue_since == _PINNED
        # 30 minutes between ``ends_at`` and ``now`` ⇒ 30 floored minutes.
        assert event.slipped_minutes == 30


class TestDoesNotFlipInsideGraceWindow:
    """A task whose slip is below the grace minutes is left untouched."""

    def test_does_not_flip_inside_grace_window(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # ``ends_at`` is 5 minutes ago; grace is 15 minutes; cutoff is
        # 15 minutes ago. ``ends_at < cutoff`` is false → no flip.
        ends = _PINNED - timedelta(minutes=5)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )
        captured = _record(bus)

        report = detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        assert report.flipped_count == 0
        assert captured == []
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "pending"
        assert row.overdue_since is None

    def test_does_not_flip_at_exact_grace_boundary(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """``ends_at == cutoff`` is on the boundary — strict-less rejects."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # ``ends_at`` exactly 15 minutes ago; cutoff is 15 minutes ago.
        # Predicate is ``ends_at < cutoff`` — equality does not flip.
        ends = _PINNED - timedelta(minutes=15)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )

        report = detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )
        assert report.flipped_count == 0
        row = session.get(Occurrence, oid)
        assert row is not None and row.state == "pending"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """A second tick over the same data set fires no new events."""

    def test_idempotent_re_run_emits_no_second_event(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        ends = _PINNED - timedelta(minutes=30)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )
        captured = _record(bus)
        ctx = _ctx(ws)

        first = detect_overdue(
            ctx, session=session, clock=clock, event_bus=bus, grace_minutes=15
        )
        assert first.flipped_count == 1
        assert len(captured) == 1

        # Row is now in ``state='overdue'`` — the second tick's load
        # query excludes it, and no new event fires.
        second = detect_overdue(
            ctx, session=session, clock=clock, event_bus=bus, grace_minutes=15
        )
        assert second.flipped_count == 0
        assert len(captured) == 1  # no new event
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "overdue"


# ---------------------------------------------------------------------------
# Manual-transition safety
# ---------------------------------------------------------------------------


class TestManualTransitionPreserved:
    """A manual transition between SELECT and UPDATE is preserved."""

    def test_manual_transition_between_ticks_is_preserved(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """Between two ticks the user completes the task; the next
        tick must not re-overdue it.

        Drives the simpler of the two manual-transition layers: by
        the time the next sweeper tick fires, the row is already in
        a terminal state, so the load-query filter
        ``state IN ('scheduled', 'pending', 'in_progress')`` excludes
        it from the candidate list entirely. No flip, no event, the
        deliberate move stands. The sibling
        :meth:`test_where_clause_guard_skips_in_flight_manual_transition`
        covers the in-flight SELECT-then-UPDATE race the WHERE-clause
        guard catches.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        ends = _PINNED - timedelta(minutes=30)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )
        captured = _record(bus)

        # Land the "between ticks" manual completion before invoking
        # the next sweeper tick.
        row = session.get(Occurrence, oid)
        assert row is not None
        row.state = "done"
        session.flush()

        report = detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        assert report.flipped_count == 0
        assert captured == []
        post = session.get(Occurrence, oid)
        assert post is not None and post.state == "done"
        assert post.overdue_since is None

    def test_where_clause_guard_skips_in_flight_manual_transition(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """Even when SELECT picked up the row, the per-row UPDATE
        guard skips it if the state has since transitioned out of
        ``flippable_states``.

        Drives the actual SELECT-then-UPDATE race by monkey-patching
        the session's ``execute`` so that the very first per-row
        UPDATE statement is preceded by a same-session ``UPDATE
        occurrence SET state='done'`` against the same row id. The
        sweeper's UPDATE then re-asserts ``state IN
        ('scheduled', 'pending', 'in_progress')`` and matches zero
        rows; the row stays ``done`` and the report counts the skip
        under ``skipped_manual_transition``.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        ends = _PINNED - timedelta(minutes=30)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )

        original_execute = session.execute
        flipped: dict[str, bool] = {"once": False}

        def _race(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            # Detect the worker's per-row UPDATE by inspecting the
            # compiled SQL. The first time we see the sweeper's
            # ``UPDATE occurrence`` statement we land a manual
            # transition into ``done`` *before* the worker's UPDATE
            # runs — emulating the post-SELECT pre-UPDATE race a
            # concurrent transaction would create.
            sql_text = str(stmt)
            if (
                not flipped["once"]
                and "UPDATE occurrence" in sql_text
                and "overdue_since" in sql_text
            ):
                flipped["once"] = True
                row = session.get(Occurrence, oid)
                assert row is not None
                row.state = "done"
                session.flush()
            return original_execute(stmt, *args, **kwargs)

        session.execute = _race  # type: ignore[method-assign]
        try:
            report = detect_overdue(
                _ctx(ws),
                session=session,
                clock=clock,
                event_bus=bus,
                grace_minutes=15,
            )
        finally:
            del session.execute  # type: ignore[method-assign]

        # The sweeper's UPDATE matched zero rows — the manual
        # transition wins. The report records the skip and no event
        # fires for the displaced candidate.
        assert report.flipped_count == 0
        assert report.skipped_manual_transition == 1
        post = session.get(Occurrence, oid)
        assert post is not None
        assert post.state == "done"
        assert post.overdue_since is None


# ---------------------------------------------------------------------------
# Event payload — slipped_minutes math
# ---------------------------------------------------------------------------


class TestEventCarriesSlippedMinutes:
    """``slipped_minutes`` is floored ``(now - ends_at) / 60``."""

    def test_event_carries_slipped_minutes(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # 47 minutes 12 seconds in the past — should floor to 47.
        ends = _PINNED - timedelta(minutes=47, seconds=12)
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )
        captured = _record(bus)

        detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        assert len(captured) == 1
        assert captured[0].slipped_minutes == 47

    def test_event_emits_zero_slipped_minutes_under_minute_grace(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        """Sub-minute slip past a sub-minute grace floors to zero.

        The :class:`TaskOverdue` validator only rejects negative
        slipped_minutes; zero is a legal value when the grace + slip
        sum to less than 60 seconds (rare but possible under custom
        grace settings).
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # ``ends_at`` 45 seconds ago; grace=0 (minimum legal under the
        # resolver's positive-int contract is 1, so use 1). Slip is
        # less than 60 seconds → floored to 0. We use ``grace=1`` so
        # the cutoff is ``now - 1 min`` and ``ends_at < cutoff`` is
        # false; instead use a grace of exactly enough to put us just
        # over: pin ``ends_at = now - 70s`` and grace=1 (cutoff =
        # now - 60s, ends_at < cutoff is true).
        ends = _PINNED - timedelta(seconds=70)
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )
        captured = _record(bus)
        detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=1
        )
        assert len(captured) == 1
        # 70 seconds → floored 1 minute.
        assert captured[0].slipped_minutes == 1


# ---------------------------------------------------------------------------
# Per-property breakdown
# ---------------------------------------------------------------------------


class TestPerPropertyBreakdown:
    """Per-property counts; null property_id buckets under ``""``."""

    def test_per_property_breakdown_sums(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop_a = _bootstrap_property(session)
        prop_b = _bootstrap_property(session)
        ends = _PINNED - timedelta(minutes=30)
        # Two on prop_a, one on prop_b, one personal (null property_id).
        for _ in range(2):
            _bootstrap_occurrence(
                session,
                workspace_id=ws,
                property_id=prop_a,
                state="pending",
                ends_at=ends,
            )
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop_b,
            state="pending",
            ends_at=ends,
        )
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=None,
            state="pending",
            ends_at=ends,
        )

        report = detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        assert report.flipped_count == 4
        assert report.per_property_breakdown == {
            prop_a: 2,
            prop_b: 1,
            "": 1,
        }


# ---------------------------------------------------------------------------
# Workspace tenancy isolation
# ---------------------------------------------------------------------------


class TestWorkspaceIsolation:
    """A tick on workspace A does not touch workspace B."""

    def test_does_not_flip_other_workspaces(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="a")
        ws_b = _bootstrap_workspace(session, slug="b")
        prop = _bootstrap_property(session)
        ends = _PINNED - timedelta(minutes=30)
        oid_a = _bootstrap_occurrence(
            session,
            workspace_id=ws_a,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )
        oid_b = _bootstrap_occurrence(
            session,
            workspace_id=ws_b,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )

        report = detect_overdue(
            _ctx(ws_a, slug="a"),
            session=session,
            clock=clock,
            event_bus=bus,
            grace_minutes=15,
        )

        assert report.flipped_count == 1
        row_a = session.get(Occurrence, oid_a)
        row_b = session.get(Occurrence, oid_b)
        assert row_a is not None and row_a.state == "overdue"
        assert row_b is not None and row_b.state == "pending"


# ---------------------------------------------------------------------------
# Audit row
# ---------------------------------------------------------------------------


class TestAuditRow:
    """One ``tasks.overdue_tick`` audit row per tick."""

    def test_audit_row_carries_summary(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        ends = _PINNED - timedelta(minutes=30)
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state="pending",
            ends_at=ends,
        )

        detect_overdue(
            _ctx(ws), session=session, clock=clock, event_bus=bus, grace_minutes=15
        )

        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "tasks.overdue_tick")
        ).all()
        assert len(audits) == 1
        diff = audits[0].diff
        assert diff["flipped_count"] == 1
        assert diff["per_property_breakdown"] == {prop: 1}
        assert diff["grace_minutes"] == 15
        assert diff["skipped_manual_transition"] == 0
        assert "tick_started_at" in diff
        assert "tick_ended_at" in diff


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    """Naive ``now`` and non-positive ``grace_minutes`` raise."""

    def test_naive_now_rejected(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        with pytest.raises(ValueError, match="timezone-aware"):
            detect_overdue(
                _ctx(ws),
                session=session,
                now=datetime(2026, 4, 19, 12, 0, 0),
                clock=clock,
                event_bus=bus,
                grace_minutes=15,
            )

    def test_non_positive_grace_rejected(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        with pytest.raises(ValueError, match="grace_minutes must be a positive"):
            detect_overdue(
                _ctx(ws),
                session=session,
                clock=clock,
                event_bus=bus,
                grace_minutes=0,
            )
        with pytest.raises(ValueError, match="grace_minutes must be a positive"):
            detect_overdue(
                _ctx(ws),
                session=session,
                clock=clock,
                event_bus=bus,
                grace_minutes=-5,
            )


# ---------------------------------------------------------------------------
# Workspace-setting resolvers
# ---------------------------------------------------------------------------


class TestSettingsResolvers:
    """``settings_json`` reads + defaults."""

    def test_resolve_grace_falls_back_to_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, settings_json={})
        assert resolve_overdue_grace_minutes(session, workspace_id=ws) == (
            DEFAULT_OVERDUE_GRACE_MINUTES
        )

    def test_resolve_grace_uses_workspace_setting(self, session: Session) -> None:
        ws = _bootstrap_workspace(
            session,
            settings_json={SETTINGS_KEY_OVERDUE_GRACE_MINUTES: 45},
        )
        assert resolve_overdue_grace_minutes(session, workspace_id=ws) == 45

    def test_resolve_grace_rejects_non_int(self, session: Session) -> None:
        ws = _bootstrap_workspace(
            session,
            settings_json={SETTINGS_KEY_OVERDUE_GRACE_MINUTES: "thirty"},
        )
        assert resolve_overdue_grace_minutes(session, workspace_id=ws) == (
            DEFAULT_OVERDUE_GRACE_MINUTES
        )

    def test_resolve_grace_rejects_zero(self, session: Session) -> None:
        ws = _bootstrap_workspace(
            session,
            settings_json={SETTINGS_KEY_OVERDUE_GRACE_MINUTES: 0},
        )
        assert resolve_overdue_grace_minutes(session, workspace_id=ws) == (
            DEFAULT_OVERDUE_GRACE_MINUTES
        )

    def test_resolve_grace_rejects_bool(self, session: Session) -> None:
        # ``isinstance(True, int)`` is True in Python; the resolver
        # must reject bool explicitly so a stray ``"key": true`` does
        # not collapse to ``1``.
        ws = _bootstrap_workspace(
            session,
            settings_json={SETTINGS_KEY_OVERDUE_GRACE_MINUTES: True},
        )
        assert resolve_overdue_grace_minutes(session, workspace_id=ws) == (
            DEFAULT_OVERDUE_GRACE_MINUTES
        )

    def test_resolve_tick_seconds_uses_setting(self, session: Session) -> None:
        ws = _bootstrap_workspace(
            session,
            settings_json={SETTINGS_KEY_OVERDUE_TICK_SECONDS: 600},
        )
        assert resolve_overdue_tick_seconds(session, workspace_id=ws) == 600

    def test_resolve_tick_seconds_falls_back_to_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session)
        assert resolve_overdue_tick_seconds(session, workspace_id=ws) == (
            DEFAULT_OVERDUE_TICK_SECONDS
        )


# ---------------------------------------------------------------------------
# Completion-side cleanup (manual-transition clears overdue_since)
# ---------------------------------------------------------------------------


class TestCompletionClearsOverdueSince:
    """Manual transitions clear the soft-overdue marker."""

    def _seed(
        self,
        session: Session,
        *,
        state: str,
        assignee: str | None,
        overdue_since: datetime | None = None,
    ) -> tuple[str, str]:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        oid = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            state=state,
            ends_at=_PINNED - timedelta(minutes=30),
            assignee_user_id=assignee,
        )
        if overdue_since is not None:
            row = session.get(Occurrence, oid)
            assert row is not None
            row.overdue_since = overdue_since
            session.flush()
        return ws, oid

    def test_revert_overdue_clears_overdue_since(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        worker = _bootstrap_user(session)
        ws, oid = self._seed(
            session,
            state="overdue",
            assignee=worker,
            overdue_since=_PINNED - timedelta(minutes=15),
        )
        ctx = _ctx(ws, role="manager", owner=True)

        result = revert_overdue(session, ctx, oid, target_state="pending", clock=clock)
        assert result.state == "pending"
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "pending"
        assert row.overdue_since is None

    def test_completion_clears_overdue_since(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        worker = _bootstrap_user(session)
        ws, oid = self._seed(
            session,
            state="overdue",
            assignee=worker,
            overdue_since=_PINNED - timedelta(minutes=15),
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=worker)

        result = complete(session, ctx, oid, clock=clock, event_bus=bus)
        assert result.state == "done"
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "done"
        assert row.overdue_since is None

    def test_skip_clears_overdue_since(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        worker = _bootstrap_user(session)
        ws, oid = self._seed(
            session,
            state="overdue",
            assignee=worker,
            overdue_since=_PINNED - timedelta(minutes=15),
        )
        ctx = _ctx(ws, role="manager", owner=True)

        result = skip(
            session, ctx, oid, reason="not_needed", clock=clock, event_bus=bus
        )
        assert result.state == "skipped"
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "skipped"
        assert row.overdue_since is None

    def test_cancel_clears_overdue_since(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws, oid = self._seed(
            session,
            state="overdue",
            assignee=None,
            overdue_since=_PINNED - timedelta(minutes=15),
        )
        ctx = _ctx(ws, role="manager", owner=True)

        result = cancel(
            session, ctx, oid, reason="property_closed", clock=clock, event_bus=bus
        )
        assert result.state == "cancelled"
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "cancelled"
        assert row.overdue_since is None

    def test_start_clears_overdue_since(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        worker = _bootstrap_user(session)
        ws, oid = self._seed(
            session,
            state="overdue",
            assignee=worker,
            overdue_since=_PINNED - timedelta(minutes=15),
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=worker)

        result = start(session, ctx, oid, clock=clock, event_bus=bus)
        assert result.state == "in_progress"
        row = session.get(Occurrence, oid)
        assert row is not None
        assert row.state == "in_progress"
        assert row.overdue_since is None


# ---------------------------------------------------------------------------
# TaskPayload uses the column when present
# ---------------------------------------------------------------------------


class TestTaskPayloadUsesColumnWhenPresent:
    """``TaskPayload.overdue`` is True when ``overdue_since`` is set."""

    def _make_view(
        self,
        *,
        state: str = "overdue",
        scheduled_for_utc: datetime | None = None,
        overdue_since: datetime | None = None,
    ) -> TaskView:
        scheduled = scheduled_for_utc or (_PINNED - timedelta(hours=1))
        return TaskView(
            id="01HW00000000000000000000T1",
            workspace_id="01HW00000000000000000000W1",
            template_id=None,
            schedule_id=None,
            property_id=None,
            area_id=None,
            unit_id=None,
            title="Pool clean",
            description_md=None,
            priority="normal",
            state=state,  # type: ignore[arg-type]
            scheduled_for_local="2026-04-19T10:00",
            scheduled_for_utc=scheduled,
            duration_minutes=60,
            photo_evidence="disabled",
            linked_instruction_ids=(),
            inventory_consumption_json={},
            expected_role_id=None,
            assigned_user_id=None,
            created_by="01HW00000000000000000000U1",
            is_personal=False,
            created_at=_PINNED,
            overdue_since=overdue_since,
        )

    def test_taskpayload_uses_column_when_present(self) -> None:
        # Column says overdue; the time anchor is in the future, so a
        # purely time-derived computation would say "not overdue".
        view = self._make_view(
            state="pending",
            scheduled_for_utc=_PINNED + timedelta(hours=2),
            overdue_since=_PINNED - timedelta(minutes=10),
        )
        # Still True because the column wins.
        assert _compute_overdue(view, _PINNED) is True
        payload = TaskPayload.from_view(view, now_utc=_PINNED)
        assert payload.overdue is True

    def test_taskpayload_falls_back_to_time_when_column_null(self) -> None:
        # Column null, state ``pending``, anchor in the past → time-
        # derived overdue True.
        view = self._make_view(
            state="pending",
            scheduled_for_utc=_PINNED - timedelta(hours=1),
            overdue_since=None,
        )
        assert _compute_overdue(view, _PINNED) is True

    def test_taskpayload_terminal_states_never_overdue(self) -> None:
        # Terminal state masks the column entirely — a ``done`` row
        # should never render as overdue even if the column was set
        # by the sweeper before the manual ``complete`` cleared it
        # (defensive — the completion path clears the column).
        view = self._make_view(
            state="done",
            scheduled_for_utc=_PINNED - timedelta(hours=1),
            overdue_since=_PINNED - timedelta(minutes=10),
        )
        assert _compute_overdue(view, _PINNED) is False

    def test_taskpayload_state_overdue_is_overdue(self) -> None:
        # ``state='overdue'`` + null column (shouldn't happen in
        # practice but is the legacy / defensive path) still computes
        # to True via the state branch.
        view = self._make_view(
            state="overdue",
            scheduled_for_utc=_PINNED + timedelta(hours=2),
            overdue_since=None,
        )
        assert _compute_overdue(view, _PINNED) is True
