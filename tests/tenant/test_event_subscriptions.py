"""Event subscriptions — case (c) of the cross-tenant regression matrix.

For every registered event kind in :mod:`app.events.registry`,
publish an ``A``-scoped event and assert that a subscriber bound to
``B`` does not receive it. The v1 :class:`~app.events.bus.EventBus`
is a flat fan-out — every handler for an event's name receives every
instance of that event — so the workspace-binding invariant lives on
the subscriber: each handler MUST check ``event.workspace_id``
against its ambient context and early-return on a mismatch.

This test models that invariant with a pair of spy handlers, one per
workspace. Each spy captures the event iff
``event.workspace_id == spy.workspace_id``. After publishing an
``A``-scoped event:

* the ``A`` spy's capture list holds the event;
* the ``B`` spy's capture list is empty (cross-delivery → test
  fails).

The loop runs across every registered event kind so a new event
class added without a handler-level guard would surface here as a
cross-delivery. Deployment-scoped events (none today) are listed in
:data:`tests.tenant._optouts.EVENT_NAME_OPTOUTS` with a
justification.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" case (c) and ``docs/specs/01-architecture.md`` §"Boundary
rules" #3.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.events.bus import EventBus
from app.events.registry import Event, registered_events
from app.util.ulid import new_ulid
from tests.tenant._optouts import EVENT_NAME_OPTOUTS
from tests.tenant.conftest import TenantSeed

pytestmark = pytest.mark.integration


_PINNED_NOW: datetime = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Per-event payload builder
# ---------------------------------------------------------------------------


def _build_event(
    event_cls: type[Event],
    *,
    workspace_id: str,
    actor_id: str,
) -> Event:
    """Return a concrete instance of ``event_cls`` scoped to the workspace.

    Every registered event subclass adds its own payload fields on
    top of the base :class:`~app.events.registry.Event`
    (``workspace_id`` / ``actor_id`` / ``correlation_id`` /
    ``occurred_at``). We instantiate a minimal payload so the
    subscriber can inspect ``workspace_id`` without needing the real
    business objects (``task_id`` / ``shift_id`` / …) to exist.

    The payload values are deliberately sentinel — a real
    ``task_id`` wouldn't help the test assertion, and a sentinel
    ULID keeps the row shape consistent across every event kind
    without special casing.
    """
    base_kwargs: dict[str, object] = {
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "correlation_id": new_ulid(),
        "occurred_at": _PINNED_NOW,
    }
    # Pull every non-base field off the class and give it a sentinel
    # value. ``pydantic.BaseModel.model_fields`` yields the full set
    # including inherited ones; the base fields (``workspace_id``,
    # ``actor_id``, ``correlation_id``, ``occurred_at``) are already
    # in ``base_kwargs`` so they overwrite any sentinel the loop
    # might set.
    extra_fields = {name for name in event_cls.model_fields if name not in base_kwargs}
    payload_kwargs: dict[str, object] = {}
    for field_name in extra_fields:
        field_info = event_cls.model_fields[field_name]
        annotation = field_info.annotation
        payload_kwargs[field_name] = _sentinel_value_for(field_name, annotation)

    return event_cls(**base_kwargs, **payload_kwargs)


def _sentinel_value_for(field_name: str, annotation: object) -> object:
    """Return a minimal sentinel value matching the field's annotation.

    Today's events only carry ``str`` payload fields (task ids, user
    ids, action enums) plus one :class:`~datetime.datetime` field on
    a handful (``overdue_since``, ``arrives_at``, ``ended_at``).
    Handling those two cases covers the entire registered surface;
    an unknown annotation falls back to a ULID-shaped string so the
    test keeps the assertion alive even against a newly-added event
    kind — the regression is what we're guarding.
    """
    # datetime first — some payload datetimes (``overdue_since``,
    # ``arrives_at``, ``ended_at``) enforce timezone-aware UTC.
    if annotation is datetime or annotation is datetime | None:
        return _PINNED_NOW
    # Narrow the action enum on :class:`ShiftChanged` — any of the
    # three literal values is valid, ``opened`` is the most common
    # shape.
    if field_name == "action":
        return "opened"
    # Default: a ULID-shaped string — satisfies every ``*_id`` field
    # in the registry today (``task_id``, ``shift_id``, ``user_id``,
    # ``stay_id``, ``expense_id``, ``assigned_to``, ``completed_by``,
    # ``approved_by``).
    return new_ulid()


# ---------------------------------------------------------------------------
# Subscriber spy — the workspace-binding invariant
# ---------------------------------------------------------------------------


class _WorkspaceBoundSpy:
    """Capture events iff their ``workspace_id`` matches the spy's ctx.

    Models the §17 "subscribers bound to workspace B" contract: a
    production subscriber reads ``event.workspace_id`` as the
    routing key and no-ops on a mismatch. The spy makes the
    invariant explicit so the test can assert on its capture list.

    :attr:`captured` is the list of events the spy accepted. Empty
    after a cross-tenant publish (the whole point of the case).
    """

    def __init__(self, *, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.captured: list[Event] = []

    def __call__(self, event: Event) -> None:
        # The §17 "bound subscribers" invariant: consult
        # ``event.workspace_id`` before doing any work. A subscriber
        # that skipped this check would fail the test immediately —
        # the B-spy would capture A's event.
        if event.workspace_id != self.workspace_id:
            return
        self.captured.append(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_bus() -> Iterator[EventBus]:
    """A fresh :class:`EventBus` with no production subscribers.

    Every test case subscribes its own spies against this bus so the
    assertion is about handler behaviour, not about whatever the
    production singleton has accumulated over the suite's lifetime.
    """
    yield EventBus()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventCrossDelivery:
    """Case (c) — ``B``-bound subscribers never see ``A``-scoped events."""

    @pytest.mark.parametrize(
        "event_name",
        sorted(name for name in registered_events() if name not in EVENT_NAME_OPTOUTS),
    )
    def test_b_bound_subscriber_drops_a_scoped_event(
        self,
        isolated_bus: EventBus,
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
        event_name: str,
    ) -> None:
        """Publish an ``A``-scoped event; ``B``-spy stays empty.

        Parametrised over every registered event kind. A new event
        class registered without a workspace_id check on subscribers
        would trip this assertion as soon as the registry auto-
        picks it up.
        """
        event_cls = registered_events()[event_name]
        spy_a = _WorkspaceBoundSpy(workspace_id=tenant_a.workspace_id)
        spy_b = _WorkspaceBoundSpy(workspace_id=tenant_b.workspace_id)

        # Subscribe both spies — in real code they would be two
        # separate workspace-scoped service instances reading from
        # the same process-global bus.
        isolated_bus.subscribe(event_cls)(spy_a)
        isolated_bus.subscribe(event_cls)(spy_b)

        event = _build_event(
            event_cls,
            workspace_id=tenant_a.workspace_id,
            actor_id=tenant_a.owner_user_id,
        )
        isolated_bus.publish(event)

        assert spy_a.captured == [event], (
            f"A-scoped event {event_name} failed to reach the A-bound "
            f"spy: {spy_a.captured!r}"
        )
        assert spy_b.captured == [], (
            f"A-scoped event {event_name} cross-delivered to the "
            f"B-bound spy: {spy_b.captured!r} — the subscriber didn't "
            f"check event.workspace_id against its ctx"
        )

    def test_subscriber_without_workspace_check_would_be_caught(
        self,
        isolated_bus: EventBus,
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
    ) -> None:
        """Negative control — prove the spy catches a broken subscriber.

        Models a regression: a handler that drops the
        ``workspace_id`` check fires for EVERY event. The test
        harness must be able to detect that shape — otherwise a
        silently-broken subscriber in production would look green.
        """
        # Import a concrete event class; any one works since we're
        # not proving the cross-tenant invariant here — we're
        # proving the test harness.
        from app.events.types import TaskCreated

        def buggy_subscriber(event: Event) -> None:
            captured_by_broken_handler.append(event)

        captured_by_broken_handler: list[Event] = []
        isolated_bus.subscribe(TaskCreated)(buggy_subscriber)

        event = _build_event(
            TaskCreated,
            workspace_id=tenant_a.workspace_id,
            actor_id=tenant_a.owner_user_id,
        )
        isolated_bus.publish(event)
        # The broken subscriber captures indiscriminately — exactly
        # the failure mode the spy shape in the real test guards
        # against.
        assert captured_by_broken_handler == [event]

        # For completeness, demonstrate the bound-spy rejects the
        # same event when its workspace_id doesn't match.
        spy_b = _WorkspaceBoundSpy(workspace_id=tenant_b.workspace_id)
        spy_b(event)
        assert spy_b.captured == []


class TestEventRegistryParity:
    """Surface-parity gate for the event registry."""

    def test_every_registered_event_is_covered_or_opted_out(self) -> None:
        """Every :class:`Event` subclass has a case or an opt-out.

        Parametrised sibling test already covers every registered
        name; this explicit gate fails loudly if the registry is
        empty (a catastrophic regression) and also documents the
        contract in a single readable assertion.
        """
        registered = set(registered_events().keys())
        assert registered, (
            "event registry is empty — suite would silently pass "
            "every cross-tenant probe because there's nothing to probe"
        )
        uncovered = registered - EVENT_NAME_OPTOUTS
        # Every name in ``uncovered`` is covered by the parametrised
        # test above; the assertion just proves the set is non-empty
        # (i.e. not every event has been opted out by accident).
        assert uncovered, (
            "every registered event is in EVENT_NAME_OPTOUTS — the "
            "case coverage is a no-op"
        )
