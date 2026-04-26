"""Cross-context port: stays → tasks "create or patch a turnover occurrence".

The stays context's turnover generator
(:mod:`app.domain.stays.turnover_generator`) reacts to
``reservation.upserted`` events and decides that a turnover
:class:`~app.adapters.db.tasks.models.Occurrence` row should exist
in a particular gap window. The actual persistence belongs to the
tasks context (cd-4qr Phase 5; the real adapter lands as part of
``p5.tasks.*``). Until the tasks-side service exists this Protocol
gives the stays generator a precise contract to call against —
production wiring injects the live concretion in
:mod:`app.main`; tests inject :class:`NoopTasksCreateOccurrencePort`
or a recording fake.

**Idempotency contract.** Implementations MUST treat
``(reservation_id, rule_id)`` as the dedup key. A second call with
the same key and a small (< 4 h) shift in ``starts_at`` /
``ends_at`` MUST patch the existing row in place; a larger shift
MUST regenerate (cancel the existing row + insert a fresh one).
Re-firing with identical inputs MUST be a no-op. The threshold is
fixed by §04 "Stay task bundles" §"Edit semantics"; the port
exposes it on the request so callers can override per-rule once
the tasks-side service grows that knob.

**Why "create or patch" rather than separate methods?** The
caller does not know the existing occurrence's state. Splitting
the surface (``create`` / ``patch`` / ``regenerate``) would force
the caller to either round-trip the read or duplicate the
state-machine across both contexts. One verb keeps the stays-side
generator's branches clean; the implementation does the lookup.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
§"Edit semantics" + §"Airbnb-style edge cases", and
``docs/specs/01-architecture.md`` §"Boundary rules" rule 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from sqlalchemy.orm import Session

from app.tenancy import WorkspaceContext

__all__ = [
    "DEFAULT_PATCH_IN_PLACE_THRESHOLD",
    "NoopTasksCreateOccurrencePort",
    "RecordingTasksCreateOccurrencePort",
    "TasksCreateOccurrenceOutcome",
    "TasksCreateOccurrencePort",
    "TurnoverOccurrenceRequest",
    "TurnoverOccurrenceResult",
]


# §04 "Stay task bundles" §"Edit semantics" — the threshold below
# which a shift in check_out_at patches the bundle's tasks in place
# rather than regenerating. Pinned in code so the port surface
# can't drift away from the spec while the tasks-side service is
# still in design; the tasks adapter is free to widen the per-rule
# override later.
DEFAULT_PATCH_IN_PLACE_THRESHOLD: Final[timedelta] = timedelta(hours=4)


# Closed enum the port returns so the stays generator can branch
# (write an audit row, count the bucket) without re-reading the
# tasks-context row.
TasksCreateOccurrenceOutcome = Literal["created", "patched", "regenerated", "noop"]


@dataclass(frozen=True, slots=True)
class TurnoverOccurrenceRequest:
    """Inputs the stays generator hands the tasks adapter.

    Frozen so the request is a value, not a mutable record — the
    adapter cannot smuggle changes back to the caller through the
    DTO. ``reservation_id`` + ``rule_id`` together form the
    idempotency key; the rest carries the materialisation shape.

    * ``reservation_id`` — the upstream reservation that triggered
      the rule. The tasks adapter persists the linkage so a
      reservation cancellation can sweep the matching occurrence.
    * ``rule_id`` — the (eventually `stay_lifecycle_rule.id`) the
      generator matched. Today the generator carries built-in
      default rule ids (see
      :mod:`app.domain.stays.turnover_generator`); when the §06
      ``stay_lifecycle_rule`` table lands the value moves to a
      real FK.
    * ``property_id`` — denormalised so the tasks adapter does not
      have to re-read the reservation. Required (a turnover
      always anchors at a property).
    * ``unit_id`` — the unit the turnover applies to, or ``None``
      when the reservation pre-dates the unit-mapping work
      (cd-1ai). The adapter persists it onto
      :attr:`Occurrence.unit_id` when set.
    * ``starts_at`` / ``ends_at`` — UTC window the rule decided.
      Both must be timezone-aware UTC; the adapter converts to
      property-local for ``scheduled_for_local`` rendering.
    * ``patch_in_place_threshold`` — overrideable per request,
      defaulting to :data:`DEFAULT_PATCH_IN_PLACE_THRESHOLD`. The
      adapter compares ``|new.starts_at - existing.starts_at|``
      against this when an existing row is found.
    """

    reservation_id: str
    rule_id: str
    property_id: str
    unit_id: str | None
    starts_at: datetime
    ends_at: datetime
    patch_in_place_threshold: timedelta = DEFAULT_PATCH_IN_PLACE_THRESHOLD


@dataclass(frozen=True, slots=True)
class TurnoverOccurrenceResult:
    """What the adapter tells the caller it did.

    * ``occurrence_id`` — the id the caller can use to write its own
      audit row, emit a domain event, or thread into a DTO. ``None``
      when the call was a true no-op (no row exists, no row
      created — happens only on cancelled-reservation upserts that
      precede a real call; today's generator never invokes the
      port in that branch but the type admits it).
    * ``outcome`` — closed enum describing the side-effect, used by
      the stays generator's reporting / audit shape.
    """

    occurrence_id: str | None
    outcome: TasksCreateOccurrenceOutcome


class TasksCreateOccurrencePort(Protocol):
    """Cross-context seam: create / patch / regenerate a turnover occurrence.

    The single method wraps the full state machine the tasks adapter
    runs:

    1. Look up the existing occurrence by
       ``(reservation_id, rule_id)``.
    2. None found → INSERT a new ``occurrence`` row tagged with
       both ids. Return ``"created"``.
    3. Found, request matches existing window exactly → no-op.
       Return ``"noop"`` with the existing occurrence id.
    4. Found, request differs by less than ``patch_in_place_
       threshold`` → patch ``starts_at`` / ``ends_at`` /
       ``scheduled_for_local`` / ``ends_at`` in place (state-gated
       to ``scheduled | pending`` per §04 "Edit semantics"). Return
       ``"patched"``.
    5. Found, request differs by more than the threshold → cancel
       the existing row (``state='cancelled'``,
       ``cancellation_reason='stay rescheduled'``) and INSERT a
       fresh row. Return ``"regenerated"``.

    Implementations MUST be transactional: the lookup + write run
    inside the caller's open ``session`` so the surrounding Unit of
    Work owns the commit boundary (§01 "Key runtime invariants" #3).

    Protocol is deliberately **not** ``runtime_checkable``: structural
    compatibility is checked statically by mypy. Runtime
    ``isinstance`` against this Protocol would mask typos and invite
    duck-typing shortcuts.
    """

    def create_or_patch_turnover_occurrence(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        request: TurnoverOccurrenceRequest,
        now: datetime,
    ) -> TurnoverOccurrenceResult:
        """Apply the create-or-patch state machine. See class docstring.

        ``now`` is threaded through rather than read from a clock so
        the caller (the stays generator's ``handle_reservation_upserted``)
        and the audit row land on the same instant — every event
        handler in the codebase pins ``now`` once at the top and
        passes it down.
        """
        ...


class NoopTasksCreateOccurrencePort:
    """Test / dev double: records every call but does nothing.

    Used by the stays generator's tests (no real tasks adapter to
    hit) and as the default wired into :mod:`app.main` until the
    Phase 5 tasks-side service lands. Once the live adapter lands
    the bootstrap swaps this for the production concretion.

    Every call returns ``("noop", None)``. The class explicitly does
    NOT pretend to track state — pretending would invite tests to
    assert against a fake state machine that diverges from the real
    adapter. Tests that need state coverage should use
    :class:`RecordingTasksCreateOccurrencePort` and assert against
    the recorded call list, NOT against a simulated occurrence row.
    """

    def create_or_patch_turnover_occurrence(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        request: TurnoverOccurrenceRequest,
        now: datetime,
    ) -> TurnoverOccurrenceResult:
        # All four parameters deliberately unused — the real adapter
        # consumes them. Reference each once so static analysis sees
        # the contract is honoured (mypy/ruff don't flag them as
        # unused) and the Protocol shape stays satisfied.
        _ = session, ctx, now, request
        return TurnoverOccurrenceResult(occurrence_id=None, outcome="noop")


class RecordingTasksCreateOccurrencePort:
    """Test double that records every call without persisting.

    Tests assert against :attr:`calls` to verify the stays generator
    produced the expected request shape. Each entry is the
    :class:`TurnoverOccurrenceRequest` the caller passed verbatim,
    in invocation order.

    The recorder emulates a deterministic outcome state machine for
    tests that want to drive a "first call creates, second call
    patches" scenario without a real DB:

    * First call for a ``(reservation_id, rule_id)`` → ``"created"``.
    * Subsequent calls with **identical** ``starts_at`` / ``ends_at``
      → ``"noop"``.
    * Subsequent calls with a shift ``< patch_in_place_threshold``
      → ``"patched"``.
    * Subsequent calls with a shift ``>= patch_in_place_threshold``
      → ``"regenerated"`` (and the recorder updates its memory of
      the latest window so the next call compares against the new
      anchor).

    The simulated state lives only in memory; the recorder never
    writes to ``session``. Tests that need to verify a real
    persistence side-effect should swap this for the SA-backed
    adapter once the Phase 5 service lands.
    """

    def __init__(self) -> None:
        self.calls: list[TurnoverOccurrenceRequest] = []
        # Deterministic id sequence: ``rec_occ_<n>`` so tests can
        # assert on the value without coupling to ULID generation.
        self._anchors: dict[tuple[str, str], tuple[datetime, datetime, str]] = {}
        self._counter = 0

    def create_or_patch_turnover_occurrence(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        request: TurnoverOccurrenceRequest,
        now: datetime,
    ) -> TurnoverOccurrenceResult:
        _ = session, ctx, now
        self.calls.append(request)
        key = (request.reservation_id, request.rule_id)
        previous = self._anchors.get(key)
        if previous is None:
            self._counter += 1
            occ_id = f"rec_occ_{self._counter}"
            self._anchors[key] = (request.starts_at, request.ends_at, occ_id)
            return TurnoverOccurrenceResult(occurrence_id=occ_id, outcome="created")
        prev_starts, prev_ends, occ_id = previous
        if prev_starts == request.starts_at and prev_ends == request.ends_at:
            return TurnoverOccurrenceResult(occurrence_id=occ_id, outcome="noop")
        # Compare on ``starts_at`` shift — the spec's "< 4h" rule
        # uses the lifecycle anchor (``check_out_at`` for an
        # ``after_checkout`` rule, which the generator has already
        # converted to ``starts_at``).
        delta = abs(request.starts_at - prev_starts)
        if delta < request.patch_in_place_threshold:
            self._anchors[key] = (request.starts_at, request.ends_at, occ_id)
            return TurnoverOccurrenceResult(occurrence_id=occ_id, outcome="patched")
        self._counter += 1
        new_id = f"rec_occ_{self._counter}"
        self._anchors[key] = (request.starts_at, request.ends_at, new_id)
        return TurnoverOccurrenceResult(occurrence_id=new_id, outcome="regenerated")
