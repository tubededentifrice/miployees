"""Unit tests for :mod:`app.api.transport.sse`.

Exercises the fan-out filtering, role/user-scope gates, heartbeat
cadence, ``Last-Event-ID`` replay, and backpressure-drop paths
against a freshly-minted :class:`SSEFanOut` and a handcrafted
``FastAPI`` app that mounts the router directly — the real tenancy
middleware stays out of scope for these cases, which are about
transport mechanics.

See ``docs/specs/11-llm-and-agents.md`` §"Agent turn lifecycle",
§"Inline approval UX"; ``docs/specs/14-web-frontend.md``
§"SSE-driven invalidation"; ``docs/specs/17-testing-quality.md``
§"Integration".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from app.api.transport import sse as sse_mod
from app.api.transport.sse import (
    MAX_CLIENT_QUEUE,
    SSEFanOut,
    _default_invalidates,
    _parse_last_event_id,
    _ParsedLastEventId,
    _stream_events,
)
from app.events import bus as default_bus
from app.events import registry as registry_module
from app.events.registry import ALL_ROLES, Event, register
from app.events.types import TaskCompleted, TaskCreated, TaskSkipped, TaskUpdated


def _fresh_id() -> _ParsedLastEventId:
    """Stand-in for a first-connection client (no ``Last-Event-ID``).

    Returning the dataclass inline once per call reads as "this test
    is starting a fresh subscription" wherever it appears.
    """
    return _ParsedLastEventId(stream_token=None, seq=0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _utc() -> datetime:
    return datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fresh_fanout(monkeypatch: pytest.MonkeyPatch) -> Iterator[SSEFanOut]:
    """Return a pristine :class:`SSEFanOut` and swap the module default.

    The handler reads :data:`sse_mod.default_fanout` lazily, so a
    test that points that module global at a fresh instance isolates
    its state from any other test. Also disables the lazy bus
    binding so the fanout stays empty.
    """
    original = sse_mod.default_fanout
    original_bound = sse_mod._bus_bound
    new = SSEFanOut()
    sse_mod.default_fanout = new
    sse_mod._bus_bound = True  # skip bind-to-bus; tests publish directly
    try:
        yield new
    finally:
        sse_mod.default_fanout = original
        sse_mod._bus_bound = original_bound


@pytest.fixture
def isolate_bus() -> Iterator[None]:
    """Snapshot and restore the registry + default bus subscriber state."""
    snapshot = dict(registry_module._REGISTRY)
    try:
        yield
    finally:
        default_bus._reset_for_tests()
        with registry_module._lock:
            registry_module._REGISTRY.clear()
            registry_module._REGISTRY.update(snapshot)


def _fake_request(disconnected: bool = False) -> MagicMock:
    """Return a request double whose ``is_disconnected`` returns ``False``.

    The SSE handler only reads ``request.is_disconnected()``; a
    :class:`MagicMock` that yields an awaitable result is sufficient
    and keeps the test free of Starlette's request-lifecycle plumbing.
    The ``disconnected`` flag is honoured on every call so a test can
    flip the knob mid-stream to exit the generator cleanly.
    """
    state = {"disconnected": disconnected}

    async def _is_disc() -> bool:
        return state["disconnected"]

    req = MagicMock()
    req.is_disconnected = _is_disc
    req._state = state  # escape hatch for tests that flip mid-stream
    return req


async def _collect_next_frame(
    gen: AsyncIterator[bytes], *, timeout: float = 1.0
) -> bytes:
    """Await one yielded chunk from ``gen`` with a bound timeout.

    The generator yields one frame per ``await`` (retry line, replay
    frame, or live frame); pulling one at a time lets a test assert
    what arrived without fighting the frame boundary.
    """
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


# ---------------------------------------------------------------------------
# Helpers — frame parsing
# ---------------------------------------------------------------------------


def _parse_frames(raw: bytes) -> list[dict[str, str]]:
    """Parse one or more SSE frames into a list of dicts.

    Each dict has whatever fields the frame carried: ``id``, ``event``,
    ``data``, ``retry``. Comment lines (`: …`) map to ``comment``.
    """
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if line == "":
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith(":"):
            current.setdefault("comment", "")
            current["comment"] += line[1:].strip()
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip()
        value = value.lstrip(" ")
        current[field] = value
    if current:
        frames.append(current)
    return frames


# ---------------------------------------------------------------------------
# Event classes used by the tests
# ---------------------------------------------------------------------------


class ManagerOnlyEvent(Event):
    """Event the transport should deliver only to managers."""

    name: ClassVar[str] = "test.manager_only"
    allowed_roles: ClassVar[tuple[str, ...]] = ("manager",)  # type: ignore[assignment]

    detail: str


class UserScopedEvent(Event):
    """Event restricted to the matching ``actor_user_id``."""

    name: ClassVar[str] = "test.user_scoped"
    user_scoped: ClassVar[bool] = True

    actor_user_id: str
    detail: str


# ---------------------------------------------------------------------------
# _parse_last_event_id
# ---------------------------------------------------------------------------


class TestParseLastEventId:
    def test_missing_returns_fresh(self) -> None:
        fresh = _ParsedLastEventId(stream_token=None, seq=0)
        assert _parse_last_event_id(None) == fresh
        assert _parse_last_event_id("") == fresh
        assert _parse_last_event_id("   ") == fresh

    def test_bare_int_is_honoured_without_a_stream_token(self) -> None:
        """A pre-stream-token build's ids (bare int) fall back to seq only.

        ``replay_since`` then treats the missing token as a mismatch
        and replays the current buffer from 0 — the safer default
        than silently respecting a stale counter.
        """
        assert _parse_last_event_id("42") == _ParsedLastEventId(
            stream_token=None, seq=42
        )
        assert _parse_last_event_id("  7 ") == _ParsedLastEventId(
            stream_token=None, seq=7
        )

    def test_composite_id_splits_on_last_hyphen(self) -> None:
        assert _parse_last_event_id("abc123-17") == _ParsedLastEventId(
            stream_token="abc123", seq=17
        )
        # rpartition: a hyphen inside the (future) token is tolerated.
        assert _parse_last_event_id("boot-ab-99") == _ParsedLastEventId(
            stream_token="boot-ab", seq=99
        )

    def test_junk_returns_fresh(self) -> None:
        fresh = _ParsedLastEventId(stream_token=None, seq=0)
        assert _parse_last_event_id("nope") == fresh
        assert _parse_last_event_id("12.5") == fresh
        assert _parse_last_event_id("-") == fresh  # empty token + empty seq
        # Composite with non-int sequence.
        assert _parse_last_event_id("abc-nope") == fresh

    def test_negative_seq_is_rejected(self) -> None:
        """A bare negative counter has no meaning on a monotonic sequence.

        We coerce to the same "fresh client" default rather than let
        a negative value act as "replay everything ever published"
        (which would happen if we honoured it against the
        ``event_id > seq`` predicate). ``"-5"`` is parsed as an empty
        token + a negative seq, which the rejection branch handles;
        the ``rpartition`` split on ``"foo--5"`` conveniently
        produces ``("foo-", -no, 5)`` → token ``"foo-"`` + seq ``5``
        (a positive reconnect, not a negative one) so that's a
        separate test.
        """
        assert _parse_last_event_id("-5") == _ParsedLastEventId(
            stream_token=None, seq=0
        )
        assert _parse_last_event_id("   -42 ") == _ParsedLastEventId(
            stream_token=None, seq=0
        )

    def test_oversized_header_is_refused(self) -> None:
        """A header longer than :data:`_MAX_LAST_EVENT_ID_LEN` is junk.

        SSE clients only ever echo an id we previously emitted, and
        ours are ≤32 chars. A much longer value is a probe or a
        broken proxy; don't spend parse time on it.
        """
        huge = "a" * 1000 + "-1"
        fresh = _ParsedLastEventId(stream_token=None, seq=0)
        assert _parse_last_event_id(huge) == fresh

    def test_numeric_overflow_still_parses(self) -> None:
        """Python ints are unbounded; a very large seq is legal.

        The server's own counter never reaches the billions in a
        60 s replay window, but a client echoing back a 10-digit
        value is still a valid reconnect — we accept it and let the
        stream-token mismatch (or ``seq > current_next_id``) lead to
        an empty replay.
        """
        assert _parse_last_event_id("token-9999999999999") == _ParsedLastEventId(
            stream_token="token", seq=9999999999999
        )


# ---------------------------------------------------------------------------
# _default_invalidates
# ---------------------------------------------------------------------------


class TestDefaultInvalidates:
    def test_known_kind(self) -> None:
        assert _default_invalidates("task.created") == [["tasks"]]
        assert _default_invalidates("shift.ended") == [["shifts"], ["my-schedule"]]

    def test_unknown_kind_empty(self) -> None:
        assert _default_invalidates("agent.turn.started") == []


# ---------------------------------------------------------------------------
# SSEFanOut unit tests (pure, no HTTP)
# ---------------------------------------------------------------------------


class TestFanOutDirect:
    async def test_publish_to_two_matching_subscribers_delivers_both(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        sub_a = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_a", role="manager"
        )
        sub_b = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_a", role="manager"
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        frame_a = await asyncio.wait_for(sub_a.queue.get(), 0.5)
        frame_b = await asyncio.wait_for(sub_b.queue.get(), 0.5)
        assert frame_a == frame_b
        parsed = _parse_frames(frame_a)[0]
        assert parsed["event"] == "task.created"
        # Composite id is ``<stream_token>-<seq>`` — the stream token
        # lets a client that reconnects after a restart notice the
        # counter has been reset and avoid shadowing fresh events
        # with a stale ``Last-Event-ID``. See
        # :class:`~app.api.transport.sse._ParsedLastEventId`.
        assert parsed["id"] == f"{fresh_fanout.stream_token}-1"
        data = json.loads(parsed["data"])
        assert data["task_id"] == "t_1"
        assert data["kind"] == "task.created"
        assert data["invalidates"] == [["tasks"]]

    async def test_role_filter_rejects_disallowed_role(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        manager = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_m", role="manager"
        )
        worker = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_w", role="worker"
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="test.manager_only",
            roles=("manager",),
            user_scope=None,
            payload={"detail": "x"},
        )
        await asyncio.wait_for(manager.queue.get(), 0.5)
        assert worker.queue.empty()

    async def test_user_scope_filter_keeps_other_users_out(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        alice = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_alice", role="worker"
        )
        bob = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_bob", role="worker"
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="agent.action.pending",
            roles=ALL_ROLES,
            user_scope="u_alice",
            payload={"actor_user_id": "u_alice", "action_id": "a_1"},
        )
        await asyncio.wait_for(alice.queue.get(), 0.5)
        assert bob.queue.empty()

    async def test_workspace_isolation(self, fresh_fanout: SSEFanOut) -> None:
        in_ws1 = fresh_fanout.subscribe(
            workspace_id="ws_1", user_id="u_a", role="manager"
        )
        in_ws2 = fresh_fanout.subscribe(
            workspace_id="ws_2", user_id="u_a", role="manager"
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        await asyncio.wait_for(in_ws1.queue.get(), 0.5)
        assert in_ws2.queue.empty()

    async def test_event_id_is_per_workspace_monotonic(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        sub = fresh_fanout.subscribe(workspace_id="ws_1", user_id="u", role="manager")
        for _ in range(3):
            fresh_fanout.publish(
                workspace_id="ws_1",
                kind="task.created",
                roles=ALL_ROLES,
                user_scope=None,
                payload={"task_id": "t"},
            )
        token = fresh_fanout.stream_token
        ids: list[str] = []
        for _ in range(3):
            raw = await asyncio.wait_for(sub.queue.get(), 0.5)
            ids.append(_parse_frames(raw)[0]["id"])
        assert ids == [f"{token}-1", f"{token}-2", f"{token}-3"]

        # Another workspace starts its own sequence at 1 (the stream
        # token is process-wide, the sequence is per-workspace).
        other = fresh_fanout.subscribe(workspace_id="ws_2", user_id="u", role="manager")
        fresh_fanout.publish(
            workspace_id="ws_2",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t"},
        )
        raw = await asyncio.wait_for(other.queue.get(), 0.5)
        assert _parse_frames(raw)[0]["id"] == f"{token}-1"

    async def test_replay_since_honours_role_and_user_scope(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        # Publish three events before anyone subscribes:
        # (1) manager-only, (2) user-scoped to alice, (3) workspace-wide.
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="test.manager_only",
            roles=("manager",),
            user_scope=None,
            payload={"detail": "m"},
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="test.user_scoped",
            roles=ALL_ROLES,
            user_scope="u_alice",
            payload={"actor_user_id": "u_alice"},
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        token = fresh_fanout.stream_token
        # Bob (worker) replaying from 0 sees only (3).
        bob_frames = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=_fresh_id(),
                role="worker",
                user_id="u_bob",
            )
        )
        assert len(bob_frames) == 1
        assert "task.created" in bob_frames[0].decode("utf-8")
        # Alice (worker) replaying from 0 sees her user-scoped + (3).
        alice_frames = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=_fresh_id(),
                role="worker",
                user_id="u_alice",
            )
        )
        assert len(alice_frames) == 2
        # Manager alice sees all three.
        manager_frames = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=_fresh_id(),
                role="manager",
                user_id="u_alice",
            )
        )
        assert len(manager_frames) == 3
        # Replay with a last-event-id skips everything already seen.
        tail = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=_ParsedLastEventId(stream_token=token, seq=2),
                role="manager",
                user_id="u_alice",
            )
        )
        assert len(tail) == 1

    async def test_replay_prunes_past_window(
        self, fresh_fanout: SSEFanOut, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Freeze time forward across the 60 s window and confirm
        # the buffer is empty on replay.
        clock = {"now": 1000.0}
        monkeypatch.setattr(sse_mod.time, "monotonic", lambda: clock["now"])

        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t"},
        )
        # Advance past the window; replay should see nothing.
        clock["now"] = 1070.0
        assert (
            list(
                fresh_fanout.replay_since(
                    workspace_id="ws_1",
                    last_event_id=_fresh_id(),
                    role="manager",
                    user_id="u",
                )
            )
            == []
        )

    async def test_backpressure_drop_flags_subscriber_and_unblocks_others(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        slow = fresh_fanout.subscribe(workspace_id="ws_1", user_id="u", role="manager")
        fast = fresh_fanout.subscribe(workspace_id="ws_1", user_id="u2", role="manager")
        # Fill both queues to the brim, drain the fast consumer
        # (simulating an attentive reader), then flood the slow one
        # past its ceiling. The fast consumer must not be flagged
        # dropped — the slow consumer's backlog is its own problem.
        for _ in range(MAX_CLIENT_QUEUE):
            fresh_fanout.publish(
                workspace_id="ws_1",
                kind="task.created",
                roles=ALL_ROLES,
                user_scope=None,
                payload={"task_id": "t"},
            )
        # Drain fast so it can keep accepting.
        for _ in range(MAX_CLIENT_QUEUE):
            fast.queue.get_nowait()
        # One more publish overflows the slow client's queue and
        # leaves fast with a single frame.
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "last"},
        )
        assert slow.dropped is True
        assert fast.dropped is False
        assert fast.queue.qsize() == 1
        assert slow.queue.qsize() == MAX_CLIENT_QUEUE


# ---------------------------------------------------------------------------
# Handler-generator tests
#
# The SSE handler's streaming body is :func:`_stream_events`. Driving it
# directly keeps the tests deterministic and avoids httpx's ASGITransport
# buffering the response end-to-end before surfacing it — which stalls any
# test that tries to read a live stream through it. This mirrors the
# pattern used in the idempotency-replay integration suite (which also
# sidesteps the ASGITransport quirk when it can).
# ---------------------------------------------------------------------------


class TestStreamEventsGenerator:
    async def test_first_frame_is_retry_hint(self, fresh_fanout: SSEFanOut) -> None:
        gen = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        first = await _collect_next_frame(gen)
        assert first == b"retry: 3000\n\n"
        await gen.aclose()

    async def test_publishing_after_subscribe_delivers_frame(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        gen = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        retry = await _collect_next_frame(gen)
        assert retry.startswith(b"retry:")
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        frame = await _collect_next_frame(gen)
        parsed = _parse_frames(frame)[0]
        assert parsed["event"] == "task.created"
        assert parsed["id"] == f"{fresh_fanout.stream_token}-1"
        await gen.aclose()

    async def test_two_tabs_same_user_receive_identical_frames(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        gen_a = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u_shared",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        gen_b = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u_shared",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        # Drain the retry-hint frame from both.
        await _collect_next_frame(gen_a)
        await _collect_next_frame(gen_b)

        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        frame_a = await _collect_next_frame(gen_a)
        frame_b = await _collect_next_frame(gen_b)
        assert frame_a == frame_b
        await gen_a.aclose()
        await gen_b.aclose()

    async def test_worker_does_not_receive_manager_only_event(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        worker = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u_w",
            role="worker",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        await _collect_next_frame(worker)  # retry

        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="test.manager_only",
            roles=("manager",),
            user_scope=None,
            payload={"detail": "top_secret_manager_data"},
        )
        # Worker-visible event lands after — we assert we see it and
        # never the manager-only one.
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        frame = await _collect_next_frame(worker)
        assert b"test.manager_only" not in frame
        assert b"top_secret_manager_data" not in frame
        assert b"task.created" in frame
        await worker.aclose()

    async def test_user_scoped_event_not_delivered_to_other_user(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        bob = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u_bob",
            role="worker",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        await _collect_next_frame(bob)  # retry

        # Targeted at alice, not bob. Bob must never see this frame.
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="agent.action.pending",
            roles=ALL_ROLES,
            user_scope="u_alice",
            payload={"actor_user_id": "u_alice", "action_id": "a_1"},
        )
        # Workspace-wide ping so the generator has something to yield.
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        frame = await _collect_next_frame(bob)
        assert b"agent.action.pending" not in frame
        assert b"a_1" not in frame
        assert b"task.created" in frame
        await bob.aclose()

    async def test_last_event_id_replay_skips_seen_events(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_2"},
        )
        gen = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            # Client previously saw ``<token>-1``; the resume path
            # must skip id 1 and hand back only id 2.
            role="manager",
            last_event_id=_ParsedLastEventId(
                stream_token=fresh_fanout.stream_token, seq=1
            ),
            heartbeat_interval=10.0,
        )
        await _collect_next_frame(gen)  # retry
        # Replay: only id 2 should come back.
        replay = await _collect_next_frame(gen)
        parsed = _parse_frames(replay)[0]
        assert parsed["id"] == f"{fresh_fanout.stream_token}-2"
        assert "t_2" in parsed["data"]
        await gen.aclose()

    async def test_heartbeat_fires_on_idle_stream(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        gen = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=0.05,
        )
        await _collect_next_frame(gen)  # retry hint
        hb = await _collect_next_frame(gen, timeout=1.0)
        assert hb == b": keepalive\n\n"
        # A second heartbeat on a still-idle connection proves the
        # cadence keeps the socket warm past a 60 s proxy idle.
        hb2 = await _collect_next_frame(gen, timeout=1.0)
        assert hb2 == b": keepalive\n\n"
        await gen.aclose()

    async def test_backpressure_drops_with_dropped_frame(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        gen = _stream_events(
            request=_fake_request(),
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        await _collect_next_frame(gen)  # retry

        # Flood past MAX_CLIENT_QUEUE without the generator draining.
        # (The generator has yielded the retry frame but hasn't
        # awaited ``queue.get`` yet — it's suspended between yields,
        # so nothing is consumed from the queue until the next
        # ``anext``.)
        for _ in range(MAX_CLIENT_QUEUE + 5):
            fresh_fanout.publish(
                workspace_id="ws_1",
                kind="task.created",
                roles=ALL_ROLES,
                user_scope=None,
                payload={"task_id": "t"},
            )
        # The next awaited frame will drain one buffered event from
        # the queue; we may see real task.created frames first. Keep
        # pulling until we observe the ``dropped`` frame.
        seen_dropped = False
        for _ in range(MAX_CLIENT_QUEUE + 10):
            frame = await _collect_next_frame(gen, timeout=1.0)
            if frame.startswith(b"event: dropped"):
                seen_dropped = True
                break
        assert seen_dropped is True

    async def test_client_disconnect_exits_generator(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        request = _fake_request()
        gen = _stream_events(
            request=request,
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=0.05,
        )
        await _collect_next_frame(gen)  # retry

        # Flip the disconnect flag; the next heartbeat tick should
        # then bring the generator to a clean exit.
        request._state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            # Heartbeat wakes, sees disconnect, returns.
            for _ in range(3):
                await _collect_next_frame(gen, timeout=1.0)


# ---------------------------------------------------------------------------
# Event-bus binding — registry + forward path
# ---------------------------------------------------------------------------


class TestBindToBus:
    async def test_forward_serialises_event_into_frame(
        self,
        fresh_fanout: SSEFanOut,
        isolate_bus: None,
    ) -> None:
        from app.events.bus import EventBus

        local_bus = EventBus()
        fresh_fanout.bind_to_bus(local_bus)
        sub = fresh_fanout.subscribe(
            workspace_id="01HX00000000000000000WS0000",
            user_id="01HX00000000000000000USR000",
            role="manager",
        )
        local_bus.publish(
            TaskCreated(
                workspace_id="01HX00000000000000000WS0000",
                actor_id="01HX00000000000000000USR000",
                correlation_id="01HX00000000000000000COR000",
                occurred_at=_utc(),
                task_id="01HX00000000000000000TSK000",
            )
        )
        raw = await asyncio.wait_for(sub.queue.get(), 0.5)
        parsed = _parse_frames(raw)[0]
        assert parsed["event"] == "task.created"
        data = json.loads(parsed["data"])
        assert data["task_id"] == "01HX00000000000000000TSK000"
        assert data["kind"] == "task.created"
        assert data["workspace_id"] == "01HX00000000000000000WS0000"
        # Timestamp is serialised as ISO-8601 string.
        assert data["occurred_at"].startswith("2026-04-24")


# ---------------------------------------------------------------------------
# Wire-shape contract for task.* events (cd-m0hz)
#
# The SPA dispatcher reads ``payload.task_id`` (and never a hand-shaped
# ``payload.task`` object). Pin the canonical wire shape on the
# server side so a future drift — adding a rendered ``Task`` field, for
# instance — fails this test before it slips past review and the SPA
# breaks silently. Spec refs:
#
# * ``docs/specs/06-tasks-and-scheduling.md`` — task event contract.
# * ``docs/specs/14-web-frontend.md`` §"SSE-driven invalidation".
# * Beads cd-m0hz — surfaced during cd-43wv selfreview.
# ---------------------------------------------------------------------------


class TestTaskEventWireShape:
    """Pin the JSON payload shape for task.{updated,completed,skipped}.

    The dispatcher consumes ``{task_id, ...}`` only — never a rendered
    ``Task``. Subscribers re-fetch via REST under per-row authz.
    """

    _WS = "01HX00000000000000000WS0000"
    _ACTOR = "01HX00000000000000000USR000"
    _CORR = "01HX00000000000000000COR000"
    _TASK = "01HX00000000000000000TSK000"

    async def test_task_updated_payload_keys_are_minimal(
        self,
        fresh_fanout: SSEFanOut,
        isolate_bus: None,
    ) -> None:
        """task.updated → ``{kind, workspace_id, task_id, changed_fields,
        invalidates, actor_id, correlation_id, occurred_at}`` — never a
        rendered ``task`` object.

        The frame envelope (``actor_id`` + ``correlation_id`` +
        ``occurred_at``) is added by the base :class:`Event` model_dump;
        the SSE transport stamps ``kind``, ``workspace_id``, and
        ``invalidates`` on top. The point of this test is what is
        **not** there — no ``task`` field.
        """
        from app.events.bus import EventBus

        local_bus = EventBus()
        fresh_fanout.bind_to_bus(local_bus)
        sub = fresh_fanout.subscribe(
            workspace_id=self._WS, user_id=self._ACTOR, role="manager"
        )
        local_bus.publish(
            TaskUpdated(
                workspace_id=self._WS,
                actor_id=self._ACTOR,
                correlation_id=self._CORR,
                occurred_at=_utc(),
                task_id=self._TASK,
                changed_fields=("title", "scheduled_for_local"),
            )
        )
        raw = await asyncio.wait_for(sub.queue.get(), 0.5)
        data = json.loads(_parse_frames(raw)[0]["data"])

        # The keys the SPA dispatcher reads.
        assert data["kind"] == "task.updated"
        assert data["workspace_id"] == self._WS
        assert data["task_id"] == self._TASK
        assert data["changed_fields"] == ["title", "scheduled_for_local"]
        # The transport stamps the default invalidation map for the
        # kind; ``task.updated`` invalidates the workspace tasks list.
        assert data["invalidates"] == [["tasks"]]
        # Crucially: no rendered ``Task`` field on the wire. The SPA
        # treats the event as a pure invalidation signal.
        assert "task" not in data

    async def test_task_completed_payload_has_no_task_field(
        self,
        fresh_fanout: SSEFanOut,
        isolate_bus: None,
    ) -> None:
        """task.completed → ``{task_id, completed_by, ...}`` — no ``task``."""
        from app.events.bus import EventBus

        local_bus = EventBus()
        fresh_fanout.bind_to_bus(local_bus)
        sub = fresh_fanout.subscribe(
            workspace_id=self._WS, user_id=self._ACTOR, role="manager"
        )
        local_bus.publish(
            TaskCompleted(
                workspace_id=self._WS,
                actor_id=self._ACTOR,
                correlation_id=self._CORR,
                occurred_at=_utc(),
                task_id=self._TASK,
                completed_by=self._ACTOR,
            )
        )
        raw = await asyncio.wait_for(sub.queue.get(), 0.5)
        data = json.loads(_parse_frames(raw)[0]["data"])
        assert data["kind"] == "task.completed"
        assert data["task_id"] == self._TASK
        assert data["completed_by"] == self._ACTOR
        assert "task" not in data

    async def test_task_skipped_payload_has_no_task_field(
        self,
        fresh_fanout: SSEFanOut,
        isolate_bus: None,
    ) -> None:
        """task.skipped → ``{task_id, skipped_by, reason, ...}`` — no ``task``."""
        from app.events.bus import EventBus

        local_bus = EventBus()
        fresh_fanout.bind_to_bus(local_bus)
        sub = fresh_fanout.subscribe(
            workspace_id=self._WS, user_id=self._ACTOR, role="manager"
        )
        local_bus.publish(
            TaskSkipped(
                workspace_id=self._WS,
                actor_id=self._ACTOR,
                correlation_id=self._CORR,
                occurred_at=_utc(),
                task_id=self._TASK,
                skipped_by=self._ACTOR,
                reason="guest_left_early",
            )
        )
        raw = await asyncio.wait_for(sub.queue.get(), 0.5)
        data = json.loads(_parse_frames(raw)[0]["data"])
        assert data["kind"] == "task.skipped"
        assert data["task_id"] == self._TASK
        assert data["skipped_by"] == self._ACTOR
        assert data["reason"] == "guest_left_early"
        assert "task" not in data


# ---------------------------------------------------------------------------
# Event-registry validation
# ---------------------------------------------------------------------------


class TestRegistryValidation:
    def test_user_scoped_requires_actor_user_id(self, isolate_bus: None) -> None:
        with pytest.raises(ValueError, match="actor_user_id"):

            @register
            class Bad(Event):
                name: ClassVar[str] = "test.bad_user_scoped"
                user_scoped: ClassVar[bool] = True

    def test_allowed_roles_cannot_be_empty(self, isolate_bus: None) -> None:
        with pytest.raises(ValueError, match="allowed_roles"):

            @register
            class Bad(Event):
                name: ClassVar[str] = "test.bad_allowed_roles"
                allowed_roles: ClassVar[tuple[str, ...]] = ()  # type: ignore[assignment]

    def test_user_scoped_with_actor_user_id_registers_cleanly(
        self, isolate_bus: None
    ) -> None:
        """Positive twin — a well-formed ``user_scoped`` event registers.

        Locks in the happy path so a future refactor of
        ``register()`` can't silently break ``user_scoped`` events.
        """

        @register
        class Ok(Event):
            name: ClassVar[str] = "test.ok_user_scoped"
            user_scoped: ClassVar[bool] = True

            actor_user_id: str

        assert Ok.user_scoped is True
        assert "actor_user_id" in Ok.model_fields


# ---------------------------------------------------------------------------
# Stream-token handling across a restart-like boundary
# ---------------------------------------------------------------------------


class TestStreamTokenResumeSemantics:
    """Protect the "process restart / fixture swap" resume path.

    A client that reconnects with a stale ``Last-Event-ID`` from a
    prior server instance must not silently shadow fresh events
    whose sequence numbers happen to be smaller. The stream token
    is the seam that detects the mismatch.
    """

    async def test_mismatched_token_replays_full_buffer(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        # Publish two events under the current stream token.
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_2"},
        )
        # Client reconnects citing a different (stale) token and a
        # *larger* seq than our current counter — exactly the
        # silent-skip case the token exists to prevent. We should
        # replay the whole buffer.
        stale = _ParsedLastEventId(stream_token="deadbeef", seq=999)
        frames = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=stale,
                role="manager",
                user_id="u",
            )
        )
        assert len(frames) == 2

    async def test_matched_token_honours_seq(self, fresh_fanout: SSEFanOut) -> None:
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_2"},
        )
        resume = _ParsedLastEventId(stream_token=fresh_fanout.stream_token, seq=1)
        frames = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=resume,
                role="manager",
                user_id="u",
            )
        )
        assert len(frames) == 1  # only id 2
        assert b"t_2" in frames[0]

    async def test_missing_token_with_bare_seq_replays_full_buffer(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        """A pre-stream-token client (bare int) replays from the start.

        Covers the rolling-upgrade case where a client that
        previously received a bare-int id reconnects to a
        token-aware server. We err toward over-delivery (the client
        deduplicates) rather than silent skip.
        """
        fresh_fanout.publish(
            workspace_id="ws_1",
            kind="task.created",
            roles=ALL_ROLES,
            user_scope=None,
            payload={"task_id": "t_1"},
        )
        bare = _ParsedLastEventId(stream_token=None, seq=9999)
        frames = list(
            fresh_fanout.replay_since(
                workspace_id="ws_1",
                last_event_id=bare,
                role="manager",
                user_id="u",
            )
        )
        assert len(frames) == 1


# ---------------------------------------------------------------------------
# Cross-thread publisher (the APScheduler / background-worker path)
# ---------------------------------------------------------------------------


class TestCrossThreadPublish:
    """Confirm :meth:`SSEFanOut.publish` is safe from a non-asyncio thread.

    In production the event bus can be fired from an APScheduler
    worker thread (§worker jobs). The fan-out must hop the per-
    subscriber queue write through ``call_soon_threadsafe`` — a
    plain ``put_nowait`` from the wrong thread would corrupt
    :class:`asyncio.Queue` state.
    """

    async def test_publish_from_worker_thread_reaches_subscriber(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        import threading

        sub = fresh_fanout.subscribe(workspace_id="ws_1", user_id="u", role="manager")

        def _publish() -> None:
            fresh_fanout.publish(
                workspace_id="ws_1",
                kind="task.created",
                roles=ALL_ROLES,
                user_scope=None,
                payload={"task_id": "t_1"},
            )

        worker = threading.Thread(target=_publish)
        worker.start()
        worker.join(timeout=1.0)
        assert not worker.is_alive()

        # The cross-thread hop posts the queue put via
        # ``call_soon_threadsafe``; we need to hand back to the
        # event loop for it to run the callback, which
        # ``asyncio.wait_for(queue.get, …)`` does naturally.
        frame = await asyncio.wait_for(sub.queue.get(), 1.0)
        assert b"task.created" in frame
        assert b"t_1" in frame


# ---------------------------------------------------------------------------
# Subscription cleanup on cancellation
# ---------------------------------------------------------------------------


class TestSubscriptionCleanup:
    """The handler's ``finally`` must unsubscribe on every exit path."""

    async def test_cancelled_generator_unsubscribes(
        self, fresh_fanout: SSEFanOut
    ) -> None:
        """Cancelling the streaming task removes the subscriber."""
        request = _fake_request()

        async def _run() -> None:
            gen = _stream_events(
                request=request,
                fanout=fresh_fanout,
                workspace_id="ws_1",
                user_id="u",
                role="manager",
                last_event_id=_fresh_id(),
                heartbeat_interval=10.0,
            )
            await _collect_next_frame(gen)  # retry hint
            # Keep awaiting a never-arriving frame until cancelled.
            try:
                await _collect_next_frame(gen, timeout=5.0)
            finally:
                await gen.aclose()

        task = asyncio.create_task(_run())
        # Wait for the subscriber to register.
        for _ in range(20):
            await asyncio.sleep(0)
            state = fresh_fanout._workspaces.get("ws_1")
            if state is not None and state.subscribers:
                break
        assert fresh_fanout._workspaces["ws_1"].subscribers  # registered

        task.cancel()
        with pytest.raises((asyncio.CancelledError, TimeoutError)):
            await task

        # Subscriber was removed by the generator's ``finally``.
        assert fresh_fanout._workspaces["ws_1"].subscribers == set()

    async def test_normal_exit_unsubscribes(self, fresh_fanout: SSEFanOut) -> None:
        """Clean end-of-generator (client disconnect) also unsubscribes."""
        request = _fake_request()
        gen = _stream_events(
            request=request,
            fanout=fresh_fanout,
            workspace_id="ws_1",
            user_id="u",
            role="manager",
            last_event_id=_fresh_id(),
            heartbeat_interval=0.05,
        )
        await _collect_next_frame(gen)  # retry hint
        request._state["disconnected"] = True

        # Drive the generator to completion.
        with pytest.raises(StopAsyncIteration):
            for _ in range(3):
                await _collect_next_frame(gen, timeout=1.0)

        assert fresh_fanout._workspaces["ws_1"].subscribers == set()


# ---------------------------------------------------------------------------
# Default-role review gate
# ---------------------------------------------------------------------------


# Enumerate every currently-registered event that intentionally keeps the
# :data:`ALL_ROLES` default. Adding a new event without updating this
# allowlist is a review signal — the Coder should either add the event
# here (confirming every role may see it) or declare a narrower
# ``allowed_roles`` tuple on the subclass. Keeping the list in the test
# suite rather than on the event class means the review gate moves with
# the test-isolation snapshot instead of polluting runtime state.
DEFAULT_ROLE_EVENTS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "task.created",
        "task.assigned",
        "task.reassigned",
        "task.unassigned",
        "task.primary_unavailable",
        "task.updated",
        "task.completed",
        "task.overdue",
        "stay.upcoming",
        "shift.ended",
        "time.shift.changed",
        # ``notification.created`` keeps every role on ``allowed_roles``
        # because a notification can legitimately land for any grant
        # (an owner / manager / worker / client). The real narrowing
        # is ``user_scoped=True`` — the transport only delivers the
        # frame to the matching ``actor_user_id``, so a manager
        # watching the workspace stream does not see another user's
        # notifications.
        "notification.created",
    }
)


class TestDefaultRolesReviewGate:
    def test_no_new_event_silently_inherits_all_roles(self) -> None:
        """Every registered event using the default must be allowlisted.

        The base class defaults ``allowed_roles=ALL_ROLES`` for
        ergonomic reasons (business events typically fan out to the
        whole workspace), but a new event class that inherits the
        default must be a conscious decision. If this test fails
        after you register a new event, choose one of:

        1. Add the event name to
           :data:`DEFAULT_ROLE_EVENTS_ALLOWLIST` above, confirming
           every grant role may see it (manager, worker, client,
           guest).
        2. Declare a narrower ``allowed_roles`` tuple on the event
           subclass.

        Either is correct; leaving the implicit default silent is
        not. The PII posture relies on the author of each event
        confirming role scope — see :mod:`app.events.types` top
        comment.
        """
        offenders: list[str] = []
        for name, cls in registry_module._REGISTRY.items():
            if tuple(cls.allowed_roles) != ALL_ROLES:
                continue
            if name.startswith("test."):
                # Test-only event classes registered by another test
                # in the same run; they're fine.
                continue
            if name not in DEFAULT_ROLE_EVENTS_ALLOWLIST:
                offenders.append(name)
        assert offenders == [], (
            f"Newly-registered event(s) {offenders!r} inherit "
            "``allowed_roles = ALL_ROLES`` without being declared "
            "safe in DEFAULT_ROLE_EVENTS_ALLOWLIST. See the test's "
            "docstring."
        )
