"""Server-Sent Events transport at ``/w/<slug>/events`` (cd-clz9).

A single ``EventSource('/w/<slug>/events')`` per browser tab carries
every cross-client coherence signal — the TanStack Query invalidation
stream (§14), plus the agent.* lifecycle signals (§11). The SPA feeds
received events into ``queryClient.invalidateQueries`` and the inline
approval card renderer; there is no polling anywhere in the product
for workspace-scoped live state.

This module wires three seams:

1. **Ingress — the event bus.** A single handler registered per
   concrete :class:`~app.events.registry.Event` subclass forwards every
   publication into :class:`SSEFanOut`. The handler subscription is
   done lazily the first time a request handler reaches this module,
   so an import-time side effect does not surprise the factory.
2. **Fan-out state — :class:`SSEFanOut`.** Per-workspace state: a
   monotonic ``next_id`` counter, a bounded deque of recent events
   (60 s retention) for ``Last-Event-ID`` replay, and the set of live
   subscribers. Every subscriber is a bounded ``asyncio.Queue``; a
   slow consumer that lets the queue fill gets a single
   ``event: dropped`` frame and the connection is closed — the rest
   of the fanout never blocks on any one client.
3. **Egress — the SSE handler.** A FastAPI handler on the
   workspace-scoped router that returns a
   :class:`~starlette.responses.StreamingResponse` with the
   ``text/event-stream`` envelope, honouring ``Last-Event-ID``
   reconnects and the 15 s heartbeat cadence clients expect.

Role / user scope filtering reads two ``ClassVar`` seams declared on
:class:`~app.events.registry.Event`:

* ``allowed_roles`` — role allowlist for the event kind. The caller's
  :attr:`~app.tenancy.WorkspaceContext.actor_grant_role` must be in
  the tuple.
* ``user_scoped`` — when ``True`` the event has an ``actor_user_id``
  payload field and is delivered only to the matching user's
  connections.

PII posture: the transport forwards whatever the publisher put on the
event payload. Events carry foreign-key IDs + small scalars only —
free-text bodies are pulled by the client via REST using the
delegated session, so the REST-layer authorisation stays the single
source of truth. The role allowlist protects against a worker
seeing a manager-only event kind at all; the user scope protects
against user A seeing user B's agent messages.

The admin-scope ``/admin/events`` twin is **future work** (gated on
cd-yj4k's ``/admin/api/v1/me`` foundation). See the note next to
:func:`_extract_role` below.

See ``docs/specs/11-llm-and-agents.md`` §"Agent turn lifecycle",
§"Inline approval UX", §"Workspace usage budget", §"Embedded agents",
and ``docs/specs/14-web-frontend.md`` §"SSE-driven invalidation".
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from starlette.responses import StreamingResponse

from app.api.deps import current_workspace_context
from app.events.bus import EventBus
from app.events.bus import bus as default_bus
from app.events.registry import Event, EventRole, registered_events
from app.tenancy.context import WorkspaceContext

# ``Annotated[T, Depends(...)]`` rather than ``= Depends(...)`` to
# keep ruff's B008 happy — matches the convention in
# :mod:`app.api.v1.time` and siblings.
_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]

__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "MAX_CLIENT_QUEUE",
    "REPLAY_WINDOW_S",
    "SSEFanOut",
    "default_fanout",
    "router",
]

# ``_ParsedLastEventId`` + ``_parse_last_event_id`` are intentionally
# module-private (underscore-prefixed), same as the other internals
# the test suite drives directly. Tests import them by name rather
# than adding them to :data:`__all__`; the public surface here is
# deliberately narrow.

_log = logging.getLogger(__name__)


# Retention window for the per-workspace replay buffer. A client that
# reconnects with ``Last-Event-ID`` gets any newer events still inside
# this window; anything older is dropped (the client issues a full
# refetch instead, which is cheaper than keeping unbounded history).
REPLAY_WINDOW_S: Final[float] = 60.0

# Heartbeat comment cadence. Must be short enough that a stale proxy /
# intermediary closes the connection on actual death rather than on a
# quiet stretch. 15 s is the value SSE libraries converge on — long
# enough to be cheap, short enough to beat most proxy idle timeouts.
HEARTBEAT_INTERVAL_S: Final[float] = 15.0

# Bounded per-client queue depth. A manager tab watching a chatty
# workspace might see a burst (20+ events in a second on a batch
# import); this bound gives the client ~a second of buffer before we
# drop. Picked empirically — small enough to catch a wedged client
# quickly, large enough that a healthy client never trips it.
MAX_CLIENT_QUEUE: Final[int] = 256

# First-packet ``retry:`` hint. Clients use it as the reconnect delay
# when the socket drops; 3 s is the SSE convention and matches what
# the SPA's default ``EventSource`` expects.
_RECONNECT_MS: Final[int] = 3000

# Maximum accepted length for a ``Last-Event-ID`` header value. SSE
# clients only ever echo back an id the server previously emitted, so
# the field is effectively bounded by the server's own format
# (``<stream_token>-<seq>``, ≤32 chars in practice). A header longer
# than this is almost certainly junk (a broken proxy, a probe) and we
# refuse to parse it rather than spend time on adversarial input.
_MAX_LAST_EVENT_ID_LEN: Final[int] = 128


router = APIRouter(tags=["transport"])


# ---------------------------------------------------------------------------
# Fan-out state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedLastEventId:
    """Result of parsing the ``Last-Event-ID`` header.

    ``stream_token`` is the client's last-seen server instance token
    (``None`` when the header is missing or the client never saw an
    id on the prior session). ``seq`` is the monotonic counter the
    client last saw within that instance; 0 means "no resume point".

    Treating missing / malformed headers as ``(None, 0)`` keeps the
    downstream decision simple: we always replay from the start of
    the current buffer when the tokens don't line up, and we never
    400 a client that has already reconnected (rejecting would only
    make it spin — SSE reconnection has no way to ignore a bad
    server response and stop).
    """

    stream_token: str | None
    seq: int


def _effective_replay_seq(parsed: _ParsedLastEventId, current_token: str) -> int:
    """Return the sequence cutoff to use against the current buffer.

    Match → honour the client's cutoff. Mismatch (or missing token)
    → ``0`` so the current buffer is replayed in full.
    """
    if parsed.stream_token == current_token and parsed.seq > 0:
        return parsed.seq
    return 0


@dataclass(frozen=True)
class _BufferedEvent:
    """A single event retained for ``Last-Event-ID`` replay.

    ``emitted_at_monotonic`` is used to prune entries older than the
    replay window; it is monotonic-clock seconds, not wall-clock, so
    the buffer behaves correctly across a clock step.

    ``wire_bytes`` is the pre-encoded SSE frame (including the
    trailing blank line). We pre-encode once at publish time so a
    burst of reconnects replaying the same range doesn't pay the
    JSON encoder cost per client.
    """

    event_id: int
    kind: str
    user_scope: str | None  # user_id when the event is user-scoped
    roles: tuple[EventRole, ...]
    emitted_at_monotonic: float
    wire_bytes: bytes


@dataclass
class _WorkspaceState:
    """Per-workspace SSE fan-out state.

    One instance is created the first time an event is published for
    (or a subscriber is added to) a workspace; it is never deleted
    — workspace churn over a process lifetime is low and the tiny
    per-workspace overhead (one deque + one set) is cheaper than the
    synchronisation needed to reap empty entries.
    """

    next_id: int = 0
    buffer: deque[_BufferedEvent] = field(default_factory=deque)
    subscribers: set[_Subscriber] = field(default_factory=set)


@dataclass
class _Subscriber:
    """One connected client.

    ``queue`` is bounded; when a publisher cannot ``put_nowait`` the
    subscriber is flagged ``dropped`` and the handler's generator
    notices on the next wake, emits the ``dropped`` frame, and
    unsubscribes.

    Equality is identity — two :class:`_Subscriber` instances that
    carry the same ``user_id`` + ``role`` are still distinct clients
    (two tabs of the same user both subscribe and should each get
    their own copy).
    """

    user_id: str
    role: EventRole
    queue: asyncio.Queue[bytes]
    loop: asyncio.AbstractEventLoop
    dropped: bool = False

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return other is self


class SSEFanOut:
    """Per-process SSE fan-out for the workspace ``/events`` stream.

    One instance (:data:`default_fanout`) lives on the module. Tests
    instantiate their own to avoid state leaks; the handler reads
    :data:`default_fanout` through :func:`_get_fanout` so tests can
    monkeypatch the lookup without reaching into module globals.

    The public surface is small:

    * :meth:`publish` — called by the bus handler registered in
      :meth:`bind_to_bus`. Appends to the per-workspace buffer and
      wakes every matching subscriber.
    * :meth:`subscribe` / :meth:`unsubscribe` — per-connection add /
      remove. The handler calls these once per request.
    * :meth:`replay_since` — returns buffered events newer than the
      ``Last-Event-ID`` the client reconnected with.
    """

    def __init__(self) -> None:
        self._workspaces: dict[str, _WorkspaceState] = {}
        # The bus is synchronous but can fire from any thread (the
        # APScheduler worker, a background task). Subscribe /
        # unsubscribe comes from the HTTP handler's asyncio loop.
        # A plain :class:`threading.Lock` keeps the ``next_id``
        # increment + deque append + subscribers snapshot atomic
        # across threads. Held only while we mutate the state — the
        # actual queue writes happen outside the lock (either
        # ``put_nowait`` on the same loop or ``call_soon_threadsafe``
        # onto the subscriber's loop) so holding it never blocks
        # the event loop.
        self._lock = threading.Lock()
        # Per-instance stream token prefixed onto every emitted event
        # id. Regenerated for each :class:`SSEFanOut` (so a process
        # restart — or a test swap — produces a new token). Clients
        # reconnect with ``Last-Event-ID: <stream_token>-<seq>``; when
        # the token matches ours we replay from ``seq``, and when it
        # doesn't we know the counter they cite is from a prior
        # instance and replay our current buffer from 0 instead of
        # silently skipping events whose fresh sequence numbers fall
        # below the client's stale reference. 8 hex chars (32 bits) is
        # enough entropy to make a collision across restart cycles
        # essentially impossible for an operator-scale deployment.
        self._stream_token: str = secrets.token_hex(4)

    # ---- Bus binding -------------------------------------------------

    def bind_to_bus(self, event_bus: EventBus) -> None:
        """Wire one forwarding handler per registered Event subclass.

        Called once per process from the handler's startup path
        (:func:`_ensure_bus_binding`). Safe to call more than once:
        the registry is a snapshot at call time and a later
        ``@register`` would not be seen, but re-binding would double
        every handler. The caller owns the once-per-process gate;
        :class:`SSEFanOut` itself is re-binding-safe only within a
        fresh instance.
        """
        snapshot = dict(registered_events())
        for event_cls in snapshot.values():
            event_bus.subscribe(event_cls)(self._forward)

    def _forward(self, event: Event) -> None:
        """Bus handler — turn ``event`` into a wire frame and fan out.

        Runs in whatever thread the publisher was on (the bus is sync).
        The per-subscriber dispatch in :meth:`_deliver` hops the
        actual queue write onto the subscriber's event loop via
        :func:`asyncio.AbstractEventLoop.call_soon_threadsafe` when
        we're off-loop; when we're on-loop (the common case —
        publishers inside a FastAPI handler) we ``put_nowait``
        directly.
        """
        cls = type(event)
        kind = cls.name
        roles = _roles_for_event(event, kind=kind, default=tuple(cls.allowed_roles))
        user_scope: str | None = None
        if cls.user_scoped:
            # The registry invariant (see ``registry.register``)
            # guarantees the field exists on any class registered with
            # ``user_scoped=True``; a ``getattr`` without a default
            # would trip on a mis-registered class that somehow
            # slipped past. We validate + narrow to ``str`` here so
            # mypy and the runtime agree.
            raw_scope: object = getattr(event, "actor_user_id", None)
            if not isinstance(raw_scope, str) or not raw_scope:
                raise TypeError(
                    f"{cls.__name__}.actor_user_id must be a non-empty "
                    "string for user-scoped events."
                )
            user_scope = raw_scope
        payload = self._serialise_payload(event, kind)
        self.publish(
            workspace_id=event.workspace_id,
            kind=kind,
            roles=roles,
            user_scope=user_scope,
            payload=payload,
        )

    @staticmethod
    def _serialise_payload(event: Event, kind: str) -> dict[str, Any]:
        """Return the JSON-serialisable ``data:`` body for ``event``.

        We call :meth:`pydantic.BaseModel.model_dump` with ``mode=json``
        so :class:`datetime` fields come out as ISO-8601 strings (the
        SPA expects UTC ISO strings everywhere; see §14 "Time is UTC
        at rest"). The ``kind`` field is added so a client that
        multiplexes over the SSE stream does not have to re-derive it
        from the ``event:`` header line.
        """
        dumped = event.model_dump(mode="json")
        dumped["kind"] = kind
        return dumped

    # ---- Publish / subscribe ----------------------------------------

    def publish(
        self,
        *,
        workspace_id: str,
        kind: str,
        roles: tuple[EventRole, ...],
        user_scope: str | None,
        payload: dict[str, Any],
    ) -> None:
        """Append ``payload`` to the workspace buffer; wake subscribers.

        ``user_scope`` is ``None`` for workspace-wide events and the
        ``actor_user_id`` for user-scoped events. Matching is done at
        delivery time so a late-attaching subscriber with a different
        ``user_id`` cannot replay a buffered user-scoped event not
        addressed to them.

        Called from the bus handler (synchronously, on the publisher's
        thread). Safe to call without an active event loop — when the
        publisher is off-loop we detect the missing running loop
        (:func:`asyncio.get_running_loop` raises
        :class:`RuntimeError`) and hop every subscriber dispatch
        through ``call_soon_threadsafe`` onto the subscriber's loop.
        :class:`asyncio.Queue` itself is not thread-safe; the
        cross-loop hop is what makes the fan-out safe across threads.
        """
        now = time.monotonic()
        with self._lock:
            state = self._state_for(workspace_id)
            state.next_id += 1
            event_id = state.next_id

            # Stamp the invalidation contract into the payload so
            # the SPA doesn't have to carry a name→query-keys map
            # (§14). Callers that want to override the computed
            # default can pre-set ``invalidates`` on their payload
            # before publishing; we respect it if present.
            # Workspace-scoped query keys all start with the
            # workspace slug, but the server does not know the slug
            # here (the event carries ``workspace_id``), so the
            # invalidation entries are kind names the client maps.
            payload.setdefault("invalidates", _default_invalidates(kind))
            # Always stamp ``kind`` + ``workspace_id`` so a client
            # multiplexing over the stream doesn't have to re-derive
            # them from the ``event:`` / URL path (§14 "SSE-driven
            # invalidation").
            payload.setdefault("kind", kind)
            payload.setdefault("workspace_id", workspace_id)

            wire_bytes = _format_sse_frame(
                event_id=f"{self._stream_token}-{event_id}",
                kind=kind,
                payload=payload,
            )
            buffered = _BufferedEvent(
                event_id=event_id,
                kind=kind,
                user_scope=user_scope,
                roles=roles,
                emitted_at_monotonic=now,
                wire_bytes=wire_bytes,
            )
            state.buffer.append(buffered)
            self._prune(state, now=now)
            # Snapshot subscribers while we hold the lock so a
            # concurrent subscribe/unsubscribe can't trip the fan-out
            # iteration below. We don't hold the lock across the
            # queue writes themselves.
            targets = tuple(state.subscribers)

        # Resolve "am I on the subscriber's loop?" once per publish
        # so we don't pay the ``get_running_loop`` lookup in the
        # per-subscriber inner loop. ``None`` means "no loop is
        # running on the publisher's thread" — the bus was fired
        # synchronously from a worker thread — in which case every
        # subscriber dispatch goes through ``call_soon_threadsafe``.
        publisher_loop: asyncio.AbstractEventLoop | None
        try:
            publisher_loop = asyncio.get_running_loop()
        except RuntimeError:
            publisher_loop = None

        # Dispatch to the snapshot captured under the lock. Dropped
        # subscribers remain in the set until their handler loop
        # notices + unsubscribes; we simply skip them here.
        for sub in targets:
            if sub.dropped:
                continue
            if sub.role not in roles:
                continue
            if user_scope is not None and sub.user_id != user_scope:
                continue
            self._deliver(
                sub,
                wire_bytes,
                publisher_loop=publisher_loop,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _deliver(
        sub: _Subscriber,
        wire_bytes: bytes,
        *,
        publisher_loop: asyncio.AbstractEventLoop | None,
        workspace_id: str,
    ) -> None:
        """Push ``wire_bytes`` to ``sub.queue`` safely across loops.

        Same-loop path: plain ``put_nowait``; a full queue raises
        :class:`asyncio.QueueFull` which we translate to a dropped
        subscriber. Cross-loop path: hop via
        ``call_soon_threadsafe`` so we never touch the queue from
        the wrong thread (``asyncio.Queue`` is not thread-safe).

        A ``QueueFull`` raised on the subscriber's loop still marks
        the client dropped on ours — the closure captures ``sub`` so
        the flag lives on the subscriber record, not on a local.
        """
        if publisher_loop is sub.loop:
            try:
                sub.queue.put_nowait(wire_bytes)
            except asyncio.QueueFull:
                sub.dropped = True
                _log.warning(
                    "sse client dropped (queue full)",
                    extra={
                        "event": "sse.client_dropped",
                        "workspace_id": workspace_id,
                        "user_id": sub.user_id,
                    },
                )
            return

        def _put() -> None:
            try:
                sub.queue.put_nowait(wire_bytes)
            except asyncio.QueueFull:
                sub.dropped = True
                _log.warning(
                    "sse client dropped (queue full)",
                    extra={
                        "event": "sse.client_dropped",
                        "workspace_id": workspace_id,
                        "user_id": sub.user_id,
                    },
                )

        try:
            sub.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # Subscriber's loop has closed (client's lifespan ended
            # mid-publish). Treat as dropped — the handler loop's
            # ``finally`` will unsubscribe anyway.
            sub.dropped = True

    def subscribe(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: EventRole,
    ) -> _Subscriber:
        """Register a new subscriber for ``workspace_id``.

        Returns the :class:`_Subscriber` record the handler uses to
        pull frames from ``.queue``. The caller is responsible for
        pairing this with :meth:`unsubscribe` on handler exit.
        """
        # Record the loop the subscriber lives on so a cross-thread
        # publisher (the bus is sync and may fire from a worker
        # thread) can schedule the ``put_nowait`` via
        # ``call_soon_threadsafe`` without racing the queue's
        # non-thread-safe internals.
        sub = _Subscriber(
            user_id=user_id,
            role=role,
            queue=asyncio.Queue(maxsize=MAX_CLIENT_QUEUE),
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            state = self._state_for(workspace_id)
            state.subscribers.add(sub)
        return sub

    def unsubscribe(self, *, workspace_id: str, subscriber: _Subscriber) -> None:
        """Remove ``subscriber``. Idempotent — no-op if already gone."""
        with self._lock:
            state = self._workspaces.get(workspace_id)
            if state is None:
                return
            state.subscribers.discard(subscriber)

    def replay_since(
        self,
        *,
        workspace_id: str,
        last_event_id: _ParsedLastEventId,
        role: EventRole,
        user_id: str,
    ) -> Iterable[bytes]:
        """Return buffered frames newer than the client's ``Last-Event-ID``.

        Applies the same role + user-scope filter the live fan-out
        uses — a client that reconnects with a stale
        ``Last-Event-ID`` must not see an event kind they weren't
        authorised to see on the first connection.

        Stream-token handling:

        * Token missing (fresh client with no Last-Event-ID) →
          ``seq = 0`` and we return the full buffer after filtering.
        * Token matches ours → the client is resuming the same process
          instance. We replay frames with ``event_id > seq``.
        * Token differs from ours → the server instance has been
          replaced (restart, fixture swap) since the client last
          connected. The sequence the client cites cannot be compared
          to ours because our sequence restarted at zero, so we
          replay every frame in the current buffer (``seq = 0``) and
          let the SPA's on-reconnect refetch (§14) paper over the gap
          the restart produced. Refusing to replay would silently
          shadow up to 60 s of events whose fresh sequence numbers
          fall below the stale reference.
        """
        seq_cutoff = _effective_replay_seq(last_event_id, self._stream_token)
        with self._lock:
            state = self._workspaces.get(workspace_id)
            if state is None:
                return ()
            self._prune(state, now=time.monotonic())
            return tuple(
                buf.wire_bytes
                for buf in state.buffer
                if buf.event_id > seq_cutoff
                and role in buf.roles
                and (buf.user_scope is None or buf.user_scope == user_id)
            )

    @property
    def stream_token(self) -> str:
        """The per-instance token prefixed onto every emitted event id.

        Exposed for tests (and the occasional diagnostic) so a caller
        can build a valid ``Last-Event-ID`` without reaching into
        private attributes. Not part of the wire contract — clients
        only ever echo a token back; they never synthesise one.
        """
        return self._stream_token

    # ---- Internals ---------------------------------------------------

    def _state_for(self, workspace_id: str) -> _WorkspaceState:
        """Get-or-create the per-workspace state object."""
        state = self._workspaces.get(workspace_id)
        if state is None:
            state = _WorkspaceState()
            self._workspaces[workspace_id] = state
        return state

    @staticmethod
    def _prune(state: _WorkspaceState, *, now: float) -> None:
        """Drop buffered events older than :data:`REPLAY_WINDOW_S`."""
        cutoff = now - REPLAY_WINDOW_S
        buf = state.buffer
        while buf and buf[0].emitted_at_monotonic < cutoff:
            buf.popleft()


# ---------------------------------------------------------------------------
# Frame encoding + invalidation contract
# ---------------------------------------------------------------------------


def _format_sse_frame(*, event_id: str, kind: str, payload: dict[str, Any]) -> bytes:
    """Encode a single SSE frame per the text/event-stream grammar.

    Shape::

        id: <event_id>
        event: <kind>
        data: <json>
        \n

    ``event_id`` is always the composite ``<stream_token>-<seq>``
    string; the stream-token prefix lets a reconnecting client detect
    that the server instance they last talked to has been replaced
    (process restart, test fixture swap) and re-enter the stream from
    zero rather than silently skipping events whose fresh sequence
    numbers fall below the stale ``Last-Event-ID`` they cached.

    No ``retry:`` here — that lives on the connection's first packet
    (see :func:`_stream_events`) so replay frames don't fight the
    reconnection-delay contract.
    """
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return (f"id: {event_id}\nevent: {kind}\ndata: {body}\n\n").encode()


# Default TanStack Query invalidation map. Keyed by event kind; each
# value is a list of cache-key prefixes the SPA should invalidate. The
# SPA joins them with the active workspace slug
# (``['w', slug, ...prefix]``) — see §14 "Workspace-scoped query
# keys". Kept co-located with the SSE transport because it is the
# canonical consumer; a separate ``invalidations.py`` module would
# only add one more file to cross-reference.
#
# An event not listed here forwards an empty ``invalidates`` array;
# the SPA then falls back to whatever ad-hoc handler it has
# registered for the kind (agent.* events, for example, drive chat
# state directly rather than invalidating a query).
#
# TODO(transport): once the map grows past a screenful (likely when
# the agent.* and llm.* kinds land with the agent runtime), move this
# to a data file (e.g. ``app/api/transport/invalidations.py`` or a
# JSON under ``docs/specs/``) so the SPA can import the same source
# rather than tracking a parallel map. For now every entry is one
# line so the overhead of another module is not worth the round-trip.
_INVALIDATIONS: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "task.created": (("tasks",),),
    "task.assigned": (("tasks",),),
    "task.updated": (("tasks",),),
    "task.completed": (("tasks",),),
    "task.overdue": (("tasks",),),
    "stay.upcoming": (("stays",),),
    "expense.approved": (("expenses",),),
    "shift.ended": (("shifts",), ("my-schedule",)),
    "time.shift.changed": (("shifts",), ("my-schedule",)),
    "chat.message.sent": (("chat", "channels"),),
    "chat.message.received": (("chat", "channels"),),
    # Bell-menu unread count + the notification list. Both query keys
    # are per-recipient; the event is user-scoped so only the
    # addressee's tabs receive it — the invalidation fires against
    # their own cache entry and does not waste work on sibling tabs
    # in other browsers logged into the same workspace.
    "notification.created": (("notifications", "unread"), ("notifications",)),
}


def _roles_for_event(
    event: Event,
    *,
    kind: str,
    default: tuple[EventRole, ...],
) -> tuple[EventRole, ...]:
    if kind not in {"chat.message.sent", "chat.message.received"}:
        return default
    channel_kind = getattr(event, "channel_kind", None)
    if channel_kind == "staff":
        return ("manager", "worker")
    return ("manager",)


def _default_invalidates(kind: str) -> list[list[str]]:
    """Return the JSON-friendly invalidations array for ``kind``.

    Shape: ``[[segment, …], …]`` — one list of segments per cache
    entry to invalidate. Empty list when the kind has no default
    mapping; the SPA then handles the event by name.
    """
    entries = _INVALIDATIONS.get(kind, ())
    return [list(prefix) for prefix in entries]


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------


default_fanout: SSEFanOut = SSEFanOut()


# One-shot flag so the module binds to the default bus exactly once,
# lazily on first SSE handler invocation. A module-import-time bind
# would run during ``from app.api.transport import sse`` inside the
# app factory, which would couple every test that imports
# ``create_app`` to the global bus — forcing each test to reset the
# bus or live with leaked subscriptions. Lazy binding keeps
# "imported" and "wired" separable.
_bus_bound: bool = False
# Guard the check-and-set so two concurrent first requests can't
# both enter the bind path and double-register every handler.
_bus_bind_lock: threading.Lock = threading.Lock()


def _ensure_bus_binding() -> None:
    """Bind :data:`default_fanout` to the default event bus once."""
    global _bus_bound
    # Fast path: already bound. No lock needed — a stale ``False``
    # read just falls into the slow path where the lock re-checks.
    if _bus_bound:
        return
    with _bus_bind_lock:
        if _bus_bound:
            return
        default_fanout.bind_to_bus(default_bus)
        _bus_bound = True


def _reset_for_tests() -> None:
    """Drop the default fanout's state. Used only from tests."""
    global _bus_bound, default_fanout
    default_fanout = SSEFanOut()
    _bus_bound = False


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _extract_role(ctx: WorkspaceContext) -> EventRole:
    """Return the effective role the fan-out should filter against.

    The spec's role allowlist is workspace-grant-role based. The
    owner flag on :class:`WorkspaceContext` is ignored here because
    ownership is modelled as ``manager + owner-membership`` in §05 —
    an owner is always a manager, and any event manager-visible is
    owner-visible too. If a future event kind needs to gate on
    "owners only, not every manager", we add a discriminator on the
    allowlist tuple rather than a second role enum.

    Future: see cd-yj4k for the deployment-admin (``/admin/events``)
    twin. That endpoint is NOT mounted from this module; the role
    filter here is purposely workspace-scoped only.
    """
    return ctx.actor_grant_role


def _parse_last_event_id(header_value: str | None) -> _ParsedLastEventId:
    """Parse the ``Last-Event-ID`` header.

    Accepted shapes:

    * ``<stream_token>-<seq>`` — the composite id this transport
      emits. Any ``-``-containing value splits at the **last** ``-``
      so a token that itself contained a hyphen (our tokens are hex,
      they don't, but a future-proofing nod) wouldn't cause parsing
      to fail.
    * A bare positive integer — either an id from an older
      pre-stream-token server build echoed back after a deploy, or a
      misbehaving proxy trimming the prefix. We honour it as
      ``(stream_token=None, seq=N)`` so the fanout replays from zero
      (see :func:`_effective_replay_seq`).

    Missing, empty, oversized (>:data:`_MAX_LAST_EVENT_ID_LEN`
    characters), or malformed → ``(None, 0)``. We never 400 a client
    that has already reconnected — rejecting is a busy-loop
    generator under the SSE reconnection contract.
    """
    if not header_value:
        return _ParsedLastEventId(stream_token=None, seq=0)
    stripped = header_value.strip()
    if not stripped or len(stripped) > _MAX_LAST_EVENT_ID_LEN:
        return _ParsedLastEventId(stream_token=None, seq=0)

    token: str | None = None
    seq_str = stripped
    if "-" in stripped:
        token_part, _, seq_part = stripped.rpartition("-")
        if token_part and seq_part:
            token = token_part
            seq_str = seq_part

    try:
        seq = int(seq_str)
    except ValueError:
        return _ParsedLastEventId(stream_token=None, seq=0)
    # Negative / non-positive values have no meaning on a monotonic
    # counter; floats / scientific notation are not ``int``-parseable
    # anyway and fell to the ``except`` above.
    if seq < 0:
        return _ParsedLastEventId(stream_token=None, seq=0)
    return _ParsedLastEventId(stream_token=token, seq=seq)


async def _stream_events(
    *,
    request: Request,
    fanout: SSEFanOut,
    workspace_id: str,
    user_id: str,
    role: EventRole,
    last_event_id: _ParsedLastEventId,
    heartbeat_interval: float,
) -> AsyncGenerator[bytes]:
    """Yield SSE frames for one client connection.

    Three loops interleave:

    1. **Initial frame** — ``retry: 3000`` so a client that drops
       reconnects fast. No ``id:`` on this one; it's metadata, not an
       event.
    2. **Replay** — every buffered frame newer than
       ``last_event_id`` that passes the role + user-scope filter.
    3. **Live stream** — a ``queue.get()`` race against a heartbeat
       sleep. Whichever wakes first wins; a drop flag short-circuits
       the loop and emits ``event: dropped`` before closing.

    A ``CancelledError`` from the framework (client hangup,
    ``asyncio.shield`` revoked) is the normal disconnect path; we
    re-raise after unsubscribing so Starlette records the close
    cleanly.
    """
    subscriber = fanout.subscribe(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role,
    )
    try:
        # Initial retry hint — SSE clients use it as the reconnect
        # delay when the socket drops. Plain comment-less frame; spec
        # says "retry:" is its own line with no accompanying data.
        yield f"retry: {_RECONNECT_MS}\n\n".encode()

        for frame in fanout.replay_since(
            workspace_id=workspace_id,
            last_event_id=last_event_id,
            role=role,
            user_id=user_id,
        ):
            yield frame

        while True:
            if subscriber.dropped:
                yield b"event: dropped\ndata: {}\n\n"
                return
            if await _client_disconnected(request):
                return

            try:
                frame = await asyncio.wait_for(
                    subscriber.queue.get(),
                    timeout=heartbeat_interval,
                )
            except TimeoutError:
                # No event for the heartbeat window — send a comment
                # to keep the connection warm past proxy idle
                # timeouts. This path is also what notices a
                # ``dropped`` flag set while the generator was
                # suspended: :meth:`SSEFanOut._deliver` never pushes
                # a sentinel frame, so if the queue had no room for
                # the real frame (dropped path) we block on
                # ``queue.get()`` until the heartbeat wakes us and
                # the ``if subscriber.dropped`` check at the top of
                # the next iteration fires. Worst case: the client
                # sees the ``dropped`` frame a full heartbeat window
                # late (≤15 s), which is acceptable — the socket is
                # about to close anyway.
                yield b": keepalive\n\n"
                continue

            # Re-check drop after wake: publish may have flagged the
            # subscriber between our put-wake and this read.
            yield frame
            if subscriber.dropped:
                yield b"event: dropped\ndata: {}\n\n"
                return
    finally:
        fanout.unsubscribe(workspace_id=workspace_id, subscriber=subscriber)


async def _client_disconnected(request: Request) -> bool:
    """Return ``True`` if the client has hung up.

    Starlette surfaces disconnects through ``request.is_disconnected()``
    which polls the receive channel without blocking. Wrapped so the
    stream generator can short-circuit cleanly without a TCP write
    error being the thing that tells us to stop.
    """
    try:
        return await request.is_disconnected()
    except RuntimeError:
        # Some test harnesses invalidate the receive channel after
        # the handler returns; treat that as "disconnected".
        return True


@router.get(
    "/events",
    include_in_schema=True,
    summary="Workspace-scoped Server-Sent Events stream",
    operation_id="transport.events",
)
async def events(
    request: Request,
    ctx: _Ctx,
) -> Response:
    """Subscribe to the workspace's SSE stream.

    Filters by:

    * the caller's :attr:`WorkspaceContext.actor_grant_role` against
      the event class's ``allowed_roles`` tuple;
    * the caller's :attr:`WorkspaceContext.actor_id` against the
      event's ``actor_user_id`` for user-scoped events.

    The ``Last-Event-ID`` header (standard SSE reconnection seam)
    replays every buffered event newer than the id, subject to the
    same filter. Heartbeat comment every 15 s keeps the connection
    alive across idle stretches.
    """
    _ensure_bus_binding()

    fanout = default_fanout
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
    role = _extract_role(ctx)

    generator = _stream_events(
        request=request,
        fanout=fanout,
        workspace_id=ctx.workspace_id,
        user_id=ctx.actor_id,
        role=role,
        last_event_id=last_event_id,
        heartbeat_interval=HEARTBEAT_INTERVAL_S,
    )

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # Nginx / Cloudflare buffer responses aggressively by default;
        # this opt-out is the documented way to flush SSE frames
        # through without batching. Uvicorn's own
        # :class:`StreamingResponse` path already streams
        # byte-for-byte (it does not buffer the async iterator before
        # sending), so the ``text/event-stream`` frames reach the
        # browser as soon as we yield them — the header here matters
        # only for downstream reverse proxies (the production dev
        # stack fronts uvicorn with Pangolin, which respects
        # ``X-Accel-Buffering: no``).
        # httpx's ASGITransport (used by the test harness) *does*
        # buffer the entire response before surfacing it, which is
        # why the unit + integration suites drive the generator
        # directly rather than going through ``TestClient`` — see
        # the header comments on ``tests/api/transport/test_sse.py``
        # and ``tests/integration/api/transport/test_sse_integration.py``.
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=headers,
    )
