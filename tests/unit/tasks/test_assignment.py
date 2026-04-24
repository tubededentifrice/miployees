"""Unit tests for :mod:`app.domain.tasks.assignment`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_oneoff.py`` — fresh engine per test, load
every sibling ``models`` module onto the shared metadata, run
``create_all``, drive the service with :class:`FrozenClock` and a
private :class:`EventBus` so subscriptions do not leak between tests.

Covers cd-8luu:

* ``override_user_id`` path — direct assign, ``assignment_source =
  "manual"``; emits ``task.assigned`` on a first assign,
  ``task.reassigned`` when the move replaces a previous holder.
* Primary walk — the schedule's ``default_assignee`` wins when
  available.
* Backup walk — each index in ``backup_assignee_user_ids`` takes
  over in order; :attr:`AssignmentResult.backup_index` lands on the
  result + audit row.
* Candidate pool — step 2 runs when primary/backups are all
  unavailable (or the schedule has none); tiebreakers sort by
  fewest-tasks → oldest-history → user_id.
* Candidate pool excludes users already tried in step 1.
* Three event variants —
  :class:`app.events.types.TaskAssigned` on success,
  :class:`app.events.types.TaskPrimaryUnavailable` when step 1 was
  attempted but both it and the pool fail,
  :class:`app.events.types.TaskUnassigned` when no step 1 existed
  and the pool was empty.
* Audit row carries ``assignment_source`` and ``candidate_count``.
* :func:`reassign_task` requires a current assignee; idempotent
  when the target equals the current holder.
* :func:`unassign_task` emits ``task.unassigned`` with the caller's
  reason; no-op when already unassigned.
* :func:`build_assignment_hook` returns a
  :data:`~app.domain.tasks.oneoff.AssignmentHook`-shaped callable
  that silences its private events but still writes through.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Assignment
algorithm".
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.assignment import (
    AvailabilityPort,
    AvailabilityVerdict,
    TaskAlreadyAssigned,
    TaskNotFound,
    assign_task,
    availability_for,
    build_assignment_hook,
    reassign_task,
    unassign_task,
)
from app.events.bus import EventBus
from app.events.types import (
    TaskAssigned,
    TaskPrimaryUnavailable,
    TaskReassigned,
    TaskUnassigned,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"


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


def _ctx(workspace_id: str, *, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(session: Session, *, slug: str = "ws") -> str:
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


def _bootstrap_property(session: Session) -> str:
    prop_id = new_ulid()
    session.add(
        Property(
            id=prop_id,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return prop_id


def _bootstrap_user(session: Session, *, user_id: str | None = None) -> str:
    """Insert a minimal user row with a fresh id.

    ``user_id`` is optional — callers that do not need a stable id
    across tests let the helper mint a ``new_ulid`` so the PII
    redactor (see :mod:`app.util.redact`) does not mistake a long
    run of zeros in a pinned test id for a payment-card number and
    rewrite it as ``<redacted:pan>`` inside audit diffs.
    """
    uid = user_id if user_id is not None else new_ulid()
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


def _bootstrap_template(session: Session, *, workspace_id: str) -> str:
    tpl = TaskTemplate(
        id=new_ulid(),
        workspace_id=workspace_id,
        title="Pool clean",
        name="Pool clean",
        description_md="",
        default_duration_min=30,
        duration_minutes=30,
        required_evidence="none",
        photo_required=False,
        default_assignee_role=None,
        role_id="role-pool",
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
    session.add(tpl)
    session.flush()
    return tpl.id


def _bootstrap_schedule(
    session: Session,
    *,
    workspace_id: str,
    template_id: str,
    property_id: str | None,
    default_assignee: str | None,
    backups: Sequence[str] = (),
) -> str:
    sid = new_ulid()
    session.add(
        Schedule(
            id=sid,
            workspace_id=workspace_id,
            template_id=template_id,
            property_id=property_id,
            name="Schedule",
            area_id=None,
            rrule_text="FREQ=WEEKLY;BYDAY=SA",
            dtstart=_PINNED,
            dtstart_local="2026-04-19T14:00",
            until=None,
            duration_minutes=30,
            rdate_local="",
            exdate_local="",
            active_from="2026-04-01",
            active_until=None,
            paused_at=None,
            deleted_at=None,
            assignee_user_id=default_assignee,
            backup_assignee_user_ids=list(backups),
            assignee_role=None,
            enabled=True,
            next_generation_at=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return sid


def _bootstrap_occurrence(
    session: Session,
    *,
    workspace_id: str,
    schedule_id: str | None,
    property_id: str | None,
    template_id: str | None = None,
    expected_role_id: str | None = "role-pool",
    assignee_user_id: str | None = None,
    scheduled_for_local: str = "2026-04-19T14:00",
) -> str:
    oid = new_ulid()
    starts_at = _PINNED
    session.add(
        Occurrence(
            id=oid,
            workspace_id=workspace_id,
            schedule_id=schedule_id,
            template_id=template_id,
            property_id=property_id,
            assignee_user_id=assignee_user_id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            scheduled_for_local=scheduled_for_local,
            originally_scheduled_for=scheduled_for_local,
            state="scheduled",
            cancellation_reason=None,
            title="Pool clean",
            description_md="",
            priority="normal",
            photo_evidence="disabled",
            duration_minutes=30,
            area_id=None,
            unit_id=None,
            expected_role_id=expected_role_id,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=None,
            created_at=starts_at,
        )
    )
    session.flush()
    return oid


def _record(
    bus: EventBus,
) -> tuple[
    list[TaskAssigned],
    list[TaskReassigned],
    list[TaskUnassigned],
    list[TaskPrimaryUnavailable],
]:
    assigned: list[TaskAssigned] = []
    reassigned: list[TaskReassigned] = []
    unassigned: list[TaskUnassigned] = []
    primary_unavail: list[TaskPrimaryUnavailable] = []
    bus.subscribe(TaskAssigned)(assigned.append)
    bus.subscribe(TaskReassigned)(reassigned.append)
    bus.subscribe(TaskUnassigned)(unassigned.append)
    bus.subscribe(TaskPrimaryUnavailable)(primary_unavail.append)
    return assigned, reassigned, unassigned, primary_unavail


def _block(*user_ids: str) -> AvailabilityPort:
    blocked = frozenset(user_ids)

    def _port(
        session: Session,
        ctx: WorkspaceContext,
        user_id: str,
        local_dt: datetime,
        property_id: str | None,
    ) -> AvailabilityVerdict:
        _ = session, ctx, local_dt, property_id
        if user_id in blocked:
            return AvailabilityVerdict(available=False, reason="blocked")
        return AvailabilityVerdict(available=True)

    return _port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOverride:
    """``override_user_id`` skips the algorithm and writes the pick through."""

    def test_first_assignment(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
        )
        assigned, reassigned, unassigned, _ = _record(bus)

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            override_user_id=u1,
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == u1
        assert result.source == "manual"
        assert result.candidate_count == 0
        row = session.get(Occurrence, occ)
        assert row is not None
        assert row.assignee_user_id == u1
        assert len(assigned) == 1 and assigned[0].assigned_to == u1
        assert reassigned == []
        assert unassigned == []

    def test_override_over_previous_fires_reassigned(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        u2 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            assignee_user_id=u1,
        )
        assigned, reassigned, _, _ = _record(bus)

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            override_user_id=u2,
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == u2
        assert result.source == "manual"
        assert assigned == []
        assert len(reassigned) == 1
        assert reassigned[0].previous_user_id == u1
        assert reassigned[0].new_user_id == u2

    def test_task_not_found_raises(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        with pytest.raises(TaskNotFound):
            assign_task(
                session,
                _ctx(ws),
                "task-does-not-exist",
                override_user_id="u1",
                clock=clock,
                event_bus=bus,
            )


class TestPrimaryAndBackups:
    """Primary-then-backup ordering per §06 step 1."""

    def test_primary_wins_when_available(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        backup_a = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[backup_a],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )
        assigned, _, _, _ = _record(bus)

        result = assign_task(session, _ctx(ws), occ, clock=clock, event_bus=bus)

        assert result.assigned_user_id == primary
        assert result.source == "primary"
        assert result.backup_index is None
        assert result.candidate_count == 0
        assert len(assigned) == 1

    def test_first_backup_used_when_primary_blocked(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        b0 = _bootstrap_user(session)
        b1 = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[b0, b1],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            available=_block(primary),
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == b0
        assert result.source == "backup"
        assert result.backup_index == 0

    def test_second_backup_used_when_primary_and_first_backup_blocked(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        b0 = _bootstrap_user(session)
        b1 = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[b0, b1],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            available=_block(primary, b0),
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == b1
        assert result.source == "backup"
        assert result.backup_index == 1

    def test_rota_filter_rejects_primary(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """A user available per the stack but outside their rota falls
        through — matches spec §"Rota composition"."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        backup = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[backup],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )

        def rota(
            sess: Session,
            ctx: WorkspaceContext,
            user_id: str,
            property_id: str | None,
            local_dt: datetime,
        ) -> bool:
            _ = sess, ctx, property_id, local_dt
            return user_id != primary

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            rota=rota,
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == backup


class TestCandidatePool:
    """Step 2-4 — candidate pool + tiebreakers."""

    def test_pool_excludes_primary_and_backups_already_tried(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        b0 = _bootstrap_user(session)
        pool_user = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[b0],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )

        seen_exclude: list[tuple[str, ...]] = []

        def pool(
            sess: Session,
            ctx: WorkspaceContext,
            role_id: str | None,
            property_id: str | None,
            exclude: Sequence[str],
        ) -> Sequence[str]:
            _ = sess, ctx, role_id, property_id
            seen_exclude.append(tuple(exclude))
            # Return every user including the tried ones to prove the
            # service filters rather than trusting the port.
            return [primary, b0, pool_user]

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            available=_block(primary, b0),
            pool=pool,
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == pool_user
        assert result.source == "candidate_pool"
        assert result.candidate_count == 1
        assert seen_exclude == [(primary, b0)]

    def test_tiebreakers_fewest_tasks_then_rotation(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        u_busy = _bootstrap_user(session)
        u_oldest = _bootstrap_user(session)
        u_recent = _bootstrap_user(session)
        # No schedule → step 1 is a no-op; algorithm goes straight to
        # the candidate pool.
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
        )

        class FakeWorkload:
            def count_tasks_in_window(
                self,
                session: Session,
                ctx: WorkspaceContext,
                *,
                user_id: str,
                property_id: str | None,
                local_dt: datetime,
                window_days: int = 7,
            ) -> int:
                _ = session, ctx, property_id, local_dt, window_days
                # u_busy has five, u_oldest and u_recent both have one
                # → the busy one loses on the primary sort.
                return {u_busy: 5, u_oldest: 1, u_recent: 1}[user_id]

            def last_task_at(
                self,
                session: Session,
                ctx: WorkspaceContext,
                *,
                user_id: str,
                property_id: str | None,
            ) -> datetime | None:
                _ = session, ctx, property_id
                # u_oldest last worked here a week ago; u_recent
                # yesterday. Rotation pick is u_oldest.
                return {
                    u_busy: datetime(2026, 4, 1),
                    u_oldest: datetime(2026, 4, 12),
                    u_recent: datetime(2026, 4, 18),
                }[user_id]

        def pool(
            sess: Session,
            ctx: WorkspaceContext,
            role_id: str | None,
            property_id: str | None,
            exclude: Sequence[str],
        ) -> Sequence[str]:
            _ = sess, ctx, role_id, property_id, exclude
            return [u_recent, u_busy, u_oldest]

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            pool=pool,
            workload=FakeWorkload(),
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == u_oldest
        assert result.source == "candidate_pool"
        assert result.candidate_count == 3

    def test_never_worked_here_beats_recent_under_rotation(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        u_new = _bootstrap_user(session)
        u_recent = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
        )

        class FakeWorkload:
            def count_tasks_in_window(self, *a: Any, **k: Any) -> int:
                return 0

            def last_task_at(
                self,
                session: Session,
                ctx: WorkspaceContext,
                *,
                user_id: str,
                property_id: str | None,
            ) -> datetime | None:
                return None if user_id == u_new else datetime(2026, 4, 18)

        def pool(*a: Any, **k: Any) -> Sequence[str]:
            return [u_recent, u_new]

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            pool=pool,
            workload=FakeWorkload(),
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id == u_new


class TestZeroCandidates:
    """§06 step 5 — unassigned + the right event."""

    def test_primary_attempted_empty_pool_fires_primary_unavailable(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )
        assigned, _, unassigned, primary_unavail = _record(bus)

        result = assign_task(
            session,
            _ctx(ws),
            occ,
            available=_block(primary),
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id is None
        assert result.source == "unassigned"
        assert result.candidate_count == 0
        row = session.get(Occurrence, occ)
        assert row is not None and row.assignee_user_id is None
        assert assigned == []
        assert unassigned == []
        assert len(primary_unavail) == 1
        assert primary_unavail[0].candidate_count == 0

    def test_no_step_one_empty_pool_fires_task_unassigned(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
        )
        assigned, _, unassigned, primary_unavail = _record(bus)

        result = assign_task(session, _ctx(ws), occ, clock=clock, event_bus=bus)

        assert result.assigned_user_id is None
        assert result.source == "unassigned"
        assert assigned == []
        assert primary_unavail == []
        assert len(unassigned) == 1
        assert unassigned[0].reason == "candidate_pool_empty"

    def test_clears_stale_assignee_on_zero_candidates(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        existing = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
            assignee_user_id=existing,
        )

        assign_task(session, _ctx(ws), occ, clock=clock, event_bus=bus)

        row = session.get(Occurrence, occ)
        assert row is not None and row.assignee_user_id is None


class TestAudit:
    """Audit row carries ``assignment_source`` + ``candidate_count``."""

    def test_primary_branch_writes_audit(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )

        assign_task(session, _ctx(ws), occ, clock=clock, event_bus=bus)

        audit = session.scalars(select(AuditLog).where(AuditLog.entity_id == occ)).one()
        assert audit.action == "task.assigned"
        assert audit.entity_kind == "task"
        assert audit.diff["after"]["assignment_source"] == "primary"
        assert audit.diff["after"]["candidate_count"] == 0
        assert audit.diff["after"]["assigned_user_id"] == primary
        assert audit.diff["before"]["assigned_user_id"] is None

    def test_backup_branch_records_index(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        b0 = _bootstrap_user(session)
        b1 = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
            backups=[b0, b1],
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )

        assign_task(
            session,
            _ctx(ws),
            occ,
            available=_block(primary, b0),
            clock=clock,
            event_bus=bus,
        )

        audit = session.scalars(select(AuditLog).where(AuditLog.entity_id == occ)).one()
        assert audit.diff["after"]["assignment_source"] == "backup"
        assert audit.diff["after"]["backup_index"] == 1

    def test_zero_candidate_branch_writes_unassigned_action(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
        )

        assign_task(session, _ctx(ws), occ, clock=clock, event_bus=bus)

        audit = session.scalars(select(AuditLog).where(AuditLog.entity_id == occ)).one()
        assert audit.action == "task.unassigned"
        assert audit.diff["after"]["assignment_source"] == "unassigned"
        assert audit.diff["after"]["reason"] == "candidate_pool_empty"


class TestReassign:
    """Explicit moves via :func:`reassign_task`."""

    def test_happy_path_fires_reassigned(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        u2 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            assignee_user_id=u1,
        )
        assigned, reassigned, _, _ = _record(bus)

        result = reassign_task(session, _ctx(ws), occ, u2, clock=clock, event_bus=bus)

        assert result.assigned_user_id == u2
        assert result.source == "manual"
        assert assigned == []
        assert len(reassigned) == 1
        assert reassigned[0].previous_user_id == u1
        assert reassigned[0].new_user_id == u2

    def test_requires_current_assignee(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
        )

        with pytest.raises(TaskAlreadyAssigned):
            reassign_task(session, _ctx(ws), occ, u1, clock=clock, event_bus=bus)

    def test_noop_when_target_equals_current(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            assignee_user_id=u1,
        )
        _, reassigned, _, _ = _record(bus)

        result = reassign_task(session, _ctx(ws), occ, u1, clock=clock, event_bus=bus)

        assert result.assigned_user_id == u1
        assert reassigned == []
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == occ)
        ).all()
        assert audits == []


class TestUnassign:
    """Explicit clears via :func:`unassign_task`."""

    def test_happy_path_fires_unassigned_with_reason(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            assignee_user_id=u1,
        )
        _, _, unassigned, _ = _record(bus)

        result = unassign_task(
            session,
            _ctx(ws),
            occ,
            reason="manager_cleared",
            clock=clock,
            event_bus=bus,
        )

        assert result.assigned_user_id is None
        assert result.source == "unassigned"
        assert len(unassigned) == 1
        assert unassigned[0].reason == "manager_cleared"
        row = session.get(Occurrence, occ)
        assert row is not None and row.assignee_user_id is None

    def test_noop_when_already_unassigned(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
        )
        _, _, unassigned, _ = _record(bus)

        unassign_task(
            session,
            _ctx(ws),
            occ,
            reason="redundant",
            clock=clock,
            event_bus=bus,
        )

        assert unassigned == []

    def test_free_text_reason_rejected_at_publish_time(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """``unassign_task`` passes its ``reason`` through to the
        :class:`TaskUnassigned` event, which enforces an
        identifier-shape on the field. A caller that tries to ship a
        manager-typed note (or any free text) gets a
        :class:`ValidationError` at publish time so the PII posture
        is enforced at the service boundary."""
        from pydantic import ValidationError

        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        u1 = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            assignee_user_id=u1,
        )

        with pytest.raises(ValidationError):
            unassign_task(
                session,
                _ctx(ws),
                occ,
                reason="Manager decided to clear this for the guest",
                clock=clock,
                event_bus=bus,
            )


class TestAvailabilityPublic:
    """``availability_for`` is the public entry point scheduler UI re-uses."""

    def test_default_port_always_available(self, session: Session) -> None:
        ws = _bootstrap_workspace(session)
        ctx = _ctx(ws)
        verdict = availability_for(
            session, ctx, "u1", datetime(2026, 4, 19, 14, 0), None
        )
        assert verdict.available is True

    def test_injectable_port_overrides_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session)
        ctx = _ctx(ws)
        verdict = availability_for(
            session,
            ctx,
            "u1",
            datetime(2026, 4, 19, 14, 0),
            None,
            port=_block("u1"),
        )
        assert verdict.available is False


class TestAssignmentHook:
    """``build_assignment_hook`` yields a silent :data:`AssignmentHook`."""

    def test_runs_algorithm_but_does_not_publish_on_caller_bus(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        primary = _bootstrap_user(session)
        sched = _bootstrap_schedule(
            session,
            workspace_id=ws,
            template_id=tpl,
            property_id=prop,
            default_assignee=primary,
        )
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=sched,
            property_id=prop,
            template_id=tpl,
        )
        assigned, reassigned, unassigned, primary_unavail = _record(bus)

        hook = build_assignment_hook(clock=clock)
        chosen = hook(session, _ctx(ws), occ)

        assert chosen == primary
        row = session.get(Occurrence, occ)
        assert row is not None and row.assignee_user_id == primary
        # The caller's bus stays silent — the hook routes events to a
        # private bus so the one-off / generator callers can keep
        # control of their outward event fanout.
        assert assigned == [] and reassigned == []
        assert unassigned == [] and primary_unavail == []
        # Audit still lands so the algorithm's decision is observable.
        audit = session.scalars(select(AuditLog).where(AuditLog.entity_id == occ)).one()
        assert audit.diff["after"]["assignment_source"] == "primary"

    def test_returns_none_when_unassigned(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
        )

        hook = build_assignment_hook(clock=clock)
        chosen = hook(session, _ctx(ws), occ)

        assert chosen is None


class TestDefaultWorkloadPort:
    """The default :class:`WorkloadPort` queries the ``occurrence`` table."""

    def test_counts_7_day_window_tasks_for_user_at_property(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """Spec §06 step 4 — "fewest tasks in the 7-day window around
        ``scheduled_for_local``". ``window_days`` is the **total**
        width; the default 7 means ±3.5 days around the anchor. The
        target anchor below is ``2026-04-19T14:00`` → window spans
        ``[2026-04-16T02:00, 2026-04-23T02:00]`` (inclusive)."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        candidate = _bootstrap_user(session)
        other = _bootstrap_user(session)

        # Two tasks for the candidate inside the ±3.5d window.
        # 17 Apr 12:00 and 20 Apr 16:00 both sit inside
        # [16 Apr 02:00, 23 Apr 02:00].
        for day, hour in [(17, 12), (20, 16)]:
            _bootstrap_occurrence(
                session,
                workspace_id=ws,
                schedule_id=None,
                property_id=prop,
                template_id=tpl,
                assignee_user_id=candidate,
                scheduled_for_local=f"2026-04-{day:02d}T{hour:02d}:00",
            )
        # Just outside the lower bound (15 Apr 09:00 < 16 Apr 02:00).
        # Must not count — verifies the boundary arithmetic.
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
            assignee_user_id=candidate,
            scheduled_for_local="2026-04-15T09:00",
        )
        # Far future — outside the window by weeks.
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
            assignee_user_id=candidate,
            scheduled_for_local="2026-05-15T09:00",
        )
        # One for another user inside the window → must not count
        # against the candidate (user-id filter).
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
            assignee_user_id=other,
            scheduled_for_local="2026-04-19T09:00",
        )
        # Target occurrence (the one being tiebroken). Assigning
        # ``other`` so the tiebreaker really compares `candidate` vs
        # `other` on the busy-ness front.
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
        )

        def pool(*a: Any, **k: Any) -> Sequence[str]:
            return [candidate, other]

        # Block nothing; the default workload port queries the DB so
        # the tiebreaker sees two tasks on candidate vs one on other
        # inside the ±3.5d window. → other wins (fewest).
        result = assign_task(
            session,
            _ctx(ws),
            occ,
            pool=pool,
            clock=clock,
            event_bus=bus,
        )
        assert result.assigned_user_id == other
        assert result.candidate_count == 2

    def test_last_task_at_returns_latest_scheduled_for_local(
        self, session: Session
    ) -> None:
        """The default rotation input picks the most-recent prior task
        at the property for a user. Covers the direct code path; the
        tiebreaker integration uses a fake ``WorkloadPort`` elsewhere.
        """
        from app.domain.tasks.assignment import _DEFAULT_WORKLOAD

        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        tpl = _bootstrap_template(session, workspace_id=ws)
        user = _bootstrap_user(session)
        other = _bootstrap_user(session)

        for day in (1, 10, 5):
            _bootstrap_occurrence(
                session,
                workspace_id=ws,
                schedule_id=None,
                property_id=prop,
                template_id=tpl,
                assignee_user_id=user,
                scheduled_for_local=f"2026-04-{day:02d}T09:00",
            )
        # Different user at same property — must be ignored.
        _bootstrap_occurrence(
            session,
            workspace_id=ws,
            schedule_id=None,
            property_id=prop,
            template_id=tpl,
            assignee_user_id=other,
            scheduled_for_local="2026-05-30T09:00",
        )

        latest = _DEFAULT_WORKLOAD.last_task_at(
            session, _ctx(ws), user_id=user, property_id=prop
        )
        assert latest == datetime(2026, 4, 10, 9, 0)

        # No property → no query (rotation skips this path).
        assert (
            _DEFAULT_WORKLOAD.last_task_at(
                session, _ctx(ws), user_id=user, property_id=None
            )
            is None
        )

    def test_last_task_at_none_when_user_has_no_history(self, session: Session) -> None:
        from app.domain.tasks.assignment import _DEFAULT_WORKLOAD

        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # User exists but has never been assigned here.
        user = _bootstrap_user(session)

        assert (
            _DEFAULT_WORKLOAD.last_task_at(
                session, _ctx(ws), user_id=user, property_id=prop
            )
            is None
        )
