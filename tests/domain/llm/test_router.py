"""Unit tests for :mod:`app.domain.llm.router` (cd-k0qf).

Covers every branch of the Beads acceptance criteria:

* Happy path seed-only chain.
* Priority demotion after an admin edit + cache invalidation.
* Inheritance one-hop and two-hop walks.
* Cycle defense in the inheritance walker.
* Unknown capability → :class:`CapabilityUnassignedError`.
* Fully-disabled chain (no parent) → :class:`CapabilityUnassignedError`.
* Cache TTL expiry.
* SSE-triggered cache invalidation via the
  :class:`~app.events.types.LlmAssignmentChanged` event on the
  production bus.
* Property-style test: random chains length 1..5 always come back in
  ascending priority order.
* Zero upstream I/O — we patch the LLM adapter import and assert it
  never got called.

See ``docs/specs/11-llm-and-agents.md`` §"Model assignment",
§"Capability inheritance", §"Client abstraction".
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.domain.llm import router as router_module
from app.domain.llm.router import (
    CACHE_TTL_SECONDS,
    CapabilityUnassignedError,
    ModelPick,
    resolve_model,
    resolve_primary,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import LlmAssignmentChanged
from app.tenancy import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.llm.conftest import (
    build_context,
    seed_assignment,
    seed_inheritance,
    seed_workspace,
)

_SEED_MODEL = "01HWA00000000000000000MDL0"


def _publish_assignment_changed(bus: EventBus, workspace_id: str) -> None:
    """Emit an :class:`LlmAssignmentChanged` event on ``bus``.

    Factored out so every invalidation test uses the exact payload
    shape the admin API would publish.
    """
    bus.publish(
        LlmAssignmentChanged(
            workspace_id=workspace_id,
            actor_id=new_ulid(),
            correlation_id=new_ulid(),
            occurred_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        )
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """The resolver returns the seeded assignment when only the seed exists."""

    def test_primary_returns_seed(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            row = seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                model_id=_SEED_MODEL,
                priority=1,
                max_tokens=4096,
                temperature=0.2,
                extra_api_params={"top_p": 0.95},
                required_capabilities=["chat", "function_calling"],
            )

            pick = resolve_primary(db_session, ctx, "chat.manager", clock=clock)

            assert isinstance(pick, ModelPick)
            assert pick.assignment_id == row.id
            assert pick.provider_model_id == _SEED_MODEL
            assert pick.api_model_id == _SEED_MODEL
            assert pick.max_tokens == 4096
            assert pick.temperature == pytest.approx(0.2)
            assert pick.extra_api_params == {"top_p": 0.95}
            assert pick.required_capabilities == ("chat", "function_calling")
        finally:
            reset_current(token)

    def test_chain_returns_priority_ascending(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Multiple rungs come back priority-ascending."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            # Insert out of order to prove the resolver sorts, not the
            # insert order, drives the return.
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=2,
                model_id="01HWA00000000000000000MDL2",
            )
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000MDL0",
            )
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=1,
                model_id="01HWA00000000000000000MDL1",
            )

            chain = resolve_model(db_session, ctx, "chat.manager", clock=clock)

            assert [p.provider_model_id for p in chain] == [
                "01HWA00000000000000000MDL0",
                "01HWA00000000000000000MDL1",
                "01HWA00000000000000000MDL2",
            ]
        finally:
            reset_current(token)

    def test_disabled_rows_skipped(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """An ``enabled=False`` rung is invisible to the resolver."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                enabled=False,
                model_id="01HWA00000000000000000DISB",
            )
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=1,
                enabled=True,
                model_id="01HWA00000000000000000ENBL",
            )

            chain = resolve_model(db_session, ctx, "chat.manager", clock=clock)

            assert [p.provider_model_id for p in chain] == [
                "01HWA00000000000000000ENBL",
            ]
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Priority demotion + invalidation
# ---------------------------------------------------------------------------


class TestPriorityDemotion:
    """Adding priority=0 demotes the priority=1 seed on next call."""

    def test_adding_priority_zero_demotes_seed_after_invalidation(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=1,
                model_id="01HWA00000000000000000SEED",
            )

            first = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert first.provider_model_id == "01HWA00000000000000000SEED"

            # Admin adds a priority=0 row; without invalidation the
            # cache would still hand out the old head.
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000NEW0",
            )

            # Publish the SSE signal the admin API would emit.
            _publish_assignment_changed(default_event_bus, ws.id)

            second = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert second.provider_model_id == "01HWA00000000000000000NEW0"
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


class TestInheritance:
    """``chat.admin`` inherits ``chat.manager``'s chain when it has none."""

    def test_one_hop_inheritance(self, db_session: Session, clock: FrozenClock) -> None:
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            # Parent has an enabled chain; child has nothing of its own.
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000PARM",
            )
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=1,
                model_id="01HWA00000000000000000PARF",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.admin",
                inherits_from="chat.manager",
            )

            chain = resolve_model(db_session, ctx, "chat.admin", clock=clock)

            assert [p.provider_model_id for p in chain] == [
                "01HWA00000000000000000PARM",
                "01HWA00000000000000000PARF",
            ]
        finally:
            reset_current(token)

    def test_child_chain_overrides_parent(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """The child's enabled chain wins over the parent's (not merged)."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000PARM",
            )
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.admin",
                priority=0,
                model_id="01HWA00000000000000000CHLD",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.admin",
                inherits_from="chat.manager",
            )

            chain = resolve_model(db_session, ctx, "chat.admin", clock=clock)

            assert [p.provider_model_id for p in chain] == [
                "01HWA00000000000000000CHLD",
            ]
        finally:
            reset_current(token)

    def test_two_hop_inheritance(self, db_session: Session, clock: FrozenClock) -> None:
        """Grand-child walks through grand-parent when both intermediate
        layers have no enabled assignments.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            # Only the grand-parent ``chat.base`` carries a chain; the
            # intermediate ``chat.manager`` has no rows; the child
            # ``chat.admin`` inherits from manager, which inherits
            # from base.
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.base",
                priority=0,
                model_id="01HWA00000000000000000BASE",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                inherits_from="chat.base",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.admin",
                inherits_from="chat.manager",
            )

            chain = resolve_model(db_session, ctx, "chat.admin", clock=clock)

            assert [p.provider_model_id for p in chain] == [
                "01HWA00000000000000000BASE",
            ]
        finally:
            reset_current(token)

    def test_cycle_defense_raises_rather_than_spins(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """A direct A → B → A cycle in the DB fails closed, not hangs.

        The write-path rejects cycles with
        ``422 capability_inheritance_cycle``; a dirty-migration path
        that lands one must not hang the resolver. The DB CHECK
        ``capability <> inherits_from`` only blocks the 0-hop
        self-loop; the resolver's ``visited`` set is what catches
        multi-hop cycles.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.a",
                inherits_from="chat.b",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.b",
                inherits_from="chat.a",
            )

            with pytest.raises(CapabilityUnassignedError) as exc_info:
                resolve_primary(db_session, ctx, "chat.a", clock=clock)
            assert exc_info.value.capability == "chat.a"
            assert exc_info.value.workspace_id == ws.id
        finally:
            reset_current(token)

    def test_two_hop_cycle_defense(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """A three-node cycle ``A → B → C → A`` still fails closed.

        Covers the same ``visited`` guard as the 1-hop case but with
        an extra node in the ring — a bug that dedups only the
        immediately-previous parent (off-by-one on ``visited``) would
        pass the shorter case and hang here.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.a",
                inherits_from="chat.b",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.b",
                inherits_from="chat.c",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.c",
                inherits_from="chat.a",
            )

            with pytest.raises(CapabilityUnassignedError):
                resolve_primary(db_session, ctx, "chat.a", clock=clock)
        finally:
            reset_current(token)

    def test_three_hop_cycle_defense(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """A four-node cycle ``A → B → C → D → A`` still fails closed."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.a",
                inherits_from="chat.b",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.b",
                inherits_from="chat.c",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.c",
                inherits_from="chat.d",
            )
            seed_inheritance(
                db_session,
                workspace_id=ws.id,
                capability="chat.d",
                inherits_from="chat.a",
            )

            with pytest.raises(CapabilityUnassignedError):
                resolve_primary(db_session, ctx, "chat.a", clock=clock)
        finally:
            reset_current(token)

    def test_runaway_chain_hop_budget(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """A pathologically long acyclic chain trips the hop guard.

        The ``visited`` set already defends against true cycles; this
        belt-and-braces hop budget catches a dirty-migration state
        where an operator manages to land an enormous legitimate
        chain that the write-path would normally refuse on policy
        grounds. We seed a linear chain of length
        ``_MAX_INHERITANCE_HOPS + 5`` — all nodes distinct, no
        cycle — and assert the resolver terminates with
        :class:`CapabilityUnassignedError` rather than walking every
        hop.

        ``_MAX_INHERITANCE_HOPS`` is the implementation's private
        bound; the test imports it directly so a raise of the bound
        automatically lengthens the scenario. Any node past the
        budget would have *no* enabled assignment anyway, so the
        failure mode is identical to the real cycle-guard path
        (``return []`` → ``CapabilityUnassignedError``).
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        length = router_module._MAX_INHERITANCE_HOPS + 5
        try:
            # chat.0 → chat.1 → chat.2 → … → chat.<length-1>. Every
            # node has an inheritance edge forward; nothing has an
            # enabled assignment.
            for i in range(length - 1):
                seed_inheritance(
                    db_session,
                    workspace_id=ws.id,
                    capability=f"chat.{i}",
                    inherits_from=f"chat.{i + 1}",
                )

            with pytest.raises(CapabilityUnassignedError):
                resolve_primary(db_session, ctx, "chat.0", clock=clock)
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Error branches
# ---------------------------------------------------------------------------


class TestErrors:
    def test_unknown_capability_raises(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            with pytest.raises(CapabilityUnassignedError) as exc_info:
                resolve_primary(db_session, ctx, "does.not.exist", clock=clock)
            assert exc_info.value.capability == "does.not.exist"
            assert exc_info.value.workspace_id == ws.id
        finally:
            reset_current(token)

    def test_fully_disabled_chain_with_no_parent_raises(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Every row disabled, no inheritance edge → raises."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                enabled=False,
            )
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=1,
                enabled=False,
            )

            with pytest.raises(CapabilityUnassignedError):
                resolve_primary(db_session, ctx, "chat.manager", clock=clock)
        finally:
            reset_current(token)

    def test_resolve_model_returns_empty_list_on_unassigned(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """``resolve_model`` (not ``_primary``) returns ``[]`` rather than raising.

        Callers that want to branch on "is this capability even
        assigned?" without catching an exception use the list form.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            chain = resolve_model(db_session, ctx, "ghost.cap", clock=clock)
            assert chain == []
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_hits_second_call_same_window(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Inside the TTL window, a second call does not re-read the DB."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            row = seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000HIT1",
            )

            first = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert first.provider_model_id == "01HWA00000000000000000HIT1"

            # Admin replaces the rung WITHOUT firing the SSE event.
            # If the router cached correctly, the second call still
            # sees the old pick (inside the TTL window).
            row.model_id = "01HWA00000000000000000HIT2"
            db_session.flush()

            second = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            # Still the cached value — invalidation was not signalled.
            assert second.provider_model_id == "01HWA00000000000000000HIT1"
        finally:
            reset_current(token)

    def test_cache_expires_after_ttl(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """After ``CACHE_TTL_SECONDS`` the next call re-reads the DB."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            row = seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000TTL1",
            )
            first = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert first.provider_model_id == "01HWA00000000000000000TTL1"

            # Swap the rung; do not publish the event.
            row.model_id = "01HWA00000000000000000TTL2"
            db_session.flush()

            # Inside the TTL: still cached.
            clock.advance(timedelta(seconds=CACHE_TTL_SECONDS - 1))
            still_cached = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert still_cached.provider_model_id == "01HWA00000000000000000TTL1"

            # Step past the TTL: fresh read.
            clock.advance(timedelta(seconds=2))
            fresh = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert fresh.provider_model_id == "01HWA00000000000000000TTL2"
        finally:
            reset_current(token)

    def test_sse_event_invalidates_cache_immediately(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Publishing :class:`LlmAssignmentChanged` drops the cache entry.

        Proves the SSE-invalidation hook is wired to the production
        bus so an operator edit lands on the next call even without
        waiting for the 30 s TTL.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            row = seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000SSE1",
            )

            first = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert first.provider_model_id == "01HWA00000000000000000SSE1"

            row.model_id = "01HWA00000000000000000SSE2"
            db_session.flush()

            _publish_assignment_changed(default_event_bus, ws.id)

            # No time advance: the SSE event alone moved the cache.
            second = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert second.provider_model_id == "01HWA00000000000000000SSE2"
        finally:
            reset_current(token)

    def test_invalidation_is_workspace_scoped(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """An event for workspace A does not drop workspace B's cache."""
        ws_a = seed_workspace(db_session, slug="ws-a")
        ws_b = seed_workspace(db_session, slug="ws-b")
        ctx_a = build_context(ws_a.id, slug="ws-a")
        ctx_b = build_context(ws_b.id, slug="ws-b")

        # Seed both workspaces.
        token = set_current(ctx_a)
        try:
            row_a = seed_assignment(
                db_session,
                workspace_id=ws_a.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000ISOA",
            )
        finally:
            reset_current(token)

        token = set_current(ctx_b)
        try:
            row_b = seed_assignment(
                db_session,
                workspace_id=ws_b.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000ISOB",
            )
        finally:
            reset_current(token)

        # Warm both caches.
        token = set_current(ctx_a)
        try:
            a1 = resolve_primary(db_session, ctx_a, "chat.manager", clock=clock)
            assert a1.provider_model_id == "01HWA00000000000000000ISOA"
        finally:
            reset_current(token)

        token = set_current(ctx_b)
        try:
            b1 = resolve_primary(db_session, ctx_b, "chat.manager", clock=clock)
            assert b1.provider_model_id == "01HWA00000000000000000ISOB"
        finally:
            reset_current(token)

        # Mutate both rows; publish only A's event. Only A's cache
        # should drop; B's cached pick stays.
        row_a.model_id = "01HWA00000000000000000ISA2"
        row_b.model_id = "01HWA00000000000000000ISB2"
        db_session.flush()

        _publish_assignment_changed(default_event_bus, ws_a.id)

        token = set_current(ctx_a)
        try:
            a2 = resolve_primary(db_session, ctx_a, "chat.manager", clock=clock)
            assert a2.provider_model_id == "01HWA00000000000000000ISA2"
        finally:
            reset_current(token)

        token = set_current(ctx_b)
        try:
            b2 = resolve_primary(db_session, ctx_b, "chat.manager", clock=clock)
            # B still sees its cached rung — the event was for A.
            assert b2.provider_model_id == "01HWA00000000000000000ISOB"
        finally:
            reset_current(token)

    def test_extra_api_params_is_read_only_view(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """A caller mutation on ``extra_api_params`` must not poison the cache.

        ``ModelPick.extra_api_params`` is a
        :class:`~types.MappingProxyType` over a defensive copy of the
        DB row's JSON column. Two defences stack:

        1. Any write through the proxy raises ``TypeError`` —
           proved here by attempting an item assignment.
        2. Even if a caller bypasses the proxy and mutates the
           underlying dict, the cache's snapshot came from a
           ``dict(row.extra_api_params)`` copy, so the row's live
           value and the cache are decoupled.

        A regression where ``_to_pick`` handed back the raw
        ``row.extra_api_params`` dict would silently re-poison every
        subsequent cache hit.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000EXP1",
                extra_api_params={"top_p": 0.9, "stop": ["STOP"]},
            )

            first = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert first.extra_api_params == {"top_p": 0.9, "stop": ["STOP"]}

            # Attempt (1): direct assignment through the proxy must
            # fail at the type level.
            with pytest.raises(TypeError):
                first.extra_api_params["top_p"] = 0.1  # type: ignore[index]

            # Attempt (2): if a caller somehow mutated a mutable view
            # they obtained elsewhere, the *next* resolve must still
            # see the pristine dict — not because the proxy blocked
            # it (it did), but because the cache row is independent
            # of any earlier caller's copy.
            second = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert second.extra_api_params == {"top_p": 0.9, "stop": ["STOP"]}
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Purity — no LLM I/O
# ---------------------------------------------------------------------------


class TestPurity:
    """The resolver never performs I/O against the LLM provider."""

    def test_no_llm_client_used_on_resolve(
        self,
        db_session: Session,
        clock: FrozenClock,
    ) -> None:
        """Patching the OpenRouter client's transport points the finger.

        Any attempt by the resolver to reach the provider would
        trigger the mock. Kept tight: we patch the client class'
        ``.complete`` and ``.chat`` so both the sync and chat
        codepaths are covered.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000PURE",
            )

            with (
                patch(
                    "app.adapters.llm.openrouter.OpenRouterClient.complete"
                ) as complete_mock,
                patch("app.adapters.llm.openrouter.OpenRouterClient.chat") as chat_mock,
            ):
                pick = resolve_primary(db_session, ctx, "chat.manager", clock=clock)

            assert pick.provider_model_id == "01HWA00000000000000000PURE"
            complete_mock.assert_not_called()
            chat_mock.assert_not_called()
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


class TestChainOrdering:
    """Random chains 1..5 always come back priority-ascending."""

    @pytest.mark.parametrize("seed", [7, 31, 97, 127, 251])
    def test_random_chains_sort_ascending(
        self,
        db_session: Session,
        clock: FrozenClock,
        seed: int,
    ) -> None:
        """Sample a random-length chain with shuffled insert order and
        assert the resolver hands it back sorted.

        Parameterised across five seeds so the property runs against
        enough distinct permutations to catch an off-by-one in the
        ORDER BY while staying deterministic under a pytest-xdist
        worker.
        """
        rng = random.Random(seed)
        length = rng.randint(1, 5)
        # Random (distinct) priorities — not necessarily contiguous;
        # the resolver orders by the raw ``priority`` value.
        priorities = rng.sample(range(0, 20), length)
        rng.shuffle(priorities)

        ws = seed_workspace(db_session, slug=f"ws-{seed}")
        ctx = build_context(ws.id, slug=f"ws-{seed}")
        token = set_current(ctx)
        try:
            expected: list[tuple[int, str]] = []
            for p in priorities:
                mid = f"01HWA0000000000000000MDL{p:02d}"
                seed_assignment(
                    db_session,
                    workspace_id=ws.id,
                    capability="chat.manager",
                    priority=p,
                    model_id=mid,
                )
                expected.append((p, mid))

            chain = resolve_model(db_session, ctx, "chat.manager", clock=clock)

            # Sort the expected tuples ourselves; the actual chain must
            # match the ascending-priority projection.
            expected.sort(key=lambda t: t[0])
            assert [p.provider_model_id for p in chain] == [mid for _, mid in expected]
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Context-scope sanity
# ---------------------------------------------------------------------------


class TestTenancy:
    """The resolver only returns rows from the caller's workspace.

    A per-workspace cache miss could silently hand back another
    tenant's chain if the ORM filter were absent or the cache key
    dropped the workspace id. The test exercises the full path.
    """

    def test_workspaces_see_different_chains(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        ws_a = seed_workspace(db_session, slug="ws-a")
        ws_b = seed_workspace(db_session, slug="ws-b")
        ctx_a = build_context(ws_a.id, slug="ws-a")
        ctx_b = build_context(ws_b.id, slug="ws-b")

        # Seed each workspace with its own rung.
        token = set_current(ctx_a)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws_a.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000WSA0",
            )
        finally:
            reset_current(token)

        token = set_current(ctx_b)
        try:
            seed_assignment(
                db_session,
                workspace_id=ws_b.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000WSB0",
            )
        finally:
            reset_current(token)

        # Resolve each side under its own context.
        token = set_current(ctx_a)
        try:
            pick_a = resolve_primary(db_session, ctx_a, "chat.manager", clock=clock)
            assert pick_a.provider_model_id == "01HWA00000000000000000WSA0"
        finally:
            reset_current(token)

        token = set_current(ctx_b)
        try:
            pick_b = resolve_primary(db_session, ctx_b, "chat.manager", clock=clock)
            assert pick_b.provider_model_id == "01HWA00000000000000000WSB0"
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Bus / subscription wire-up
# ---------------------------------------------------------------------------


class TestBusSubscription:
    """The router subscribes the production bus at import time."""

    def test_production_bus_subscription_is_idempotent(
        self,
        db_session: Session,
        clock: FrozenClock,
    ) -> None:
        """Calling ``_subscribe_to_bus`` on the same bus twice is a no-op.

        Proves re-entry during a test re-run won't stack duplicate
        handlers. Covers a ratcheting-handler hazard that would show
        up as a cache drop happening more times than it should.
        """
        before = router_module._SUBSCRIBED_BUSES.copy()
        router_module._subscribe_to_bus(default_event_bus)
        router_module._subscribe_to_bus(default_event_bus)
        after = router_module._SUBSCRIBED_BUSES.copy()
        assert before == after

    def test_fresh_bus_fixture_invalidates_cache(
        self,
        db_session: Session,
        clock: FrozenClock,
        bus: EventBus,
    ) -> None:
        """The test-local ``bus`` fixture is also wired to invalidate.

        Keeps the invalidation seam testable without having to
        publish on the singleton production bus for every assertion.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            row = seed_assignment(
                db_session,
                workspace_id=ws.id,
                capability="chat.manager",
                priority=0,
                model_id="01HWA00000000000000000LBUS",
            )

            first = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert first.provider_model_id == "01HWA00000000000000000LBUS"

            row.model_id = "01HWA00000000000000000LBU2"
            db_session.flush()

            _publish_assignment_changed(bus, ws.id)

            second = resolve_primary(db_session, ctx, "chat.manager", clock=clock)
            assert second.provider_model_id == "01HWA00000000000000000LBU2"
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Context factory sanity — keeps tests self-contained
# ---------------------------------------------------------------------------


def test_build_context_shape() -> None:
    """The helper returns a plain :class:`WorkspaceContext` — no surprises."""
    ctx = build_context("01HWA00000000000000000WSXX", slug="ws-sanity")
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == "01HWA00000000000000000WSXX"
    assert ctx.workspace_slug == "ws-sanity"
    assert ctx.actor_kind == "user"
    assert ctx.actor_grant_role == "manager"
