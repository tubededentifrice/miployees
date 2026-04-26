"""Auto-generate turnover task occurrences from reservation events (cd-4qr).

Subscribes to :class:`~app.events.types.ReservationUpserted` on the
process event bus and, for every covered reservation, decides whether
a turnover :class:`~app.adapters.db.tasks.models.Occurrence` should
exist in the gap between the reservation's check-out and the next
reservation's check-in on the **same unit** (or property when no
unit-mapping exists yet — the v1 ``reservation`` slice carries no
``unit_id`` column; that lands with cd-1ai). The actual write is
delegated through the
:class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`
inter-context seam: the tasks-side service that owns occurrence
persistence lands in Phase 5 (``p5.tasks.*``); until then the port's
no-op double satisfies the contract and lets this generator ship.

**Default rule set (in-memory, until §06 ``stay_lifecycle_rule``
table lands).** One ``after_checkout`` rule, 120 minutes long,
firing for every guest_kind except ``owner`` — the §04 default
seed for ``vacation`` / ``str`` properties. The rule shape mirrors
the spec's `stay_lifecycle_rule` columns so the eventual table
ports cleanly. Tests can override the rule list to cover edge
cases.

**Skip conditions, in order:**

1. The event's ``change_kind`` is ``"cancelled"`` — never
   materialise turnover for a cancelled booking.
2. The reservation row is gone (was deleted between the publish
   and the handler firing).
3. The reservation row's ``status`` is ``"cancelled"``. Defence in
   depth against the publisher's discriminator and the row state
   drifting (a manual flow that re-fires an ``updated`` event
   after the row has already been cancelled would otherwise leak
   through skip condition #1).
4. The rule's ``guest_kind_filter`` excludes the reservation's
   :attr:`guest_kind`. Today the v1 ``reservation`` row carries
   no :attr:`guest_kind` column; the
   :class:`ReservationContextResolver` defaults it to ``"guest"``.
   When the column lands, the production resolver simply reads
   it directly.
5. Any :class:`~app.adapters.db.places.models.PropertyClosure`
   intersects the gap window. The whole rule for this reservation
   skips — even one closure inside the window is a "do not
   schedule" signal. §04 "Airbnb-style edge cases" + §06 closure
   suppression.
6. The gap is zero or negative (back-to-back checkouts, manual
   data with check-out > next-check-in, same-day flip on the same
   unit). Nothing to schedule; spec §"Same-day turnovers"
   time-boxes to the gap, and a zero gap is "no turnover today".

When none of those skip — the handler hands the request to the
:class:`TasksCreateOccurrencePort` and trusts the adapter to do
the (reservation_id, rule_id) lookup + create-or-patch state
machine. Re-firing the same event is a no-op via the port's
idempotency contract.

**Subscription discipline.** Per repo `AGENTS.md` /
`.claude/agents/coder.md` and the cd-4qr instructions, this module
does **NOT** subscribe at import time. Production wiring calls
:func:`register_subscriptions` once at startup with the live bus,
ports, and a session factory; tests drive
:func:`handle_reservation_upserted` directly with synthesised
inputs.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
+ §"Airbnb-style edge cases", ``docs/specs/02-domain-model.md``
§"reservation" / §"property_closure" / §"occurrence", and
``docs/specs/01-architecture.md`` §"Boundary rules" rule 4.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import PropertyClosure
from app.adapters.db.stays.models import Reservation
from app.events.bus import EventBus
from app.events.types import ReservationUpserted
from app.ports.tasks_create_occurrence import (
    DEFAULT_PATCH_IN_PLACE_THRESHOLD,
    TasksCreateOccurrencePort,
    TurnoverOccurrenceRequest,
    TurnoverOccurrenceResult,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "DEFAULT_AFTER_CHECKOUT_RULE_ID",
    "DEFAULT_RULES",
    "DEFAULT_TURNOVER_DURATION",
    "GuestKind",
    "HandleResult",
    "ReservationContextResolver",
    "SessionContextProvider",
    "StaticReservationContextResolver",
    "TurnoverRule",
    "default_resolver",
    "handle_reservation_upserted",
    "register_subscriptions",
]


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed enums + defaults
# ---------------------------------------------------------------------------


# §04 ``stay.guest_kind`` column. Mirrored here as a Literal so the
# rule + resolver can type ``guest_kind_filter`` precisely without
# requiring the (yet-unbuilt) §06 ``stay_lifecycle_rule`` table to
# define the alphabet.
GuestKind = Literal["owner", "guest", "staff", "other"]


# §04 "Stay task bundles" — the default seed rule the §"`kind`
# semantics" table prescribes for ``vacation`` / ``str`` properties.
# Pinned id so audit / port idempotency keys stay stable across
# process restarts.
DEFAULT_AFTER_CHECKOUT_RULE_ID: Final[str] = "rule_default_after_checkout"


# Conservative default for the turnover task's wall-clock duration.
# Matches the median Airbnb host-supplied "turnover time" we see in
# the wild; managers will tune via the rule editor once cd-1ai
# lands the rule CRUD.
DEFAULT_TURNOVER_DURATION: Final[timedelta] = timedelta(minutes=120)


@dataclass(frozen=True, slots=True)
class TurnoverRule:
    """An in-memory ``stay_lifecycle_rule``-shaped value.

    Mirrors the §06 ``stay_lifecycle_rule`` row that will land with
    cd-1ai. Frozen so a misbehaving caller can't mutate the shared
    :data:`DEFAULT_RULES` tuple.

    * ``id`` — stable identifier the
      :class:`TasksCreateOccurrencePort` keys idempotency on. When
      the table lands the value moves to a real FK.
    * ``trigger`` — closed enum mirroring the spec; today the
      generator only handles ``"after_checkout"`` (the spec's
      §"Stay task bundles" enumerates ``before_checkin`` and
      ``during_stay`` too — those need a separate trigger
      pipeline + reservation-state listener and are tracked as
      cd-sdo follow-up).
    * ``duration`` — wall-clock duration of the materialised
      occurrence.
    * ``guest_kind_filter`` — frozen tuple of guest kinds the rule
      fires for. A reservation whose kind is **not** in the filter
      skips the rule. The default tuple excludes ``owner``
      explicitly (§04 "Airbnb-style edge cases" + the §"`kind`
      semantics" table for ``mixed`` properties).
    """

    id: str
    trigger: Literal["after_checkout"]
    duration: timedelta
    # Frozen tuple so dataclass equality + hashing work and we can
    # share a single instance across rules without aliasing.
    guest_kind_filter: tuple[GuestKind, ...]


DEFAULT_RULES: Final[tuple[TurnoverRule, ...]] = (
    TurnoverRule(
        id=DEFAULT_AFTER_CHECKOUT_RULE_ID,
        trigger="after_checkout",
        duration=DEFAULT_TURNOVER_DURATION,
        # §04 default: every guest kind except ``owner``. An owner
        # stay (Airbnb host blocking their own villa for personal
        # use) shouldn't trigger a paid turnover crew automatically.
        guest_kind_filter=("guest", "staff", "other"),
    ),
)


# ---------------------------------------------------------------------------
# Reservation context resolver — bridges the v1 → §04 schema gap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReservationContext:
    """Enriched view the resolver returns for one :class:`Reservation`.

    The v1 ``reservation`` table carries neither ``unit_id`` nor
    ``guest_kind`` (cd-1ai widens the slice). The resolver bridges
    that gap: production wires a real adapter once the columns
    land; tests inject a fake.

    ``unit_id`` is ``None`` until the schema gains the column —
    the generator falls back to property-level matching for the
    "next stay" lookup in that case (any reservation at the same
    property is a candidate, since we cannot disambiguate units).
    """

    unit_id: str | None
    guest_kind: GuestKind


class ReservationContextResolver(Protocol):
    """Look up the §04 fields the v1 ``reservation`` slice doesn't carry yet.

    Production wires this against ``Reservation.unit_id`` /
    ``Reservation.guest_kind`` once cd-1ai lands those columns;
    tests inject a stub that returns whatever shape they need.

    Protocol is deliberately not ``runtime_checkable``; structural
    compatibility is checked statically by mypy.
    """

    def resolve(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        reservation: Reservation,
    ) -> ReservationContext:
        """Return the enriched context for ``reservation``.

        Implementations MUST honour ``ctx.workspace_id`` — the
        reservation row is already workspace-scoped via the ORM
        tenant filter, but a defensive resolver re-asserts so a
        misconfigured filter cannot leak rows across tenants.
        """
        ...


@dataclass(frozen=True, slots=True)
class StaticReservationContextResolver:
    """Default resolver: returns the same shape for every reservation.

    Used as the production default until cd-1ai widens the
    ``reservation`` slice with ``unit_id`` and ``guest_kind``
    columns. Returns:

    * ``unit_id = None`` — no unit mapping; the generator falls
      back to property-level matching for the "next stay" lookup.
    * ``guest_kind = "guest"`` — the §04 default for stays without
      an explicit kind. Owner stays never reach the bus today
      (manual API + manual entry both default to ``"guest"`` in
      v1); when they do, the production resolver flips this.

    Tests that need to drive the owner-skip / unit-pinning paths
    instantiate this dataclass with custom defaults or use a fake
    that maps per reservation id.
    """

    unit_id: str | None = None
    guest_kind: GuestKind = "guest"

    def resolve(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        reservation: Reservation,
    ) -> ReservationContext:
        # ``session`` / ``ctx`` / ``reservation`` deliberately
        # unused — the static resolver does not need them. Reference
        # them once so the Protocol signature is satisfied without
        # silencing the unused-arg lint.
        _ = session, ctx, reservation
        return ReservationContext(unit_id=self.unit_id, guest_kind=self.guest_kind)


def default_resolver() -> ReservationContextResolver:
    """Return the production-default :class:`StaticReservationContextResolver`.

    Centralised so a future production resolver swap (cd-1ai+) only
    has to update this factory rather than every call site that
    today instantiates :class:`StaticReservationContextResolver`
    directly.
    """
    return StaticReservationContextResolver()


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """Per-rule decision the handler made.

    * ``rule_id`` — pinned :class:`TurnoverRule.id`.
    * ``decision`` — closed enum:
      ``"materialised"`` (the port was called and produced a
      side-effect; ``port_outcome`` carries the verb), or one of
      the skip codes — ``"skipped_guest_kind"``,
      ``"skipped_no_next_stay"``, ``"skipped_zero_gap"``,
      ``"skipped_closure"``.
    * ``port_outcome`` — the
      :data:`~app.ports.tasks_create_occurrence.TasksCreateOccurrenceOutcome`
      the port returned. ``None`` when the rule was skipped before
      reaching the port.
    * ``occurrence_id`` — the port's identifier when an occurrence
      was created or patched; ``None`` for skips and pure no-ops
      where the port had nothing pre-existing.
    """

    rule_id: str
    decision: Literal[
        "materialised",
        "skipped_guest_kind",
        "skipped_no_next_stay",
        "skipped_zero_gap",
        "skipped_closure",
    ]
    port_outcome: str | None = None
    occurrence_id: str | None = None


@dataclass(frozen=True, slots=True)
class HandleResult:
    """What :func:`handle_reservation_upserted` did for one event.

    Carries the per-rule decision so callers (tests, the future
    audit shape, structured-log dashboards) can verify behaviour
    without re-reading the DB.

    * ``reservation_id`` — the event's reservation id, mirrored back
      so log lines can pair the result with the trigger.
    * ``skipped_reason`` — populated when the whole event was
      skipped before any rule was evaluated. ``None`` when at
      least one rule was considered.
    * ``per_rule`` — one entry per rule the generator evaluated,
      in :data:`DEFAULT_RULES` order.
    """

    reservation_id: str
    skipped_reason: str | None
    per_rule: tuple[RuleOutcome, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public handler
# ---------------------------------------------------------------------------


def handle_reservation_upserted(
    event: ReservationUpserted,
    *,
    session: Session,
    ctx: WorkspaceContext,
    port: TasksCreateOccurrencePort,
    resolver: ReservationContextResolver | None = None,
    rules: Sequence[TurnoverRule] = DEFAULT_RULES,
    now: datetime | None = None,
) -> HandleResult:
    """React to one ``reservation.upserted`` event.

    Public for tests; the production subscriber wraps this in a
    closure that supplies ``session`` / ``ctx`` / ``port`` from
    the active Unit of Work.

    The function is **side-effect light by design**: every write
    travels through the
    :class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`
    so this module never touches ``occurrence`` rows directly.
    Reads (``Reservation``, ``PropertyClosure``) are workspace-
    scoped by the ORM tenant filter (the
    :mod:`app.adapters.db.stays` package registers ``reservation``
    as a scoped table).

    Raises nothing on the skip paths — every skip is reflected in
    :class:`HandleResult.skipped_reason` or :class:`RuleOutcome.
    decision`. The publisher's UoW continues unaffected.
    """
    resolved_resolver = resolver if resolver is not None else default_resolver()
    resolved_now = now if now is not None else datetime.now(UTC)
    if resolved_now.tzinfo is None or resolved_now.utcoffset() != timedelta(0):
        # Same invariant the rest of the codebase carries: time at
        # rest is UTC. A naive "now" would land in event audit /
        # port requests with a silent timezone, breaking the
        # comparison-against-``check_in`` / ``check_out`` predicates
        # that the closure / next-stay queries depend on.
        raise ValueError("now must be a timezone-aware datetime in UTC")

    if event.change_kind == "cancelled":
        # A cancelled reservation never gets a fresh turnover. The
        # matching ``occurrence`` (if any) is the tasks-side
        # cancellation cascade's job (cd-sdo follow-up); this
        # handler must not silently overwrite a cancelled stay's
        # historical row.
        return HandleResult(
            reservation_id=event.reservation_id,
            skipped_reason="event_cancelled",
        )

    reservation = _load_reservation(session, ctx, reservation_id=event.reservation_id)
    if reservation is None:
        # Reservation deleted between publish and handler. Race;
        # not an error — the audit row for the delete already
        # captured the change.
        _log.info(
            "stays.turnover_generator.reservation_missing",
            extra={
                "event": "stays.turnover_generator.reservation_missing",
                "reservation_id": event.reservation_id,
                "workspace_id": ctx.workspace_id,
            },
        )
        return HandleResult(
            reservation_id=event.reservation_id,
            skipped_reason="reservation_missing",
        )

    if reservation.status == "cancelled":
        # Defence in depth against the publisher's discriminator and
        # the row state drifting: a manual flow that re-fires an
        # ``updated`` event after the row has already been cancelled
        # would otherwise leak through the ``event.change_kind``
        # check above. The cancellation cascade (cd-sdo follow-up)
        # owns sweeping any pre-existing occurrence.
        return HandleResult(
            reservation_id=event.reservation_id,
            skipped_reason="reservation_cancelled",
        )

    enriched = resolved_resolver.resolve(session, ctx, reservation=reservation)

    per_rule: list[RuleOutcome] = []
    for rule in rules:
        outcome = _evaluate_rule(
            session,
            ctx,
            rule=rule,
            reservation=reservation,
            enriched=enriched,
            port=port,
            now=resolved_now,
        )
        per_rule.append(outcome)

    return HandleResult(
        reservation_id=event.reservation_id,
        skipped_reason=None,
        per_rule=tuple(per_rule),
    )


# ---------------------------------------------------------------------------
# Subscription wiring (explicit; never at import time)
# ---------------------------------------------------------------------------


# A subscriber needs the open ``Session`` + ``WorkspaceContext`` for
# the publisher's request. The bus carries only the event payload, so
# production wiring depends on the bootstrap closing over a callable
# that yields the active session + context for the in-flight event.
# The tenancy middleware sets a context-var per request (see
# :func:`app.tenancy.set_current`); a sibling carrier for the active
# session (or a request-scoped DI container) is the realistic shape
# of this provider once cd-sdo wires production.
SessionContextProvider = Callable[
    [ReservationUpserted], tuple[Session, WorkspaceContext] | None
]


# Module-level dedup so a test that re-subscribes the same bus
# doesn't double-fire the handler. Mirrors the
# ``app.domain.llm.router._SUBSCRIBED_BUSES`` pattern.
_SUBSCRIBED_BUSES: set[int] = set()
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


def register_subscriptions(
    event_bus: EventBus,
    *,
    port: TasksCreateOccurrencePort,
    session_provider: SessionContextProvider,
    resolver: ReservationContextResolver | None = None,
    rules: Sequence[TurnoverRule] = DEFAULT_RULES,
) -> None:
    """Wire the turnover handler onto ``event_bus``.

    Called **once** by the application bootstrap (not at module
    import). Idempotent: re-subscribing the same bus is a no-op so
    a test re-run that flips the bus back to "empty" can re-call
    this without double-firing the handler. The production caller
    holds the
    :class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`
    concretion the tasks-side service exposes; tests pass
    :class:`~app.ports.tasks_create_occurrence.NoopTasksCreateOccurrencePort`
    or a recorder.

    ``session_provider`` returns the open
    :class:`~sqlalchemy.orm.Session` + :class:`WorkspaceContext`
    for the in-flight event; the publisher's UoW supplies these.
    Returning ``None`` is the "cannot service this event right now"
    escape hatch — the handler logs and exits cleanly. The provider
    is application-bootstrap territory; cd-sdo will wire the real
    one against the request-scoped session carrier.
    """
    bus_id = id(event_bus)
    with _SUBSCRIBED_BUSES_LOCK:
        if bus_id in _SUBSCRIBED_BUSES:
            return
        _SUBSCRIBED_BUSES.add(bus_id)

    resolved_resolver = resolver if resolver is not None else default_resolver()
    rules_tuple = tuple(rules)

    @event_bus.subscribe(ReservationUpserted)
    def _on_reservation_upserted(event: ReservationUpserted) -> None:
        bound = session_provider(event)
        if bound is None:
            _log.info(
                "stays.turnover_generator.no_session_for_event",
                extra={
                    "event": "stays.turnover_generator.no_session_for_event",
                    "reservation_id": event.reservation_id,
                    "workspace_id": event.workspace_id,
                },
            )
            return
        session, ctx = bound
        handle_reservation_upserted(
            event,
            session=session,
            ctx=ctx,
            port=port,
            resolver=resolved_resolver,
            rules=rules_tuple,
        )


def _reset_subscriptions_for_tests() -> None:
    """Clear the subscribed-bus dedup set.

    Production code must not call this. Tests use it after the
    usual ``EventBus._reset_for_tests`` to allow a fresh
    :func:`register_subscriptions` against the same bus instance.
    """
    with _SUBSCRIBED_BUSES_LOCK:
        _SUBSCRIBED_BUSES.clear()


# ---------------------------------------------------------------------------
# Per-rule body
# ---------------------------------------------------------------------------


def _evaluate_rule(
    session: Session,
    ctx: WorkspaceContext,
    *,
    rule: TurnoverRule,
    reservation: Reservation,
    enriched: ReservationContext,
    port: TasksCreateOccurrencePort,
    now: datetime,
) -> RuleOutcome:
    """Decide and execute the single rule against the reservation."""
    if enriched.guest_kind not in rule.guest_kind_filter:
        return RuleOutcome(rule_id=rule.id, decision="skipped_guest_kind")

    next_check_in = _next_stay_check_in(
        session,
        ctx,
        reservation=reservation,
        unit_id=enriched.unit_id,
    )
    if next_check_in is None:
        # No follow-on stay → no gap to materialise into. The §04
        # spec phrases turnover bundles as "between checkouts and
        # next check-in"; without a next check-in there's nothing
        # to bound. Operators tracking "ad-hoc post-checkout work"
        # use a different rule path (the §06 ``one-off`` task
        # surface), not this generator.
        return RuleOutcome(rule_id=rule.id, decision="skipped_no_next_stay")

    check_out = _ensure_utc(reservation.check_out)
    next_check_in_utc = _ensure_utc(next_check_in)

    if next_check_in_utc <= check_out:
        # Zero or negative gap: same-day flip on the same unit (a
        # double-booking the operator must resolve, not a turnover
        # window) or check-out exactly equal to the next check-in.
        # §04 "Same-day turnovers" notes this case explicitly —
        # we time-box to the gap, and a zero gap is "no turnover
        # today".
        return RuleOutcome(rule_id=rule.id, decision="skipped_zero_gap")

    # Time-box the rule's duration to the gap. §04 "Airbnb-style
    # edge cases" §"Same-day turnovers": ``after_checkout`` bundles
    # are time-boxed to the gap when the next check-in lands
    # before the rule's nominal end. The starts_at always anchors
    # at the check-out instant — the spec's "tasks to perform
    # after the guest leaves" reading.
    nominal_end = check_out + rule.duration
    ends_at = min(nominal_end, next_check_in_utc)
    starts_at = check_out

    if _gap_intersects_closure(
        session,
        property_id=reservation.property_id,
        starts_at=starts_at,
        ends_at=ends_at,
    ):
        return RuleOutcome(rule_id=rule.id, decision="skipped_closure")

    request = TurnoverOccurrenceRequest(
        reservation_id=reservation.id,
        rule_id=rule.id,
        property_id=reservation.property_id,
        unit_id=enriched.unit_id,
        starts_at=starts_at,
        ends_at=ends_at,
        patch_in_place_threshold=DEFAULT_PATCH_IN_PLACE_THRESHOLD,
    )
    result: TurnoverOccurrenceResult = port.create_or_patch_turnover_occurrence(
        session,
        ctx,
        request=request,
        now=now,
    )
    return RuleOutcome(
        rule_id=rule.id,
        decision="materialised",
        port_outcome=result.outcome,
        occurrence_id=result.occurrence_id,
    )


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------


def _load_reservation(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
) -> Reservation | None:
    """Workspace-scoped reservation lookup; ``None`` on miss.

    The ORM tenant filter already pins the read to
    ``ctx.workspace_id``; the explicit ``where`` is defence-in-
    depth (a misconfigured filter must fail loud, not silently
    leak rows). Returns ``None`` for unknown ids and for rows
    deleted between the event publish and the handler firing.
    """
    stmt = select(Reservation).where(
        Reservation.id == reservation_id,
        Reservation.workspace_id == ctx.workspace_id,
    )
    return session.scalars(stmt).one_or_none()


def _next_stay_check_in(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation: Reservation,
    unit_id: str | None,
) -> datetime | None:
    """Return the ``check_in`` of the next stay on the same unit (or property).

    "Next" = workspace-scoped reservation at the same property and
    (when the v1 → §04 schema gap is closed) the same unit, with
    ``check_in >= reservation.check_out``, in non-cancelled state,
    ordered by ``check_in`` ascending.

    The v1 ``reservation`` slice has no ``unit_id`` column; when
    ``unit_id`` is ``None`` we fall back to property-level
    matching. The fallback is intentionally conservative — it may
    over-match in a multi-unit property (a stay on Unit A surfaces
    when computing turnover for Unit B's reservation), suppressing
    a turnover that the spec would otherwise allow. Once cd-1ai
    lands the column the production resolver returns the real
    unit_id and the predicate narrows. The conservative fallback
    is the right default: the worst case is a missed turnover the
    operator can manually trigger, vs. the alternative (over-
    materialising turnovers across stays in different units) which
    is harder to diagnose.

    Cancelled reservations are excluded — a future reservation
    that's been cancelled does NOT bound the current turnover's
    window; the §04 spec treats cancellations as "the slot is
    open again".
    """
    stmt = (
        select(Reservation.check_in)
        .where(Reservation.workspace_id == ctx.workspace_id)
        .where(Reservation.property_id == reservation.property_id)
        .where(Reservation.id != reservation.id)
        .where(Reservation.status != "cancelled")
        .where(Reservation.check_in >= reservation.check_out)
        .order_by(Reservation.check_in.asc())
        .limit(1)
    )
    # The :class:`Reservation` ORM has no ``unit_id`` column today;
    # the predicate narrows once cd-1ai widens the slice. Until
    # then ``unit_id`` is consumed only by the future-aware
    # request shape; the lookup itself stays property-scoped.
    _ = unit_id
    row = session.scalars(stmt).first()
    return row


def _gap_intersects_closure(
    session: Session,
    *,
    property_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> bool:
    """Return ``True`` iff any closure overlaps the gap window.

    Two windows ``[a, b)`` and ``[c, d)`` overlap iff
    ``a < d AND c < b``. The half-open convention matches the
    spec's "starts inclusive, ends exclusive" reading and lines
    up with how the iCal poller writes Blocked-pattern closures.

    The ``property_closure`` table is **not** workspace-scoped —
    the table reaches the workspace boundary through its parent
    property's ``property_workspace`` rows (see
    :mod:`app.adapters.db.places`). Filtering by ``property_id``
    is sufficient: the parent property is already pinned to the
    workspace via the reservation's ``property_id`` (which IS
    workspace-scoped), so a closure attached to a property the
    workspace owns through ``property_workspace`` is the only
    closure this query can return.
    """
    stmt = (
        select(PropertyClosure.id)
        .where(PropertyClosure.property_id == property_id)
        .where(PropertyClosure.starts_at < ends_at)
        .where(PropertyClosure.ends_at > starts_at)
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


def _ensure_utc(value: datetime) -> datetime:
    """Restamp UTC tz on a SQLite-loaded naive datetime.

    SQLite drops tzinfo off ``DateTime(timezone=True)`` columns on
    read; Postgres preserves it. The column is always written as
    aware UTC, so a naive read is a UTC value that has lost its
    zone. Mirrors the helper in
    :mod:`app.worker.tasks.poll_ical` and
    :mod:`app.worker.tasks.generator`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
