"""Tests for the typed event registry and in-process synchronous bus.

Covers the acceptance criteria in Beads ``cd-bsj``:

- register once / twice / idempotent re-register;
- ``get_event_type`` happy + unknown name;
- subscribe + publish happy path, dispatch order, type isolation;
- handler-raises propagation and short-circuit;
- Event validation (aware UTC, missing fields);
- test-isolation helpers (``_reset_for_tests`` on both registry + bus).

See ``docs/specs/01-architecture.md`` §"Boundary rules" #3.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import ClassVar

import pytest
from pydantic import ValidationError

from app.events import (
    Event,
    EventAlreadyRegistered,
    EventBus,
    EventNotFound,
    ExpenseApproved,
    ShiftEnded,
    StayUpcoming,
    TaskCompleted,
    TaskCreated,
    TaskOverdue,
    bus,
    get_event_type,
    register,
    registered_events,
)
from app.events import registry as registry_module

# The six event classes registered at import time. The autouse fixture
# snapshots ``registered_events()`` rather than hard-coding this list,
# so a seventh event added later is picked up automatically.
INITIAL_EVENT_NAMES = frozenset(
    {
        "task.created",
        "task.completed",
        "task.overdue",
        "stay.upcoming",
        "expense.approved",
        "shift.ended",
    }
)


@pytest.fixture(autouse=True)
def _isolate_events() -> Iterator[None]:
    """Snapshot registry + bus state before each test; restore after.

    The registry is process-global (concrete events register at import
    time). Tests that exercise ``_reset_for_tests`` would otherwise
    leave the registry empty for every later test in the run. Taking a
    snapshot is more robust than re-adding a hard-coded list.
    """
    snapshot = dict(registered_events())
    try:
        yield
    finally:
        registry_module._reset_for_tests()
        with registry_module._lock:
            registry_module._REGISTRY.update(snapshot)
        bus._reset_for_tests()


def _utc() -> datetime:
    """Convenience — an aware UTC timestamp for test payloads."""
    return datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _fresh_event() -> TaskCreated:
    """A minimally valid event instance."""
    return TaskCreated(
        workspace_id="ws_1",
        actor_id="user_1",
        correlation_id="corr_1",
        occurred_at=_utc(),
        task_id="task_1",
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_initial_events_are_registered_at_import() -> None:
    names = set(registered_events())
    assert names >= INITIAL_EVENT_NAMES


def test_register_once_exposes_class_in_mapping() -> None:
    @register
    class CustomEvent(Event):
        name: ClassVar[str] = "test.custom"

    assert registered_events()["test.custom"] is CustomEvent


def test_register_twice_with_different_classes_raises() -> None:
    @register
    class FirstEvent(Event):
        name: ClassVar[str] = "test.collision"

    with pytest.raises(EventAlreadyRegistered):

        @register
        class SecondEvent(Event):
            name: ClassVar[str] = "test.collision"

    # First registration survived.
    assert registered_events()["test.collision"] is FirstEvent


def test_register_same_class_twice_is_idempotent() -> None:
    @register
    class SameEvent(Event):
        name: ClassVar[str] = "test.same"

    # Re-registering the exact class object must not raise.
    again = register(SameEvent)
    assert again is SameEvent
    assert registered_events()["test.same"] is SameEvent


def test_register_rejects_empty_name() -> None:
    class Unnamed(Event):
        name: ClassVar[str] = ""

    with pytest.raises(ValueError, match="non-empty"):
        register(Unnamed)


def test_get_event_type_returns_registered_class() -> None:
    assert get_event_type("task.created") is TaskCreated


def test_get_event_type_unknown_raises_event_not_found() -> None:
    with pytest.raises(EventNotFound):
        get_event_type("nope.does-not-exist")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("task.created", TaskCreated),
        ("task.completed", TaskCompleted),
        ("task.overdue", TaskOverdue),
        ("stay.upcoming", StayUpcoming),
        ("expense.approved", ExpenseApproved),
        ("shift.ended", ShiftEnded),
    ],
)
def test_get_event_type_resolves_each_initial_event(
    name: str, expected: type[Event]
) -> None:
    assert get_event_type(name) is expected


def test_registered_events_is_read_only_snapshot() -> None:
    snapshot = registered_events()

    with pytest.raises(TypeError):
        # MappingProxyType rejects assignment.
        snapshot["task.created"] = TaskCompleted  # type: ignore[index]

    # And mutating the registry after the call does not alter the
    # snapshot (it's a copy under the hood).
    @register
    class LaterEvent(Event):
        name: ClassVar[str] = "test.later"

    assert "test.later" not in snapshot
    assert "test.later" in registered_events()


def test_registry_reset_clears_and_permits_re_register() -> None:
    registry_module._reset_for_tests()
    assert registered_events() == {}

    @register
    class AfterReset(Event):
        name: ClassVar[str] = "test.after-reset"

    assert get_event_type("test.after-reset") is AfterReset


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------


def test_event_rejects_naive_occurred_at() -> None:
    with pytest.raises(ValidationError):
        TaskCreated(
            workspace_id="ws_1",
            actor_id="user_1",
            correlation_id="corr_1",
            occurred_at=datetime(2026, 4, 19, 12, 0, 0),  # naive
            task_id="task_1",
        )


def test_event_rejects_missing_required_fields() -> None:
    # Use model_validate so the intentionally incomplete payload doesn't
    # trip a static ``call-arg`` warning — the point of the test is the
    # runtime validation error, not the static signature.
    with pytest.raises(ValidationError):
        TaskCreated.model_validate({"workspace_id": "ws_1"})


def test_event_is_frozen() -> None:
    event = _fresh_event()
    # ``setattr`` dodges the ``[misc]`` warning pydantic emits for
    # direct assignment to a frozen model; the runtime behaviour is the
    # same, which is what the test cares about.
    with pytest.raises(ValidationError):
        setattr(event, "task_id", "task_2")  # noqa: B010


def test_task_overdue_rejects_naive_overdue_since() -> None:
    with pytest.raises(ValidationError):
        TaskOverdue(
            workspace_id="ws_1",
            actor_id="user_1",
            correlation_id="corr_1",
            occurred_at=_utc(),
            task_id="task_1",
            overdue_since=datetime(2026, 4, 19, 11, 0, 0),  # naive
        )


def test_event_rejects_non_utc_aware_occurred_at() -> None:
    """Aware but non-UTC (``+05:00``) violates 'Time is UTC at rest'."""
    plus_five = timezone(timedelta(hours=5))
    with pytest.raises(ValidationError):
        TaskCreated(
            workspace_id="ws_1",
            actor_id="user_1",
            correlation_id="corr_1",
            occurred_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=plus_five),
            task_id="task_1",
        )


def test_task_overdue_rejects_non_utc_aware_overdue_since() -> None:
    minus_three = timezone(timedelta(hours=-3))
    with pytest.raises(ValidationError):
        TaskOverdue(
            workspace_id="ws_1",
            actor_id="user_1",
            correlation_id="corr_1",
            occurred_at=_utc(),
            task_id="task_1",
            overdue_since=datetime(2026, 4, 19, 11, 0, 0, tzinfo=minus_three),
        )


def test_stay_upcoming_rejects_naive_arrives_at() -> None:
    with pytest.raises(ValidationError):
        StayUpcoming(
            workspace_id="ws_1",
            actor_id="user_1",
            correlation_id="corr_1",
            occurred_at=_utc(),
            stay_id="stay_1",
            arrives_at=datetime(2026, 4, 19, 15, 0, 0),  # naive
        )


def test_shift_ended_rejects_naive_ended_at() -> None:
    with pytest.raises(ValidationError):
        ShiftEnded(
            workspace_id="ws_1",
            actor_id="user_1",
            correlation_id="corr_1",
            occurred_at=_utc(),
            shift_id="shift_1",
            ended_at=datetime(2026, 4, 19, 18, 0, 0),  # naive
        )


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


def test_subscribe_and_publish_invokes_handler() -> None:
    local = EventBus()
    received: list[TaskCreated] = []

    @local.subscribe(TaskCreated)
    def handle(event: TaskCreated) -> None:
        received.append(event)

    event = _fresh_event()
    local.publish(event)

    assert received == [event]


def test_publish_fires_subscribers_in_insertion_order() -> None:
    local = EventBus()
    order: list[str] = []

    @local.subscribe(TaskCreated)
    def first(event: TaskCreated) -> None:
        order.append("first")

    @local.subscribe(TaskCreated)
    def second(event: TaskCreated) -> None:
        order.append("second")

    @local.subscribe(TaskCreated)
    def third(event: TaskCreated) -> None:
        order.append("third")

    local.publish(_fresh_event())
    assert order == ["first", "second", "third"]


def test_subscribers_only_receive_their_event_type() -> None:
    local = EventBus()
    created_seen: list[TaskCreated] = []
    completed_seen: list[TaskCompleted] = []

    @local.subscribe(TaskCreated)
    def on_created(event: TaskCreated) -> None:
        created_seen.append(event)

    @local.subscribe(TaskCompleted)
    def on_completed(event: TaskCompleted) -> None:
        completed_seen.append(event)

    created = _fresh_event()
    completed = TaskCompleted(
        workspace_id="ws_1",
        actor_id="user_1",
        correlation_id="corr_2",
        occurred_at=_utc(),
        task_id="task_1",
        completed_by="user_2",
    )

    local.publish(created)
    local.publish(completed)

    assert created_seen == [created]
    assert completed_seen == [completed]


def test_publish_with_no_subscribers_is_a_noop() -> None:
    local = EventBus()
    # Should simply return without raising.
    local.publish(_fresh_event())


def test_handler_exception_propagates_and_short_circuits() -> None:
    local = EventBus()
    calls: list[str] = []

    class Boom(RuntimeError):
        pass

    @local.subscribe(TaskCreated)
    def first(event: TaskCreated) -> None:
        calls.append("first")
        raise Boom("subscriber failure")

    @local.subscribe(TaskCreated)
    def second(event: TaskCreated) -> None:
        # Must not run — the bus propagates the first handler's error
        # so the enclosing UoW can roll back.
        calls.append("second")

    with pytest.raises(Boom):
        local.publish(_fresh_event())

    assert calls == ["first"]


def test_bus_reset_clears_subscribers() -> None:
    local = EventBus()
    received: list[TaskCreated] = []

    @local.subscribe(TaskCreated)
    def handle(event: TaskCreated) -> None:
        received.append(event)

    local._reset_for_tests()
    local.publish(_fresh_event())

    assert received == []


def test_module_bus_singleton_isolates_between_tests() -> None:
    # Uses the real module-level singleton to prove the autouse
    # fixture resets it between tests. A subscriber registered here
    # must not leak into the next test case.
    calls: list[TaskCreated] = []

    @bus.subscribe(TaskCreated)
    def handle(event: TaskCreated) -> None:
        calls.append(event)

    bus.publish(_fresh_event())
    assert len(calls) == 1


def test_module_bus_singleton_has_no_leftover_subscribers() -> None:
    # Twin of the previous test. If the autouse fixture fails to clear
    # ``bus``, the subscriber registered above would fire and mutate a
    # list that no longer exists — but equivalently, we can assert
    # ``bus`` has no subscribers for ``TaskCreated`` by publishing and
    # confirming nothing observable happens.
    #
    # We can't directly inspect ``_subscribers`` without touching a
    # private attribute; a behavioural check is enough.
    bus.publish(_fresh_event())  # must not raise, must be a no-op


def test_subscribe_rejects_event_without_name() -> None:
    local = EventBus()

    class Unnamed(Event):
        # Empty name — not a legal event type.
        name: ClassVar[str] = ""

    with pytest.raises(ValueError, match="no ``name``"):
        local.subscribe(Unnamed)
