"""In-process synchronous event bus.

v1 delivers events inline within the publisher's unit of work. If a
subscriber raises, the exception propagates to the publisher and the
surrounding UoW rolls back — that's the boundary contract (spec
§"Boundary rules" #3). Later transports (queue, websocket fan-out)
keep this same ``publish`` shape; only the dispatcher body changes.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from typing import TypeVar

from app.events.registry import Event

__all__ = ["EventBus", "Handler", "bus"]


E = TypeVar("E", bound=Event)

# A handler takes an event of some concrete subtype and returns nothing;
# return values from handlers are ignored on purpose (events are fire-
# and-forget from the publisher's perspective).
Handler = Callable[[E], None]


class EventBus:
    """In-process registry of ``event_name → [handlers]``.

    Not a singleton type — tests spin up fresh instances — but the
    module exposes a :data:`bus` singleton that production code
    subscribes against.
    """

    def __init__(self) -> None:
        # ``defaultdict(list)`` keeps insertion order per event name,
        # which the spec requires for deterministic dispatch.
        self._subscribers: dict[str, list[Handler[Event]]] = defaultdict(list)
        # Serialises subscribe/publish/reset against each other. Handler
        # invocation happens *outside* the lock so a handler that itself
        # publishes (common once the agent runtime lands) doesn't
        # deadlock.
        self._lock = threading.Lock()

    def subscribe(self, event_type: type[E]) -> Callable[[Handler[E]], Handler[E]]:
        """Register a handler for ``event_type``.

        Used as a decorator::

            @bus.subscribe(TaskCompleted)
            def on_task_completed(event: TaskCompleted) -> None:
                ...

        Handlers fire in subscription order when a matching event is
        published. Subscribing the same handler twice is allowed (and
        will fire it twice) — dedup is the caller's responsibility; the
        bus is a plain fan-out.
        """
        name = event_type.name
        if not name:
            raise ValueError(
                f"{event_type.__name__} has no ``name`` ClassVar; subscribe "
                "to a concrete registered Event subclass."
            )

        def _decorator(handler: Handler[E]) -> Handler[E]:
            # Wrap in a shim typed as ``Handler[Event]`` so the stored
            # list is homogeneous. ``Callable`` is contravariant in its
            # argument, so a handler taking a concrete subclass is not
            # a subtype of one taking ``Event`` — instead of paper-over
            # casts, the shim narrows the event via ``isinstance`` and
            # re-raises on a type mismatch. The dispatch lookup by name
            # keeps this branch unreachable in practice.
            def _shim(event: Event) -> None:
                if not isinstance(event, event_type):
                    raise TypeError(
                        f"Handler registered for {event_type.__name__} "
                        f"was dispatched an incompatible {type(event).__name__}."
                    )
                handler(event)

            with self._lock:
                self._subscribers[name].append(_shim)
            return handler

        return _decorator

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber synchronously.

        Subscribers fire in insertion order. **If a handler raises, the
        exception propagates immediately and no later handler runs.**
        That is deliberate: the publisher's UoW is still open, and the
        bus must not swallow failures that should roll the transaction
        back.
        """
        name = type(event).name
        with self._lock:
            # Snapshot under the lock so a concurrent subscribe/reset
            # can't mutate the list mid-iteration.
            handlers = list(self._subscribers.get(name, ()))
        for handler in handlers:
            handler(event)

    def _reset_for_tests(self) -> None:
        """Drop every subscription. Tests use this to isolate cases."""
        with self._lock:
            self._subscribers.clear()


# Production singleton. Tests either use a fresh ``EventBus()`` or call
# ``bus._reset_for_tests()`` between cases.
bus: EventBus = EventBus()
