"""Integration tests for :func:`app.worker.tasks.generator.generate_task_occurrences`.

Runs against the real Alembic-migrated schema (via
``tests/integration/conftest.py::migrate_once``) so the partial
unique index the cd-22e migration adds is exercised end-to-end. The
sibling unit file ``tests/unit/tasks/test_generator.py`` covers the
per-branch logic against a plain ``Base.metadata.create_all`` engine.

Covers:

* Two consecutive runs over the same horizon leave the ``occurrence``
  row count stable (the partial unique index backstops the
  pre-flight SELECT).
* Running across two workspaces doesn't leak across them (tenancy
  assertion on the integration stack).

See ``docs/specs/06-tasks-and-scheduling.md`` §"Generation" and
``docs/specs/02-domain-model.md`` §"occurrence".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.tenancy import tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.generator import generate_task_occurrences

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`.

    The generator sets the context per workspace on its own (via the
    ``ctx`` argument); leaving a stale one from a prior test would
    silently trip the tenant filter during an unrelated SELECT.
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


def _seed_workspace_and_schedule(session: Session, *, slug: str) -> tuple[str, str]:
    """Insert a workspace + property + template + schedule; return ids.

    Wrapped in a ``tenant_agnostic`` block so the SELECT paths the
    ORM uses during the INSERT (composite PK look-aheads) don't
    trip the tenant filter with no active context.
    """
    # justification: seeding a fresh workspace runs before any
    # context is set; the filter would otherwise reject the insert
    # look-aheads.
    with tenant_agnostic():
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

        template_id = new_ulid()
        session.add(
            TaskTemplate(
                id=template_id,
                workspace_id=workspace_id,
                title="Villa Sud pool",
                name="Villa Sud pool",
                description_md="",
                default_duration_min=60,
                duration_minutes=60,
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

        schedule_id = new_ulid()
        session.add(
            Schedule(
                id=schedule_id,
                workspace_id=workspace_id,
                template_id=template_id,
                property_id=property_id,
                name="Villa Sud pool schedule",
                area_id=None,
                rrule_text="FREQ=WEEKLY;BYDAY=SA",
                dtstart=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                dtstart_local="2026-04-18T09:00",
                until=None,
                duration_minutes=60,
                rdate_local="",
                exdate_local="",
                active_from="2026-04-01",
                active_until=None,
                paused_at=None,
                deleted_at=None,
                assignee_user_id=None,
                backup_assignee_user_ids=[],
                assignee_role=None,
                enabled=True,
                next_generation_at=None,
                created_at=_PINNED,
            )
        )
        session.flush()
    return workspace_id, schedule_id


def _count_occurrences(session: Session, workspace_id: str) -> int:
    """Count occurrences in ``workspace_id`` without the tenant filter.

    The generator runs inside the filter; the assertion-side query
    below uses ``tenant_agnostic`` so we can count across both
    workspaces in the leak-test case.
    """
    # justification: integration assertions need cross-workspace
    # visibility the tenant filter would block.
    with tenant_agnostic():
        stmt = select(Occurrence).where(Occurrence.workspace_id == workspace_id)
        return len(session.scalars(stmt).all())


class TestGeneratorIntegration:
    """End-to-end: migrations applied, real index enforced."""

    def test_two_runs_insert_once(self, db_session: Session) -> None:
        """Two consecutive runs over the same horizon don't duplicate rows.

        The partial unique index
        ``uq_occurrence_schedule_scheduled_for_local`` (cd-22e) is
        the backstop; the SELECT pre-flight in the generator keeps
        the INSERT side from colliding in the common case.
        """
        workspace_id, _ = _seed_workspace_and_schedule(db_session, slug="ws1")
        bus = EventBus()
        clock = FrozenClock(_PINNED)
        now = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)
        ctx = _ctx(workspace_id)

        first = generate_task_occurrences(
            ctx, session=db_session, now=now, clock=clock, event_bus=bus
        )
        assert first.tasks_created > 0
        first_count = _count_occurrences(db_session, workspace_id)
        assert first_count == first.tasks_created

        second = generate_task_occurrences(
            ctx, session=db_session, now=now, clock=clock, event_bus=bus
        )
        assert second.tasks_created == 0
        assert second.skipped_duplicate == first.tasks_created
        assert _count_occurrences(db_session, workspace_id) == first_count

    def test_does_not_leak_across_workspaces(self, db_session: Session) -> None:
        """Generator run for A does not materialise B's schedules."""
        ws_a, _ = _seed_workspace_and_schedule(db_session, slug="wsa")
        ws_b, _ = _seed_workspace_and_schedule(db_session, slug="wsb")
        bus = EventBus()
        clock = FrozenClock(_PINNED)
        now = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)

        generate_task_occurrences(
            _ctx(ws_a, slug="wsa"),
            session=db_session,
            now=now,
            clock=clock,
            event_bus=bus,
        )

        assert _count_occurrences(db_session, ws_a) > 0
        assert _count_occurrences(db_session, ws_b) == 0
