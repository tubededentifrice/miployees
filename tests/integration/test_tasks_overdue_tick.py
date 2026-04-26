"""Integration tests for :func:`app.worker.tasks.overdue.detect_overdue` (cd-hurw).

Runs against the real Alembic-migrated schema (via
``tests/integration/conftest.py::migrate_once``) so the cd-hurw
``overdue_since`` column, the widened ``state`` CHECK that admits
``'overdue'``, and the
``ix_occurrence_workspace_state_overdue_since`` composite index are
all exercised end-to-end. The sibling unit file
``tests/unit/test_tasks_overdue.py`` covers per-branch logic against a
plain ``Base.metadata.create_all`` engine.

Covers (cd-hurw acceptance criteria, integration shard):

* End-to-end: a stuck task transitions to ``state='overdue'`` with
  ``overdue_since`` stamped, while a sibling task inside the grace
  window stays put.
* Per-property breakdown lands on the ``tasks.overdue_tick`` audit
  row.
* Idempotent re-run: a second tick over the same data set fires no
  new flips and writes only an additional summary audit row.
* APScheduler integration: registering the
  :data:`OVERDUE_DETECT_JOB_ID` job lands it in the scheduler's job
  store (the runtime fan-out is exercised via the unit-level
  per-workspace driver — APScheduler's loop invocation is its own
  test and not in scope here).

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine" and
``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.events.types import TaskOverdue
from app.tenancy import tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.scheduler import (
    OVERDUE_DETECT_INTERVAL_SECONDS,
    OVERDUE_DETECT_JOB_ID,
    create_scheduler,
    register_jobs,
    registered_job_ids,
)
from app.worker.tasks.overdue import (
    DEFAULT_OVERDUE_TICK_SECONDS,
    detect_overdue,
)

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`.

    The sweeper sets the context per workspace via the ``ctx``
    argument; leaving a stale one from a prior test would silently
    trip the tenant filter during an unrelated SELECT.
    """
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


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


def _seed_workspace_property(session: Session, *, slug: str) -> tuple[str, str]:
    """Insert a workspace + property; return ids.

    Wrapped in ``tenant_agnostic`` so the SELECT paths the ORM uses
    during the INSERT (composite PK look-aheads) don't trip the
    tenant filter with no active context.
    """
    with tenant_agnostic():
        workspace_id = new_ulid()
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=f"Workspace {slug}",
                plan="free",
                quota_json={},
                settings_json={},
                created_at=_PINNED,
            )
        )
        session.flush()

        property_id = new_ulid()
        session.add(
            Property(
                id=property_id,
                address="1 Villa Sud Way",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()
    return workspace_id, property_id


def _seed_occurrence(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    state: str,
    ends_at: datetime,
) -> str:
    """Insert one ``occurrence`` row."""
    oid = new_ulid()
    session.add(
        Occurrence(
            id=oid,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=None,
            starts_at=ends_at - timedelta(hours=1),
            ends_at=ends_at,
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


class TestDetectOverdueIntegration:
    """End-to-end: real schema, real CHECK, real index."""

    def test_stuck_task_flips_sibling_stays_put(self, db_session: Session) -> None:
        """Two tasks, one past the grace window, one inside it."""
        workspace_id, property_id = _seed_workspace_property(
            db_session, slug="overdue-int-1"
        )

        # Stuck task — 30 minutes past ends_at; grace 15 → flips.
        stuck_id = _seed_occurrence(
            db_session,
            workspace_id=workspace_id,
            property_id=property_id,
            state="pending",
            ends_at=_PINNED - timedelta(minutes=30),
        )
        # Inside grace — 5 minutes past ends_at; grace 15 → stays.
        ok_id = _seed_occurrence(
            db_session,
            workspace_id=workspace_id,
            property_id=property_id,
            state="pending",
            ends_at=_PINNED - timedelta(minutes=5),
        )

        bus = EventBus()
        captured: list[TaskOverdue] = []
        bus.subscribe(TaskOverdue)(captured.append)
        clock = FrozenClock(_PINNED)
        ctx = _ctx(workspace_id, slug="overdue-int-1")

        report = detect_overdue(
            ctx,
            session=db_session,
            now=_PINNED,
            clock=clock,
            event_bus=bus,
            grace_minutes=15,
        )

        assert report.flipped_count == 1
        assert report.flipped_task_ids == (stuck_id,)
        assert report.per_property_breakdown == {property_id: 1}

        # Stuck row flipped, ok row preserved.
        with tenant_agnostic():
            stuck = db_session.get(Occurrence, stuck_id)
            ok = db_session.get(Occurrence, ok_id)
        assert stuck is not None
        assert stuck.state == "overdue"
        assert stuck.overdue_since is not None
        # SQLite drops tzinfo on round-trip; PostgreSQL preserves.
        # Compare in either frame by stripping the tz from both.
        rt = stuck.overdue_since
        rt_naive = rt.replace(tzinfo=None) if rt.tzinfo is not None else rt
        assert rt_naive == _PINNED.replace(tzinfo=None)
        assert ok is not None
        assert ok.state == "pending"
        assert ok.overdue_since is None

        # Per-property audit row landed.
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "tasks.overdue_tick")
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["per_property_breakdown"] == {property_id: 1}
        assert audits[0].diff["flipped_count"] == 1
        assert audits[0].diff["grace_minutes"] == 15

        # Event payload.
        assert len(captured) == 1
        assert captured[0].task_id == stuck_id
        assert captured[0].slipped_minutes == 30

    def test_idempotent_second_tick(self, db_session: Session) -> None:
        """A second tick over the same data fires no new event."""
        workspace_id, property_id = _seed_workspace_property(
            db_session, slug="overdue-int-2"
        )
        stuck_id = _seed_occurrence(
            db_session,
            workspace_id=workspace_id,
            property_id=property_id,
            state="pending",
            ends_at=_PINNED - timedelta(minutes=30),
        )

        bus = EventBus()
        captured: list[TaskOverdue] = []
        bus.subscribe(TaskOverdue)(captured.append)
        clock = FrozenClock(_PINNED)
        ctx = _ctx(workspace_id, slug="overdue-int-2")

        first = detect_overdue(
            ctx,
            session=db_session,
            now=_PINNED,
            clock=clock,
            event_bus=bus,
            grace_minutes=15,
        )
        assert first.flipped_count == 1
        assert len(captured) == 1

        # Second run — no flip, no new event, but a second summary
        # audit row (one per tick by design).
        second = detect_overdue(
            ctx,
            session=db_session,
            now=_PINNED,
            clock=clock,
            event_bus=bus,
            grace_minutes=15,
        )
        assert second.flipped_count == 0
        assert len(captured) == 1

        with tenant_agnostic():
            stuck = db_session.get(Occurrence, stuck_id)
        assert stuck is not None
        assert stuck.state == "overdue"

        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "tasks.overdue_tick")
        ).all()
        assert len(audits) == 2

    def test_does_not_leak_across_workspaces(self, db_session: Session) -> None:
        """A tick on workspace A does not touch workspace B."""
        ws_a, prop_a = _seed_workspace_property(db_session, slug="overdue-leak-a")
        ws_b, prop_b = _seed_workspace_property(db_session, slug="overdue-leak-b")
        ends = _PINNED - timedelta(minutes=30)
        oid_a = _seed_occurrence(
            db_session,
            workspace_id=ws_a,
            property_id=prop_a,
            state="pending",
            ends_at=ends,
        )
        oid_b = _seed_occurrence(
            db_session,
            workspace_id=ws_b,
            property_id=prop_b,
            state="pending",
            ends_at=ends,
        )

        bus = EventBus()
        clock = FrozenClock(_PINNED)
        report = detect_overdue(
            _ctx(ws_a, slug="overdue-leak-a"),
            session=db_session,
            now=_PINNED,
            clock=clock,
            event_bus=bus,
            grace_minutes=15,
        )
        assert report.flipped_count == 1

        with tenant_agnostic():
            row_a = db_session.get(Occurrence, oid_a)
            row_b = db_session.get(Occurrence, oid_b)
        assert row_a is not None and row_a.state == "overdue"
        assert row_b is not None and row_b.state == "pending"


class TestSchedulerRegistration:
    """The scheduler-wiring layer registers the ``detect_overdue`` job."""

    def test_register_jobs_includes_overdue_tick(self) -> None:
        """``register_jobs`` adds the cd-hurw job alongside the others."""
        scheduler = create_scheduler()
        try:
            register_jobs(scheduler)
            job_ids = registered_job_ids(scheduler)
        finally:
            # Don't ``start()`` the scheduler — registration alone is
            # the contract under test. APScheduler's loop integration
            # is its own test surface (cd-dcl2 covers the lifecycle).
            pass
        assert OVERDUE_DETECT_JOB_ID in job_ids
        # Cadence: surfaced as a public constant the test pins.
        assert OVERDUE_DETECT_INTERVAL_SECONDS == DEFAULT_OVERDUE_TICK_SECONDS
