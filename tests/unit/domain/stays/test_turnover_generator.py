"""Unit tests for :mod:`app.domain.stays.turnover_generator`.

Pure-Python coverage with an in-memory SQLite engine + a recording
:class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`.
The real tasks-side adapter doesn't exist yet (cd-4qr Phase 5); the
recorder asserts request shape + simulates the create / patch /
regenerate state machine so we can exercise every branch the
:class:`HandleResult` discriminator can return:

* Happy path — one default rule fires, recorder sees a ``"created"``
  call.
* Owner-kind reservation skips the default rule (``guest_kind`` not
  in ``guest_kind_filter``).
* Property closure intersecting the gap suppresses the rule entirely.
* No next stay → ``skipped_no_next_stay``.
* Zero / negative gap → ``skipped_zero_gap``.
* Re-firing the same event with identical inputs is a no-op (port
  idempotency contract).
* < 4 h check_out shift patches in place (re-emits a fresh event
  with the new check-out instant).
* >= 4 h check_out shift regenerates.
* Cancelled-event ``change_kind`` short-circuits before any DB read.
* Reservation deleted between publish + handler → graceful skip.
* Same-day turnover (next check-in lands inside the rule's nominal
  duration window) time-boxes the bundle to the gap.
* :func:`register_subscriptions` is idempotent on the same bus.

The unit suite stands up :class:`~app.adapters.db.places.models.Property`,
:class:`~app.adapters.db.stays.models.Reservation`, and
:class:`~app.adapters.db.places.models.PropertyClosure` rows directly
through SQLAlchemy because the generator's queries hit those tables;
the integration suite (``tests/integration/stays/test_turnover_e2e.py``)
exercises the same paths against the real migrated schema.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles" +
§"Airbnb-style edge cases".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyClosure
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import Reservation
from app.adapters.db.workspace.models import Workspace
from app.config import get_settings
from app.domain.stays.turnover_generator import (
    DEFAULT_AFTER_CHECKOUT_RULE_ID,
    DEFAULT_RULES,
    DEFAULT_TURNOVER_DURATION,
    GuestKind,
    ReservationContext,
    StaticReservationContextResolver,
    TurnoverRule,
    _reset_subscriptions_for_tests,
    handle_reservation_upserted,
    register_subscriptions,
)
from app.events.bus import EventBus
from app.events.types import ReservationChangeKind, ReservationUpserted
from app.ports.tasks_create_occurrence import (
    DEFAULT_PATCH_IN_PLACE_THRESHOLD,
    NoopTasksCreateOccurrencePort,
    RecordingTasksCreateOccurrencePort,
    TurnoverOccurrenceRequest,
    TurnoverOccurrenceResult,
)
from app.tenancy import set_current
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve.

    Mirrors the helper in :mod:`tests.unit.domain.stays.test_guest_link_service`.
    """
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


@pytest.fixture(autouse=True)
def fixture_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide minimal :class:`Settings` env so :func:`get_settings` works.

    The ORM tenant filter touches :func:`get_settings` for the
    ``tenant_strict`` mode flag. A throwaway SQLite URL keeps the
    field happy without touching disk — tests use their own
    in-memory engines.
    """
    monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
    # Long deterministic value: the signing-secret seam in
    # :mod:`app.config` reads ``CREWDAY_ROOT_KEY`` via pydantic-
    # settings; HKDF expands the key for downstream callers (audit,
    # token signing). The turnover generator never signs, but the
    # tenancy ORM filter eagerly loads ``Settings`` on first import.
    monkeypatch.setenv(
        "CREWDAY_ROOT_KEY",
        "test-root-key-cd-4qr-deterministic-fixed-32+ chars long for HKDF",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(name="engine_turnover")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_turnover")
def fixture_session(engine_turnover: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_turnover, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(name="ctx")
def fixture_ctx(session_turnover: Session) -> Iterator[WorkspaceContext]:
    """Workspace context + ContextVar binding so the ORM tenant filter passes."""
    ws = _bootstrap_workspace(session_turnover, slug="ws-turnover")
    ctx = WorkspaceContext(
        workspace_id=ws,
        workspace_slug="ws-turnover",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
    )
    token = set_current(ctx)
    try:
        yield ctx
    finally:
        from app.tenancy import reset_current

        reset_current(token)


# ---------------------------------------------------------------------------
# Bootstraps
# ---------------------------------------------------------------------------


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
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
    return workspace_id


def _bootstrap_property(session: Session, *, name: str = "Villa Sud") -> str:
    pid = new_ulid()
    session.add(
        Property(
            id=pid,
            name=name,
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
    status: str = "scheduled",
    external_uid: str | None = None,
) -> str:
    rid = new_ulid()
    session.add(
        Reservation(
            id=rid,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=external_uid or f"manual-{rid}",
            check_in=check_in,
            check_out=check_out,
            guest_name="A. Test Guest",
            guest_count=2,
            status=status,
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return rid


def _bootstrap_closure(
    session: Session,
    *,
    property_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> str:
    cid = new_ulid()
    session.add(
        PropertyClosure(
            id=cid,
            property_id=property_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason="renovation",
            source_ical_feed_id=None,
            created_by_user_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return cid


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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """The single default rule fires for a normal guest reservation."""

    def test_creates_one_request_for_default_rule(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_in = _PINNED + timedelta(days=1)
        check_out = _PINNED + timedelta(days=4)
        next_check_in = check_out + timedelta(days=2)
        next_check_out = next_check_in + timedelta(days=3)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_in,
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=next_check_in,
            check_out=next_check_out,
        )

        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )

        assert result.skipped_reason is None
        assert len(result.per_rule) == 1
        outcome = result.per_rule[0]
        assert outcome.decision == "materialised"
        assert outcome.port_outcome == "created"
        assert outcome.rule_id == DEFAULT_AFTER_CHECKOUT_RULE_ID

        assert len(port.calls) == 1
        request = port.calls[0]
        assert request.reservation_id == rid
        assert request.rule_id == DEFAULT_AFTER_CHECKOUT_RULE_ID
        assert request.property_id == prop
        assert request.unit_id is None
        # ``starts_at`` anchors at the reservation's check-out.
        assert request.starts_at == check_out
        # ``ends_at`` is min(check_out + duration, next_check_in).
        # The 2-day gap is wider than the 2-hour duration, so the
        # nominal end wins.
        assert request.ends_at == check_out + DEFAULT_TURNOVER_DURATION

    def test_property_local_pinning_irrelevant_for_storage(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """Stored times stay UTC; the rule's wall-clock duration does not drift.

        The turnover_generator deliberately works in UTC end-to-end;
        property-local rendering belongs to the §06 ``scheduled_for_
        local`` projection inside the tasks adapter (cd-4qr Phase 5).
        Asserting here that the request carries aware-UTC instants
        pins the contract so a future drift surfaces loudly.
        """
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        port = RecordingTasksCreateOccurrencePort()
        handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        request = port.calls[0]
        assert request.starts_at.tzinfo == UTC
        assert request.ends_at.tzinfo == UTC


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


class TestSkipPaths:
    """The four orthogonal skip conditions, plus the two pre-rule short-circuits."""

    def test_event_cancelled_short_circuits(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        # No bootstrap: the cancelled-event branch must short-
        # circuit before any DB read.
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(
                ctx=ctx,
                reservation_id="01HWA00000000000000000RES1",
                change_kind="cancelled",
            ),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert result.skipped_reason == "event_cancelled"
        assert result.per_rule == ()
        assert port.calls == []

    def test_reservation_missing_short_circuits(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id="01HWA00000000000000000RES2"),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert result.skipped_reason == "reservation_missing"
        assert port.calls == []

    def test_cancelled_row_skips_even_on_non_cancelled_event(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """Defence in depth: a cancelled row must not fire turnover.

        Covers the publisher discriminator vs. row-state drift case:
        if some other flow flips the row to ``cancelled`` and an
        ``updated`` event re-fires (manual edit, replay), the handler
        must still skip.
        """
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
            status="cancelled",
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )
        port = RecordingTasksCreateOccurrencePort()
        # Note: change_kind="updated" — *not* "cancelled". The row
        # status is what triggers the skip.
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid, change_kind="updated"),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert result.skipped_reason == "reservation_cancelled"
        assert result.per_rule == ()
        assert port.calls == []

    def test_owner_kind_skips_default_rule(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        port = RecordingTasksCreateOccurrencePort()
        owner_resolver = StaticReservationContextResolver(
            unit_id=None, guest_kind="owner"
        )
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            resolver=owner_resolver,
            now=_PINNED,
        )

        assert len(result.per_rule) == 1
        assert result.per_rule[0].decision == "skipped_guest_kind"
        assert port.calls == []

    def test_no_next_stay_skips(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=_PINNED + timedelta(days=4),
        )
        # Deliberately no second reservation.
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert len(result.per_rule) == 1
        assert result.per_rule[0].decision == "skipped_no_next_stay"
        assert port.calls == []

    def test_zero_gap_skips(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        # Same-day check-in: zero gap.
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out,
            check_out=check_out + timedelta(days=2),
        )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert len(result.per_rule) == 1
        assert result.per_rule[0].decision == "skipped_zero_gap"
        assert port.calls == []

    def test_closure_intersecting_gap_skips(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        next_check_in = check_out + timedelta(days=3)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=next_check_in,
            check_out=next_check_in + timedelta(days=2),
        )
        # Closure that fully spans the rule's nominal turnover
        # window (check_out → check_out + 2h).
        _bootstrap_closure(
            session_turnover,
            property_id=prop,
            starts_at=check_out - timedelta(hours=1),
            ends_at=check_out + timedelta(hours=4),
        )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert len(result.per_rule) == 1
        assert result.per_rule[0].decision == "skipped_closure"
        assert port.calls == []

    def test_closure_exactly_outside_gap_does_not_skip(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """Half-open interval: a closure ending at ``starts_at`` does not intersect."""
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        next_check_in = check_out + timedelta(days=3)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=next_check_in,
            check_out=next_check_in + timedelta(days=2),
        )
        # Closure ends exactly at check_out: intersection condition
        # is ``closure.ends_at > starts_at`` (strict), so a closure
        # that ends *at* the check-out does NOT intersect the gap.
        _bootstrap_closure(
            session_turnover,
            property_id=prop,
            starts_at=check_out - timedelta(hours=24),
            ends_at=check_out,
        )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert result.per_rule[0].decision == "materialised"


# ---------------------------------------------------------------------------
# Idempotency / patch / regenerate
# ---------------------------------------------------------------------------


class TestIdempotency:
    """The three port-side outcomes for repeat firings."""

    def test_re_firing_is_noop(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )
        port = RecordingTasksCreateOccurrencePort()
        event = _make_event(ctx=ctx, reservation_id=rid)
        first = handle_reservation_upserted(
            event,
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        second = handle_reservation_upserted(
            event,
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )

        assert first.per_rule[0].port_outcome == "created"
        assert second.per_rule[0].port_outcome == "noop"
        # Two calls landed on the recorder, but only the first
        # produced a side-effect.
        assert len(port.calls) == 2
        assert port.calls[0] == port.calls[1]

    def test_short_shift_patches_in_place(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        original_check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=original_check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=original_check_out + timedelta(days=1),
            check_out=original_check_out + timedelta(days=4),
        )

        port = RecordingTasksCreateOccurrencePort()
        first = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert first.per_rule[0].port_outcome == "created"
        first_occ_id = first.per_rule[0].occurrence_id

        # Shift by 2h — under the 4h threshold.
        new_check_out = original_check_out + timedelta(hours=2)
        reservation = session_turnover.get(Reservation, rid)
        assert reservation is not None
        reservation.check_out = new_check_out
        session_turnover.flush()

        second = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert second.per_rule[0].port_outcome == "patched"
        # Patches share the same occurrence id (no regeneration).
        assert second.per_rule[0].occurrence_id == first_occ_id
        assert port.calls[1].starts_at == new_check_out

    def test_long_shift_regenerates(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        original_check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=original_check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=original_check_out + timedelta(days=2),
            check_out=original_check_out + timedelta(days=5),
        )

        port = RecordingTasksCreateOccurrencePort()
        first = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        first_occ_id = first.per_rule[0].occurrence_id

        # Shift by 6h — over the 4h threshold.
        reservation = session_turnover.get(Reservation, rid)
        assert reservation is not None
        reservation.check_out = original_check_out + timedelta(hours=6)
        session_turnover.flush()

        second = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert second.per_rule[0].port_outcome == "regenerated"
        assert second.per_rule[0].occurrence_id != first_occ_id

    def test_exact_threshold_regenerates(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """Boundary: a shift of exactly 4h regenerates, per § "< 4h" wording.

        The spec phrases it as "< 4h reschedule patches in place;
        >= 4h regenerates". Pin the boundary case so a future tweak
        to the comparator cannot silently flip the semantic.
        """
        prop = _bootstrap_property(session_turnover)
        original_check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=original_check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=original_check_out + timedelta(days=2),
            check_out=original_check_out + timedelta(days=5),
        )

        port = RecordingTasksCreateOccurrencePort()
        first = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        first_occ_id = first.per_rule[0].occurrence_id

        # Shift by exactly 4h — the threshold is "< 4h patches",
        # so this lands on the regenerate side of the boundary.
        reservation = session_turnover.get(Reservation, rid)
        assert reservation is not None
        reservation.check_out = original_check_out + DEFAULT_PATCH_IN_PLACE_THRESHOLD
        session_turnover.flush()

        second = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert second.per_rule[0].port_outcome == "regenerated"
        assert second.per_rule[0].occurrence_id != first_occ_id

    def test_just_under_threshold_patches(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """Boundary: 4h - 1 microsecond patches in place."""
        prop = _bootstrap_property(session_turnover)
        original_check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=original_check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=original_check_out + timedelta(days=2),
            check_out=original_check_out + timedelta(days=5),
        )

        port = RecordingTasksCreateOccurrencePort()
        first = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        first_occ_id = first.per_rule[0].occurrence_id

        # Shift by just under 4h — strictly less than threshold.
        reservation = session_turnover.get(Reservation, rid)
        assert reservation is not None
        reservation.check_out = (
            original_check_out
            + DEFAULT_PATCH_IN_PLACE_THRESHOLD
            - timedelta(microseconds=1)
        )
        session_turnover.flush()

        second = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert second.per_rule[0].port_outcome == "patched"
        assert second.per_rule[0].occurrence_id == first_occ_id


# ---------------------------------------------------------------------------
# Same-day turnover time-boxing
# ---------------------------------------------------------------------------


class TestSameDayTurnover:
    """§04 "Same-day turnovers": time-box the bundle to the gap."""

    def test_short_gap_clips_ends_at_to_next_check_in(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        # 1-hour gap → less than the 2h default duration.
        next_check_in = check_out + timedelta(hours=1)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=next_check_in,
            check_out=next_check_in + timedelta(days=2),
        )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        assert result.per_rule[0].decision == "materialised"
        request = port.calls[0]
        assert request.starts_at == check_out
        assert request.ends_at == next_check_in
        assert request.ends_at - request.starts_at == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


class TestWorkspaceIsolation:
    """Reservations + closures from a sibling workspace must not influence the rule."""

    def test_sibling_workspace_reservation_is_not_next_stay(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        # Sibling workspace owns its own next-stay row at the same
        # property (multi-belonging via property_workspace, §02).
        # The query MUST NOT see it: the reservation table is
        # workspace-scoped via the ORM tenant filter.
        sibling_ws = _bootstrap_workspace(session_turnover, slug="ws-sibling")
        # Tenant-agnostic insert is fine here (we're seeding); the
        # tenant filter only applies to SELECT/UPDATE/DELETE.
        from app.tenancy import tenant_agnostic

        with tenant_agnostic():  # justification: cross-tenant test seed
            _bootstrap_reservation(
                session_turnover,
                workspace_id=sibling_ws,
                property_id=prop,
                check_in=check_out + timedelta(hours=1),
                check_out=check_out + timedelta(days=2),
            )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        # No next stay visible inside the workspace → skip.
        assert result.per_rule[0].decision == "skipped_no_next_stay"


# ---------------------------------------------------------------------------
# Cancelled-status next stay
# ---------------------------------------------------------------------------


class TestCancelledNextStay:
    """A future-cancelled reservation does NOT bound the turnover gap."""

    def test_cancelled_next_stay_is_invisible_to_lookup(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        # A cancelled future reservation should NOT bound the gap.
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(hours=1),
            check_out=check_out + timedelta(days=2),
            status="cancelled",
        )
        # …whereas a real follow-on stay further out *should*.
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=2),
            check_out=check_out + timedelta(days=5),
        )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            now=_PINNED,
        )
        # The further-out non-cancelled stay bounds the gap, leaving
        # plenty of room for the 2h default turnover.
        assert result.per_rule[0].decision == "materialised"
        assert (
            port.calls[0].ends_at - port.calls[0].starts_at == DEFAULT_TURNOVER_DURATION
        )


# ---------------------------------------------------------------------------
# Subscription registration
# ---------------------------------------------------------------------------


class TestRegisterSubscriptions:
    """Idempotent registration; no implicit subscriptions at import time."""

    def test_register_then_publish_invokes_handler(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _reset_subscriptions_for_tests()
        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            _ = event
            return session_turnover, ctx

        register_subscriptions(bus, port=port, session_provider=session_provider)
        bus.publish(_make_event(ctx=ctx, reservation_id=rid))
        assert len(port.calls) == 1

    def test_register_is_idempotent(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _reset_subscriptions_for_tests()
        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            _ = event
            return session_turnover, ctx

        register_subscriptions(bus, port=port, session_provider=session_provider)
        register_subscriptions(bus, port=port, session_provider=session_provider)
        bus.publish(_make_event(ctx=ctx, reservation_id=rid))
        # One handler fired once even though we registered twice.
        assert len(port.calls) == 1

    def test_session_provider_returning_none_skips_handler(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _reset_subscriptions_for_tests()
        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()
        provider_calls: list[ReservationUpserted] = []

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            provider_calls.append(event)
            return None

        register_subscriptions(bus, port=port, session_provider=session_provider)
        bus.publish(_make_event(ctx=ctx, reservation_id="01HWA00000000000000000RES3"))
        assert len(provider_calls) == 1
        assert port.calls == []

    def test_register_subscriptions_propagates_custom_rules_and_resolver(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """The closure must thread custom ``rules`` + ``resolver`` end-to-end.

        Otherwise a future drift could silently bind the bus
        subscription to :data:`DEFAULT_RULES` and the default
        resolver, ignoring caller overrides — undetectable until a
        production rule diverged from the default.
        """
        _reset_subscriptions_for_tests()
        bus = EventBus()
        port = RecordingTasksCreateOccurrencePort()
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        def session_provider(
            event: ReservationUpserted,
        ) -> tuple[Session, WorkspaceContext] | None:
            _ = event
            return session_turnover, ctx

        # Custom rule the default set does NOT contain.
        custom_rule = TurnoverRule(
            id="rule-custom-deep-clean",
            trigger="after_checkout",
            duration=timedelta(minutes=45),
            guest_kind_filter=("guest", "staff", "other"),
        )
        # Resolver that pins a sentinel unit_id so the request shape
        # carries it through — proves the resolver argument was used,
        # not the default.
        custom_resolver = StaticReservationContextResolver(
            unit_id="01HWAUNIT0000000000SENTNL", guest_kind="guest"
        )
        register_subscriptions(
            bus,
            port=port,
            session_provider=session_provider,
            resolver=custom_resolver,
            rules=(custom_rule,),
        )
        bus.publish(_make_event(ctx=ctx, reservation_id=rid))
        assert len(port.calls) == 1
        request = port.calls[0]
        assert request.rule_id == "rule-custom-deep-clean"
        assert request.unit_id == "01HWAUNIT0000000000SENTNL"
        # And the rule's duration drove the window (gap is wide
        # enough that the rule's nominal end wins over the next
        # check-in).
        assert request.ends_at - request.starts_at == timedelta(minutes=45)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """The handler validates its own preconditions loudly."""

    def test_naive_now_rejected(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        port = NoopTasksCreateOccurrencePort()
        with pytest.raises(ValueError, match="UTC"):
            handle_reservation_upserted(
                _make_event(ctx=ctx, reservation_id="01HWA00000000000000000RES1"),
                session=session_turnover,
                ctx=ctx,
                port=port,
                now=datetime(2026, 4, 26, 12, 0, 0),  # naive
            )

    def test_aware_non_utc_now_rejected(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        """A timezone-aware ``now`` with a non-zero offset must also be rejected.

        Spec invariant: time at rest is UTC. Accepting a ``+05:00``
        ``now`` would silently land non-UTC instants on downstream
        comparisons against ``check_in`` / ``check_out`` (stored as
        UTC) and on the port request shape.
        """
        port = NoopTasksCreateOccurrencePort()
        plus_five = timezone(timedelta(hours=5))
        with pytest.raises(ValueError, match="UTC"):
            handle_reservation_upserted(
                _make_event(ctx=ctx, reservation_id="01HWA00000000000000000RES1"),
                session=session_turnover,
                ctx=ctx,
                port=port,
                now=datetime(2026, 4, 26, 12, 0, 0, tzinfo=plus_five),
            )


# ---------------------------------------------------------------------------
# Default rule shape (regression guard)
# ---------------------------------------------------------------------------


def test_default_rule_excludes_owner_kind() -> None:
    """The default ``after_checkout`` rule must keep owner-kind out."""
    rule = next(r for r in DEFAULT_RULES if r.id == DEFAULT_AFTER_CHECKOUT_RULE_ID)
    assert "owner" not in rule.guest_kind_filter
    assert set(rule.guest_kind_filter) == {"guest", "staff", "other"}


def test_default_patch_in_place_threshold_is_four_hours() -> None:
    """Pin the §04 "Edit semantics" 4-hour threshold."""
    threshold = DEFAULT_PATCH_IN_PLACE_THRESHOLD
    assert threshold == timedelta(hours=4)


# ---------------------------------------------------------------------------
# Custom rule wiring
# ---------------------------------------------------------------------------


class TestCustomRules:
    """A test-supplied rule set drives the per-rule loop without monkey-patching."""

    def test_multiple_rules_evaluated_independently(
        self,
        session_turnover: Session,
        ctx: WorkspaceContext,
    ) -> None:
        prop = _bootstrap_property(session_turnover)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        _bootstrap_reservation(
            session_turnover,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=2),
            check_out=check_out + timedelta(days=5),
        )

        rules = (
            TurnoverRule(
                id="rule-fast-clean",
                trigger="after_checkout",
                duration=timedelta(minutes=60),
                guest_kind_filter=("guest", "staff", "other"),
            ),
            TurnoverRule(
                id="rule-deep-clean",
                trigger="after_checkout",
                duration=timedelta(minutes=240),
                guest_kind_filter=("guest", "staff", "other"),
            ),
            TurnoverRule(
                id="rule-owner-walkthrough",
                trigger="after_checkout",
                duration=timedelta(minutes=30),
                guest_kind_filter=("owner",),
            ),
        )
        port = RecordingTasksCreateOccurrencePort()
        result = handle_reservation_upserted(
            _make_event(ctx=ctx, reservation_id=rid),
            session=session_turnover,
            ctx=ctx,
            port=port,
            rules=rules,
            now=_PINNED,
        )
        assert len(result.per_rule) == 3
        # First two rules fire; the owner-only rule skips.
        assert result.per_rule[0].decision == "materialised"
        assert result.per_rule[0].rule_id == "rule-fast-clean"
        assert result.per_rule[1].decision == "materialised"
        assert result.per_rule[1].rule_id == "rule-deep-clean"
        assert result.per_rule[2].decision == "skipped_guest_kind"
        # Two recorder calls (the owner rule never reached the port).
        assert len(port.calls) == 2
        assert {c.rule_id for c in port.calls} == {
            "rule-fast-clean",
            "rule-deep-clean",
        }


# ---------------------------------------------------------------------------
# Reference NoopPort coverage
# ---------------------------------------------------------------------------


class TestNoopPort:
    """The reference no-op port returns the documented shape."""

    def test_noop_port_returns_noop_outcome(self) -> None:
        port = NoopTasksCreateOccurrencePort()
        result = port.create_or_patch_turnover_occurrence(
            session=None,  # type: ignore[arg-type]
            ctx=None,  # type: ignore[arg-type]
            request=TurnoverOccurrenceRequest(
                reservation_id="r",
                rule_id="r",
                property_id="p",
                unit_id=None,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(hours=1),
            ),
            now=_PINNED,
        )
        assert isinstance(result, TurnoverOccurrenceResult)
        assert result.occurrence_id is None
        assert result.outcome == "noop"


# ---------------------------------------------------------------------------
# Static resolver shape
# ---------------------------------------------------------------------------


class TestStaticResolver:
    """The default resolver returns the v1 fallback shape regardless of input."""

    @pytest.mark.parametrize("guest_kind", ["owner", "guest", "staff", "other"])
    def test_static_resolver_echoes_constructor_value(
        self, guest_kind: GuestKind
    ) -> None:
        resolver = StaticReservationContextResolver(
            unit_id="01HWAUNIT0000000000000000Z", guest_kind=guest_kind
        )
        # The resolver does not need a real reservation to project
        # its static defaults; ``None`` is fine since the body does
        # not dereference the argument.
        result = resolver.resolve(
            session=None,  # type: ignore[arg-type]
            ctx=None,  # type: ignore[arg-type]
            reservation=None,  # type: ignore[arg-type]
        )
        assert isinstance(result, ReservationContext)
        assert result.guest_kind == guest_kind
        assert result.unit_id == "01HWAUNIT0000000000000000Z"
