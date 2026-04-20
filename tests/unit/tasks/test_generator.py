"""Unit tests for :mod:`app.worker.tasks.generator`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_schedules.py``: spin up a fresh engine per
test, pull every sibling ``models`` module onto the shared
``Base.metadata``, run ``Base.metadata.create_all``, drive the
domain code with a :class:`FrozenClock` + a fresh
:class:`EventBus`.

Covers the cd-22e acceptance criteria:

* Two consecutive runs over the same window create rows once
  (idempotency, via both the SELECT pre-flight and the partial
  unique index).
* Paused / past-active-until / deleted schedules are skipped.
* Property closures suppress occurrences and emit the audit
  event; ``GenerationReport.skipped_for_closure`` counts match.
* ``task.created`` is published per new row on the event bus.
* An RRULE that crosses a DST boundary in the property tz keeps
  the local wall-clock stable (09:00 stays 09:00 across the
  Europe/Paris spring-forward).
* ``WorkspaceContext`` threading: rows from workspace A are not
  visible from workspace B.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Generation" and
``docs/specs/02-domain-model.md`` §"occurrence".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyClosure
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.events.types import TaskCreated
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.generator import (
    DEFAULT_HORIZON_DAYS,
    GenerationReport,
    generate_task_occurrences,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve.

    Matches the helper in ``tests/unit/tasks/test_schedules.py``.
    """
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
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
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


@pytest.fixture
def bus() -> EventBus:
    """Fresh in-process bus per test so subscriptions don't leak."""
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str, *, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id="01HWA00000000000000000USR1",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_property(session: Session, *, timezone: str = "Europe/Paris") -> str:
    prop_id = new_ulid()
    session.add(
        Property(
            id=prop_id,
            address="1 Villa Sud Way",
            timezone=timezone,
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return prop_id


def _bootstrap_template(
    session: Session,
    *,
    workspace_id: str,
    duration_minutes: int = 60,
) -> str:
    tpl_id = new_ulid()
    session.add(
        TaskTemplate(
            id=tpl_id,
            workspace_id=workspace_id,
            title="Villa Sud pool",
            name="Villa Sud pool",
            description_md="",
            default_duration_min=duration_minutes,
            duration_minutes=duration_minutes,
            required_evidence="none",
            photo_required=False,
            default_assignee_role=None,
            role_id="role-housekeeper",
            property_scope="any",
            listed_property_ids=[],
            area_scope="any",
            listed_area_ids=[],
            checklist_template_json=[],
            photo_evidence="disabled",
            linked_instruction_ids=[],
            priority="normal",
            inventory_consumption_json={},
            llm_hints_md=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return tpl_id


def _bootstrap_schedule(
    session: Session,
    *,
    workspace_id: str,
    template_id: str,
    property_id: str,
    rrule: str = "FREQ=WEEKLY;BYDAY=SA",
    dtstart_local: str = "2026-04-18T09:00",
    duration_minutes: int | None = 60,
    active_from: str | None = "2026-04-01",
    active_until: str | None = None,
    paused_at: datetime | None = None,
    deleted_at: datetime | None = None,
    rdate_local: str = "",
    exdate_local: str = "",
) -> str:
    schedule_id = new_ulid()
    anchor = datetime.fromisoformat(dtstart_local)
    session.add(
        Schedule(
            id=schedule_id,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            name="Villa Sud pool schedule",
            area_id=None,
            rrule_text=rrule,
            dtstart=anchor,
            dtstart_local=dtstart_local,
            until=None,
            duration_minutes=duration_minutes,
            rdate_local=rdate_local,
            exdate_local=exdate_local,
            active_from=active_from,
            active_until=active_until,
            paused_at=paused_at,
            deleted_at=deleted_at,
            assignee_user_id=None,
            backup_assignee_user_ids=[],
            assignee_role=None,
            enabled=True,
            next_generation_at=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return schedule_id


def _bootstrap_closure(
    session: Session,
    *,
    property_id: str,
    starts_at: datetime,
    ends_at: datetime,
    reason: str = "renovation",
) -> str:
    closure_id = new_ulid()
    session.add(
        PropertyClosure(
            id=closure_id,
            property_id=property_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
            created_by_user_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return closure_id


# ---------------------------------------------------------------------------
# Happy path + idempotency
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Base cases the rest of the suite builds on."""

    def test_creates_occurrences_for_weekly_schedule(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
        )

        # Pin ``now`` to a Monday in April 2026 so the 30-day
        # horizon covers four Saturdays (18, 25, 02, 09, 16, 23
        # — starting from the anchor on 2026-04-18).
        now = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)
        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=now,
            clock=clock,
            event_bus=bus,
        )

        assert isinstance(report, GenerationReport)
        assert report.schedules_walked == 1
        assert report.tasks_created >= 4
        assert report.skipped_duplicate == 0
        assert report.skipped_for_closure == 0

        rows = session.scalars(select(Occurrence)).all()
        assert len(rows) == report.tasks_created
        for row in rows:
            assert row.state == "scheduled"
            assert row.scheduled_for_local is not None
            # The local anchor is 09:00; every occurrence should keep
            # that wall-clock.
            assert row.scheduled_for_local.endswith("T09:00:00")
            assert row.originally_scheduled_for == row.scheduled_for_local
            assert row.workspace_id == workspace_id
            # UTC mirror should be 07:00Z (CEST, UTC+2) since every
            # candidate in this window is after the March change-over.
            # SQLite strips tzinfo off ``DateTime(timezone=True)``
            # columns on round-trip, so we assert the hour instead.
            assert row.starts_at.hour == 7
            assert row.ends_at - row.starts_at == timedelta(minutes=60)

    def test_task_created_event_published_per_row(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
        )

        captured: list[TaskCreated] = []

        @bus.subscribe(TaskCreated)
        def _on_created(event: TaskCreated) -> None:
            captured.append(event)

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )

        assert len(captured) == report.tasks_created
        ids = {event.task_id for event in captured}
        assert ids == set(report.new_task_ids)
        for event in captured:
            assert event.workspace_id == workspace_id
            assert event.occurred_at.utcoffset() == timedelta(0)

    def test_idempotent_across_runs(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
        )

        now = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)
        ctx = _ctx(workspace_id)

        first = generate_task_occurrences(
            ctx, session=session, now=now, clock=clock, event_bus=bus
        )
        assert first.tasks_created > 0
        assert first.skipped_duplicate == 0

        second = generate_task_occurrences(
            ctx, session=session, now=now, clock=clock, event_bus=bus
        )
        assert second.tasks_created == 0
        assert second.skipped_duplicate == first.tasks_created

        # Total rows equal the first run; nothing was double-inserted.
        total = session.scalars(select(Occurrence)).all()
        assert len(total) == first.tasks_created


# ---------------------------------------------------------------------------
# Skip cases: paused, deleted, active range
# ---------------------------------------------------------------------------


class TestSkipCases:
    """Paused, deleted, and out-of-range schedules must be silently skipped."""

    def test_paused_schedule_is_skipped(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            paused_at=datetime(2026, 4, 1, tzinfo=UTC),
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )
        assert report.schedules_walked == 0
        assert report.tasks_created == 0
        assert session.scalars(select(Occurrence)).all() == []

    def test_deleted_schedule_is_skipped(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            deleted_at=datetime(2026, 4, 10, tzinfo=UTC),
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )
        assert report.schedules_walked == 0
        assert report.tasks_created == 0

    def test_past_active_until_is_skipped(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            # active_until in the past relative to the tick clock.
            active_from="2026-01-01",
            active_until="2026-02-01",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )
        assert report.schedules_walked == 0
        assert report.tasks_created == 0

    def test_future_active_from_is_skipped(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            active_from="2027-01-01",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )
        assert report.schedules_walked == 0
        assert report.tasks_created == 0


# ---------------------------------------------------------------------------
# Closures
# ---------------------------------------------------------------------------


class TestPropertyClosures:
    """Days covered by a closure must not materialise occurrences."""

    def test_closure_suppresses_occurrences_and_emits_audit(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
        )
        # Closure covering the first two Saturdays of the horizon.
        # Saturday 2026-04-18 09:00 Europe/Paris is 07:00Z (CEST).
        _bootstrap_closure(
            session,
            property_id=property_id,
            starts_at=datetime(2026, 4, 17, 0, 0, tzinfo=UTC),
            ends_at=datetime(2026, 4, 26, 0, 0, tzinfo=UTC),
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 15, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )

        assert report.skipped_for_closure == 2  # 2026-04-18 + 2026-04-25
        assert report.tasks_created >= 1  # 2026-05-02 onward survive

        audit_rows = session.scalars(
            select(AuditLog).where(AuditLog.action == "schedules.skipped_for_closure")
        ).all()
        assert len(audit_rows) == 2
        for row in audit_rows:
            assert row.workspace_id == workspace_id
            assert row.entity_kind == "schedule"
            diff = row.diff
            assert isinstance(diff, dict)
            assert diff["property_id"] == property_id
            assert "scheduled_for_local" in diff


# ---------------------------------------------------------------------------
# DST crossing
# ---------------------------------------------------------------------------


class TestDSTBoundary:
    """§06: wall-clock is preserved across DST changes in the property tz."""

    def test_daily_rrule_keeps_local_09_across_spring_forward(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        # Europe/Paris spring-forward in 2026 is 2026-03-29 02:00 → 03:00.
        # A daily 09:00 schedule anchored before the change-over and
        # running a week past it must yield 09:00 local every day.
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session, timezone="Europe/Paris")
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            rrule="FREQ=DAILY",
            dtstart_local="2026-03-27T09:00",
            active_from="2026-03-27",
        )

        # Horizon: 10 days starting one day before DST flip.
        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
            horizon_days=7,
            clock=clock,
            event_bus=bus,
        )
        assert report.tasks_created == 8  # 2026-03-28 … 2026-04-04 inclusive

        rows = session.scalars(
            select(Occurrence).order_by(Occurrence.scheduled_for_local.asc())
        ).all()
        # Every local timestamp ends in ``T09:00:00`` regardless of
        # the DST flip on 2026-03-29.
        for row in rows:
            assert row.scheduled_for_local is not None
            assert row.scheduled_for_local.endswith("T09:00:00")

        # The UTC mirrors should show the DST shift: before flip the
        # 09:00 local = 08:00Z (CET), after flip it = 07:00Z (CEST).
        # Find one row either side of the boundary.
        before = next(r for r in rows if r.scheduled_for_local == "2026-03-28T09:00:00")
        after = next(r for r in rows if r.scheduled_for_local == "2026-03-30T09:00:00")
        # SQLite strips tzinfo on ``DateTime(timezone=True)`` columns;
        # the UTC bytes we wrote (``starts_at.hour``) survive, so the
        # DST shift is still observable as an hour delta.
        assert before.starts_at.hour == 8  # 09:00 Paris CET = 08:00 UTC
        assert after.starts_at.hour == 7  # 09:00 Paris CEST = 07:00 UTC

    def test_daily_rrule_keeps_local_09_across_fall_back(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        # Europe/Paris fall-back in 2026 is 2026-10-25 03:00 → 02:00
        # (the 02:00-03:00 wall-clock happens twice). A daily 09:00
        # schedule crossing the change-over must still yield 09:00
        # local every day; the ``.replace(tzinfo=zone)`` branch picks
        # the first occurrence of the repeated clock deterministically.
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session, timezone="Europe/Paris")
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            rrule="FREQ=DAILY",
            dtstart_local="2026-10-23T09:00",
            active_from="2026-10-23",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 10, 24, 0, 0, tzinfo=UTC),
            horizon_days=5,
            clock=clock,
            event_bus=bus,
        )
        assert report.tasks_created >= 5

        rows = session.scalars(
            select(Occurrence).order_by(Occurrence.scheduled_for_local.asc())
        ).all()
        for row in rows:
            assert row.scheduled_for_local is not None
            assert row.scheduled_for_local.endswith("T09:00:00")

        # 09:00 Paris on 10-25 is still CEST (change-over is at 03:00,
        # so 09:00 has already flipped to CET). 10-24 is CEST, 10-26
        # is CET. Pick one either side of the boundary and verify UTC
        # offsets — 07:00Z vs 08:00Z.
        before = next(r for r in rows if r.scheduled_for_local == "2026-10-24T09:00:00")
        after = next(r for r in rows if r.scheduled_for_local == "2026-10-26T09:00:00")
        assert before.starts_at.hour == 7  # 09:00 Paris CEST = 07:00 UTC
        assert after.starts_at.hour == 8  # 09:00 Paris CET = 08:00 UTC

    def test_byhour_rrule_crossing_spring_forward_preserves_local(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        # Explicit ``BYHOUR`` — a variant RRULE shape — must also keep
        # the requested wall-clock (09:00 local) stable across the
        # Europe/Paris spring-forward on 2026-03-29.
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session, timezone="Europe/Paris")
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            dtstart_local="2026-03-27T09:00",
            active_from="2026-03-27",
        )

        generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
            horizon_days=5,
            clock=clock,
            event_bus=bus,
        )

        rows = session.scalars(
            select(Occurrence).order_by(Occurrence.scheduled_for_local.asc())
        ).all()
        for row in rows:
            assert row.scheduled_for_local is not None
            assert row.scheduled_for_local.endswith("T09:00:00")


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    """Running against workspace A must not leak into workspace B."""

    def test_schedules_in_other_workspace_are_skipped(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_a = _bootstrap_workspace(session, slug="wsa")
        workspace_b = _bootstrap_workspace(session, slug="wsb")
        property_id = _bootstrap_property(session)

        template_a = _bootstrap_template(session, workspace_id=workspace_a)
        template_b = _bootstrap_template(session, workspace_id=workspace_b)

        schedule_a = _bootstrap_schedule(
            session,
            workspace_id=workspace_a,
            template_id=template_a,
            property_id=property_id,
        )
        _bootstrap_schedule(
            session,
            workspace_id=workspace_b,
            template_id=template_b,
            property_id=property_id,
        )

        report = generate_task_occurrences(
            _ctx(workspace_a, slug="wsa"),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )
        assert report.schedules_walked == 1

        rows = session.scalars(select(Occurrence)).all()
        assert all(r.workspace_id == workspace_a for r in rows)
        assert all(r.schedule_id == schedule_a for r in rows)


# ---------------------------------------------------------------------------
# Audit summary row
# ---------------------------------------------------------------------------


class TestAuditSummary:
    """Each invocation writes one ``schedules.generation_tick`` row."""

    def test_writes_one_tick_audit_row(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )

        audit_rows = session.scalars(
            select(AuditLog).where(AuditLog.action == "schedules.generation_tick")
        ).all()
        assert len(audit_rows) == 1
        diff = audit_rows[0].diff
        assert isinstance(diff, dict)
        assert diff["schedules_walked"] == report.schedules_walked
        assert diff["tasks_created"] == report.tasks_created
        assert diff["skipped_duplicate"] == report.skipped_duplicate
        assert diff["skipped_for_closure"] == report.skipped_for_closure
        assert diff["horizon_days"] == DEFAULT_HORIZON_DAYS
        assert "tick_at" in diff


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


class TestGuardRails:
    """Input validation — the generator rejects nonsense rather than loop."""

    def test_rejects_non_positive_horizon(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        with pytest.raises(ValueError, match="horizon_days"):
            generate_task_occurrences(
                _ctx(workspace_id),
                session=session,
                now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
                horizon_days=0,
                clock=clock,
                event_bus=bus,
            )

    def test_rejects_naive_now(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        with pytest.raises(ValueError, match="timezone-aware"):
            generate_task_occurrences(
                _ctx(workspace_id),
                session=session,
                now=datetime(2026, 4, 20, 8, 0),  # naive
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# Hooks (checklist + assignment)
# ---------------------------------------------------------------------------


class TestInjectableHooks:
    """Checklist expansion and assignment hooks fire once per new row."""

    def test_hooks_are_called_per_new_row(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
        )

        checklist_calls: list[str] = []
        assignment_calls: list[str] = []

        def _expand(
            _session: Session,
            _ctx: WorkspaceContext,
            occurrence_id: str,
            _template: TaskTemplate,
            _scheduled_for_local: datetime,
        ) -> None:
            checklist_calls.append(occurrence_id)

        def _assign(
            _session: Session,
            _ctx: WorkspaceContext,
            occurrence_id: str,
        ) -> None:
            assignment_calls.append(occurrence_id)

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
            expand_checklist=_expand,
            assign=_assign,
        )

        assert len(checklist_calls) == report.tasks_created
        assert len(assignment_calls) == report.tasks_created
        assert set(checklist_calls) == set(report.new_task_ids)
        assert set(assignment_calls) == set(report.new_task_ids)


# ---------------------------------------------------------------------------
# RDATE / EXDATE interaction
# ---------------------------------------------------------------------------


class TestRDateExDate:
    """RDATE adds, EXDATE skips; both are honoured by the generator."""

    def test_exdate_suppresses_specific_occurrence(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            exdate_local="2026-04-25T09:00",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 15, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )

        locals_created = {
            row.scheduled_for_local for row in session.scalars(select(Occurrence)).all()
        }
        assert "2026-04-25T09:00:00" not in locals_created
        assert report.tasks_created >= 1

    def test_rdate_adds_one_off_occurrence(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            # Extra date that's NOT a Saturday — must still appear.
            rdate_local="2026-04-22T09:00",
        )

        generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 15, 8, 0, tzinfo=UTC),
            clock=clock,
            event_bus=bus,
        )

        locals_created = {
            row.scheduled_for_local for row in session.scalars(select(Occurrence)).all()
        }
        assert "2026-04-22T09:00:00" in locals_created


# ---------------------------------------------------------------------------
# Pathological RRULE + cross-timezone active-range
# ---------------------------------------------------------------------------


class TestFarPastAnchor:
    """An ancient ``dtstart_local`` must not starve future occurrences.

    The generator's previous loop iterated from ``dtstart`` forward with
    a per-schedule cap; a daily schedule anchored decades back could
    exhaust the cap before reaching "now" and silently stop generating.
    """

    def test_ancient_anchor_still_materialises_future_occurrences(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session)
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            rrule="FREQ=DAILY",
            dtstart_local="1990-01-01T09:00",  # 36+ years in the past
            active_from="2026-04-19",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            now=datetime(2026, 4, 19, 0, 0, tzinfo=UTC),
            horizon_days=7,
            clock=clock,
            event_bus=bus,
        )

        # Without the fix, the 10k cap would hit around 2017 and no
        # future row would appear. With bounded ``.between()`` we get
        # exactly the horizon window.
        assert report.tasks_created >= 7
        rows = session.scalars(select(Occurrence)).all()
        for row in rows:
            assert row.scheduled_for_local is not None
            assert row.scheduled_for_local >= "2026-04-19"


class TestCrossTimezoneActiveRange:
    """Schedules in property tz near the day boundary must not be dropped.

    The SQL active-range gate runs in UTC; the per-candidate filter
    runs in property-local tz. The SQL gate is widened by ±1 day so
    properties east or west of UTC don't lose a day off their active
    range at the boundary.
    """

    def test_active_until_today_in_far_east_tz_not_dropped_early(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        # Pacific/Auckland is UTC+12/+13. At 2026-04-20 11:00 UTC,
        # local date in Auckland is already 2026-04-20 23:00 (same
        # day) — but at 2026-04-20 13:00 UTC it rolls to 2026-04-21.
        # Picking the worst case: UTC today is day N while Auckland
        # is day N+1, a schedule with ``active_until = day N+1``
        # (property-local) must still run.
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session, timezone="Pacific/Auckland")
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            rrule="FREQ=DAILY",
            dtstart_local="2026-04-20T09:00",
            active_from="2026-04-20",
            # Auckland-local today = 2026-04-21 when UTC is 13:00Z;
            # the schedule's last active Auckland-local day.
            active_until="2026-04-21",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            # 13:00Z → 01:00 next day Auckland-local (NZST UTC+12).
            now=datetime(2026, 4, 20, 13, 0, tzinfo=UTC),
            horizon_days=7,
            clock=clock,
            event_bus=bus,
        )

        # Without the widened gate, the SQL predicate would compare
        # ``active_until='2026-04-21' >= utc_today='2026-04-20'`` (true
        # here) — but the converse case (schedule east, UTC lags) was
        # broken. Keep this as a symmetric assertion: schedules_walked
        # must include this schedule.
        assert report.schedules_walked == 1
        assert report.tasks_created >= 1

    def test_active_until_today_in_far_west_tz_not_dropped_early(
        self, session: Session, bus: EventBus, clock: FrozenClock
    ) -> None:
        # Pacific/Samoa is UTC-11 in winter. At 2026-04-20 01:00 UTC,
        # Samoa-local is 2026-04-19 14:00 — still yesterday. A
        # schedule with ``active_until = '2026-04-19'`` (the last
        # Samoa-local active day) would previously be dropped early
        # because the SQL gate compares against UTC-today='2026-04-20'
        # which is > '2026-04-19'.
        workspace_id = _bootstrap_workspace(session, slug="ws1")
        property_id = _bootstrap_property(session, timezone="Pacific/Samoa")
        template_id = _bootstrap_template(session, workspace_id=workspace_id)
        _bootstrap_schedule(
            session,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            rrule="FREQ=DAILY",
            dtstart_local="2026-04-18T09:00",
            active_from="2026-04-18",
            # Samoa-local last day.
            active_until="2026-04-19",
        )

        report = generate_task_occurrences(
            _ctx(workspace_id),
            session=session,
            # 01:00Z → 14:00 prior-day Samoa-local (SST UTC-11).
            now=datetime(2026, 4, 20, 1, 0, tzinfo=UTC),
            horizon_days=7,
            clock=clock,
            event_bus=bus,
        )

        assert report.schedules_walked == 1
        # The remaining active Samoa-local day is 2026-04-19; we
        # expect at least that day's occurrence (if it hasn't been
        # filtered by the per-candidate check).
        assert report.tasks_created >= 1
