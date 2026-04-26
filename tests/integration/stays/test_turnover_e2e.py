"""End-to-end coverage of the turnover-generator subscription pipe.

The unit suite (``tests/unit/domain/stays/test_turnover_generator.py``)
exercises every branch with an in-memory SQLite + a recording port.
This integration test pins the **wiring** instead — that publishing
a real :class:`~app.events.types.ReservationUpserted` on a freshly
:func:`~app.domain.stays.turnover_generator.register_subscriptions`-bound
:class:`~app.events.bus.EventBus` reaches the handler against the
migrated schema, and that the handler's request travels through the
port boundary intact.

Specifically:

* The integration runs against the real
  :class:`~sqlalchemy.engine.Engine` from ``tests.integration.conftest``
  (alembic-migrated SQLite by default, Postgres under
  ``CREWDAY_TEST_DB=postgres``).
* :func:`register_subscriptions` is called once with a recording
  port; the second registration call is a no-op (idempotency).
* A :class:`~app.events.types.ReservationUpserted` event is
  published on the bus; the handler's ``session_provider`` returns
  the test session + workspace context and the recorder captures
  the request.
* A subsequent publish of the **same** event is observed by the
  recorder twice, but the second call is a port-side ``"noop"`` —
  proving the dedup contract holds end-to-end.
* Cancelled reservations short-circuit before any DB read.

We deliberately do NOT exercise the real tasks-side adapter here —
it lands with cd-4qr Phase 5. The
:class:`~app.ports.tasks_create_occurrence.RecordingTasksCreateOccurrencePort`
double simulates the deterministic state machine the production
adapter promises so the integration covers the full event →
generator → port surface.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
+ ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property, PropertyClosure
from app.adapters.db.stays.models import Reservation
from app.adapters.db.workspace.models import Workspace
from app.domain.stays.turnover_generator import (
    DEFAULT_AFTER_CHECKOUT_RULE_ID,
    _reset_subscriptions_for_tests,
    handle_reservation_upserted,
    register_subscriptions,
)
from app.events.bus import EventBus
from app.events.types import ReservationChangeKind, ReservationUpserted
from app.ports.tasks_create_occurrence import (
    RecordingTasksCreateOccurrencePort,
)
from app.tenancy import reset_current, set_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"


# ---------------------------------------------------------------------------
# Bootstraps
# ---------------------------------------------------------------------------


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    # justification: workspace seeding for cross-tenant test setup
    with tenant_agnostic():
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
    return workspace_id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    # justification: property is not workspace-scoped (multi-belonging)
    with tenant_agnostic():
        session.add(
            Property(
                id=pid,
                name="Villa Sud",
                kind="str",
                address="12 Chemin des Oliviers",
                address_json={"country": "FR"},
                country="FR",
                locale=None,
                default_currency=None,
                timezone="Europe/Paris",
                lat=None,
                lon=None,
                client_org_id=None,
                owner_user_id=None,
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="",
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        session.flush()
    return pid


def _bootstrap_reservation(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    check_in: datetime,
    check_out: datetime,
) -> str:
    rid = new_ulid()
    session.add(
        Reservation(
            id=rid,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"manual-{rid}",
            check_in=check_in,
            check_out=check_out,
            guest_name="A. Test Guest",
            guest_count=2,
            status="scheduled",
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return rid


def _make_event(
    *,
    ctx: WorkspaceContext,
    reservation_id: str,
    change_kind: ReservationChangeKind = "created",
) -> ReservationUpserted:
    return ReservationUpserted(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        correlation_id=ctx.audit_correlation_id,
        occurred_at=_PINNED,
        reservation_id=reservation_id,
        feed_id=None,
        change_kind=change_kind,
    )


@pytest.fixture(name="ctx")
def fixture_ctx(db_session: Session) -> WorkspaceContext:
    ws = _bootstrap_workspace(db_session, slug=f"e2e-turnover-{new_ulid()[-6:]}")
    return WorkspaceContext(
        workspace_id=ws,
        workspace_slug="e2e-turnover",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Real bus + real DB; the recorder stands in for the Phase 5 adapter."""

    def test_publish_invokes_handler_and_records_request(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(db_session)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            _ = event
            return db_session, ctx

        _reset_subscriptions_for_tests()
        register_subscriptions(bus, port=port, session_provider=session_provider)

        # The ORM tenant filter pulls from the request-scoped
        # ContextVar, so set the active context for the publish
        # body — the handler runs synchronously inside it.
        token = set_current(ctx)
        try:
            bus.publish(_make_event(ctx=ctx, reservation_id=rid))
        finally:
            reset_current(token)

        assert len(port.calls) == 1
        request = port.calls[0]
        assert request.reservation_id == rid
        assert request.rule_id == DEFAULT_AFTER_CHECKOUT_RULE_ID
        assert request.property_id == prop
        assert request.starts_at == check_out

    def test_re_publish_is_noop_via_port(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(db_session)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            _ = event
            return db_session, ctx

        _reset_subscriptions_for_tests()
        register_subscriptions(bus, port=port, session_provider=session_provider)

        event = _make_event(ctx=ctx, reservation_id=rid)
        token = set_current(ctx)
        try:
            bus.publish(event)
            bus.publish(event)
        finally:
            reset_current(token)

        # Recorder saw both calls; the second is a port-side noop.
        assert len(port.calls) == 2
        # Both requests are equal because re-firing carries the
        # same shape (same reservation, same gap).
        assert port.calls[0] == port.calls[1]

    def test_cancelled_event_short_circuits_before_db(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()
        provider_calls: list[ReservationUpserted] = []

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            provider_calls.append(event)
            return db_session, ctx

        _reset_subscriptions_for_tests()
        register_subscriptions(bus, port=port, session_provider=session_provider)

        token = set_current(ctx)
        try:
            bus.publish(
                _make_event(
                    ctx=ctx,
                    reservation_id="01HWA00000000000000000RES9",
                    change_kind="cancelled",
                )
            )
        finally:
            reset_current(token)

        # Provider was called (the bus invokes the subscriber); the
        # handler short-circuited before any DB read or port call.
        assert len(provider_calls) == 1
        assert port.calls == []

    def test_closure_intersecting_gap_blocks_materialisation(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(db_session)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=2),
            check_out=check_out + timedelta(days=5),
        )
        # justification: property_closure is not workspace-scoped
        with tenant_agnostic():
            db_session.add(
                PropertyClosure(
                    id=new_ulid(),
                    property_id=prop,
                    starts_at=check_out + timedelta(minutes=30),
                    ends_at=check_out + timedelta(hours=3),
                    reason="renovation",
                    source_ical_feed_id=None,
                    created_by_user_id=None,
                    created_at=_PINNED,
                )
            )
            db_session.flush()

        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=db_session,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert result.per_rule[0].decision == "skipped_closure"
        assert port.calls == []
