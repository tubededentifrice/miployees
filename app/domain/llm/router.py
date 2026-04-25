"""Capability → provider_model resolver (cd-k0qf).

Every LLM caller asks for a **capability** (``chat.manager``,
``expenses.autofill``, ``tasks.nl_intake``, …), never for a specific
model id. This module answers the question "which models should I
try, in what order, for this capability in this workspace?" by
walking the workspace's :class:`~app.adapters.db.llm.models.ModelAssignment`
rows (priority-ascending, enabled-only) and falling through
:class:`~app.adapters.db.llm.models.LlmCapabilityInheritance` edges
when the capability itself has no enabled assignments.

Public surface:

* :class:`ModelPick` — a single rung of the resolved chain. Carries
  everything a downstream client needs to dispatch the call
  (``provider_model_id``, ``api_model_id``, ``max_tokens``,
  ``temperature``, ``extra_api_params``, ``required_capabilities``,
  ``assignment_id``). The cd-4btd registry trio
  (:class:`~app.adapters.db.llm.models.LlmProvider` /
  :class:`~app.adapters.db.llm.models.LlmModel` /
  :class:`~app.adapters.db.llm.models.LlmProviderModel`) lets the
  resolver carry **two distinct strings** here:
  ``provider_model_id`` is the
  :attr:`LlmProviderModel.id` ULID (the registry row's identity);
  ``api_model_id`` is :attr:`LlmProviderModel.api_model_id` —
  whatever the provider expects on the wire (e.g.
  ``anthropic/claude-3-5-sonnet`` on OpenRouter,
  ``claude-3-5-sonnet-20241022`` on a native adapter).
* :func:`resolve_model` — the full priority-ordered chain for a
  capability. Callers walking retryable errors iterate the list.
* :func:`resolve_primary` — head of the chain; raises
  :class:`CapabilityUnassignedError` when the chain is empty
  (after inheritance has been walked). The API layer maps the
  exception to ``503 capability_unassigned`` with a ``CRITICAL``
  audit row per §11 "Failure modes".

Implementation notes:

* **Pure read path.** No audit on resolve; observability is
  recorded on the eventual ``llm_call`` row (cd-wjpl).
* **No upstream I/O.** The resolver never touches an LLM provider
  — it reads ORM rows and decides which model to try first.
* **Cycle-safe inheritance walk.** Even though the write-path (API
  / admin UI) rejects cycles at save time with ``422
  capability_inheritance_cycle`` (§11 "Capability inheritance"), a
  dirty-import path could land a cycle in the DB and the resolver
  must not spin. We track visited children and abort after a small
  hop budget; a detected cycle is treated as "no parent" — the
  caller sees :class:`CapabilityUnassignedError` rather than a
  hang.
* **30 s in-process cache, SSE-invalidated.** A workspace-wide
  dict keyed by ``(workspace_id, capability)`` avoids a DB round
  trip on every chat turn. The admin / API layer that mutates
  assignments publishes :class:`~app.events.types.LlmAssignmentChanged`
  on the production bus; a module-level subscriber drops every
  cache entry for the affected workspace on receipt, so operator
  edits land on the next call without waiting for the TTL.
  Invalidation is workspace-scoped (not capability-scoped) because
  the event payload does not carry enough information to target a
  single capability's chain — an edit to an inheritance edge can
  silently change the chain of a capability two hops downstream.

* **Single-process deployment assumption.** The cache lives in
  process memory and the event bus fans out only within the
  publishing process (:mod:`app.events.bus` is sync + in-process).
  v1 deployments run a single uvicorn worker (§16
  "Deployment / operations" — no multi-worker recipe yet), so an
  assignment edit from any process reaches every reader via the
  same bus. If a future deployment introduces multiple API workers
  or a dedicated agent-runtime process, a worker that doesn't host
  the publisher will serve stale picks until the 30 s TTL expires
  — a bounded but real staleness window. The cheapest right answer
  at that point is a Postgres ``LISTEN/NOTIFY`` (or Redis pub/sub)
  cross-process bridge that re-publishes
  :class:`LlmAssignmentChanged` on every subscriber's in-process
  bus; see the paired Beads follow-up.

* **Bus subscription at import time.** The production bus is
  wired up at the bottom of this module rather than via a lazy
  startup hook (compare :mod:`app.api.transport.sse`'s
  ``_ensure_bus_binding``). This module has exactly one handler
  to register, :class:`~app.events.bus.EventBus` is constructed at
  the import of :mod:`app.events.bus` so the subscribe call can
  never hit an uninitialised bus, and :func:`_subscribe_to_bus` is
  idempotent — so there's nothing the lazy pattern buys us that
  the simpler import-time subscribe doesn't already give. Tests
  that construct a fresh :class:`EventBus` wire it up explicitly
  via :func:`_subscribe_to_bus`.

See ``docs/specs/11-llm-and-agents.md`` §"Model assignment",
§"Capability inheritance", §"Client abstraction",
``docs/specs/02-domain-model.md`` §"LLM".
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    LlmCapabilityInheritance,
    LlmProviderModel,
    ModelAssignment,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import LlmAssignmentChanged
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "CACHE_TTL_SECONDS",
    "CapabilityUnassignedError",
    "ModelPick",
    "invalidate_cache",
    "resolve_model",
    "resolve_primary",
]


# Cache lifetime for a (workspace_id, capability) chain. 30 s is the
# §11-pinned value: long enough to cover bursty chat traffic (a
# worker process can complete dozens of turns inside one window
# without re-reading the DB) and short enough that an admin edit
# that *doesn't* ride the SSE invalidation path — a direct DB poke,
# a long-running process whose bus subscription predates the edit —
# lands within a user-tolerable delay. Callers that need the
# current chain inside the window use :func:`invalidate_cache`
# explicitly.
CACHE_TTL_SECONDS: int = 30

# Inheritance-walk cycle guard. v1 seeds one edge (``chat.admin →
# chat.manager``); deeper chains remain rare by spec intent. The
# write-path rejects cycles; this bound is a safety net against a
# corrupt DB state, not a legitimate chain length. 16 is far beyond
# any plausible inheritance tree and well short of a runaway
# hot-loop.
_MAX_INHERITANCE_HOPS: int = 16


@dataclass(frozen=True, slots=True)
class ModelPick:
    """One rung of a resolved fallback chain.

    ``provider_model_id`` is the
    :class:`~app.adapters.db.llm.models.LlmProviderModel.id` ULID —
    the registry row the assignment points at (cd-4btd FK).
    ``api_model_id`` is :attr:`LlmProviderModel.api_model_id` —
    what the provider expects on the wire (OpenRouter prefixes with
    a vendor; a native SDK adapter doesn't). Adapters dispatch on
    ``api_model_id``; observability + assignment edits hold
    ``provider_model_id``.

    Every field is a value; the dataclass is frozen + slotted so a
    rung can be stashed in a cache bucket and handed back to
    multiple callers without aliasing risk.
    """

    # The ``llm_provider_model.id`` row this rung resolved to.
    # Promoted from soft reference to a real FK by cd-4btd.
    provider_model_id: str
    # What the adapter sends on the wire — the provider's
    # ``api_model_id`` for this row. Distinct from
    # ``provider_model_id`` whenever the canonical model name and
    # the wire form diverge (the common case on OpenRouter).
    api_model_id: str
    # Per-call tuning; ``None`` = inherit the provider-model /
    # model default (§11 "Model assignment", "Provider / model /
    # provider-model registry").
    max_tokens: int | None
    temperature: float | None
    # Merged-last provider-layer params (``top_p``, tool hints, …).
    # Frozen inside the dataclass; callers MUST NOT mutate.
    extra_api_params: Mapping[str, Any] = field(default_factory=dict)
    # Capability tags copied from the §11 catalogue on save
    # (``vision``, ``json_mode``, …). Adapter cross-checks the
    # target model before dispatch.
    required_capabilities: tuple[str, ...] = ()
    # The assignment row this rung was resolved from. Denormalised
    # onto the ``llm_call`` row later for chain-level observability
    # (§11 "Failure modes" ``X-LLM-Fallback-Attempts``).
    assignment_id: str = ""


class CapabilityUnassignedError(Exception):
    """No enabled assignment found for a capability, even after inheritance.

    Raised when:

    * the capability has no enabled
      :class:`~app.adapters.db.llm.models.ModelAssignment` rows in
      the workspace, and
    * no :class:`~app.adapters.db.llm.models.LlmCapabilityInheritance`
      edge leads to a capability that does, and
    * the inheritance walk either terminates at a capability with
      no parent or trips the cycle guard.

    The API layer maps this to ``503 capability_unassigned`` with a
    ``CRITICAL`` audit row (§11 "Failure modes"). Domain callers
    typically degrade gracefully — a digest worker that loses its
    capability skips the enrichment; a chat surface falls back to a
    plain acknowledgement.
    """

    def __init__(self, capability: str, workspace_id: str) -> None:
        super().__init__(
            f"Capability {capability!r} has no enabled assignment (after "
            f"inheritance walk) in workspace {workspace_id!r}."
        )
        self.capability = capability
        self.workspace_id = workspace_id


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CacheEntry:
    """One ``(workspace_id, capability) → chain`` bucket with its TTL.

    Stored eagerly even for the empty-chain outcome so repeated
    calls against an unassigned capability don't re-walk the
    inheritance edges every time. An empty ``chain`` tuple
    therefore means "confirmed empty, re-raise
    :class:`CapabilityUnassignedError` without hitting the DB".
    """

    chain: tuple[ModelPick, ...]
    expires_at: datetime


# Module-level cache. Keyed by ``(workspace_id, capability)``. The
# lock protects mutation against the event handler (which may fire
# on a different thread once SSE fan-out lands); read paths take
# the lock too for consistent dict snapshots, but they never hold
# it across DB I/O — the pattern is "grab the bucket, release, use".
_CACHE: dict[tuple[str, str], _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()

# Tracks which buses have been wired up to our invalidation
# handler. Tests that allocate a fresh :class:`EventBus` can call
# :func:`_subscribe_to_bus` against it; the production bus is
# subscribed at import time (bottom of module).
_SUBSCRIBED_BUSES: set[int] = set()
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


def invalidate_cache(workspace_id: str | None = None) -> None:
    """Drop cache entries.

    ``workspace_id=None`` wipes every entry (used by tests and the
    belt-and-braces "something odd happened" path); a concrete
    workspace id drops only that workspace's entries, which is the
    shape the :class:`~app.events.types.LlmAssignmentChanged`
    handler uses.

    Thread-safe: the lock covers both the scan and the pop so a
    concurrent write from another thread cannot leave the dict in
    a half-cleared state.
    """
    with _CACHE_LOCK:
        if workspace_id is None:
            _CACHE.clear()
            return
        # Snapshot the matching keys, then pop. Iterating the live
        # dict while mutating would raise ``RuntimeError: dictionary
        # changed size during iteration`` on CPython.
        stale_keys = [key for key in _CACHE if key[0] == workspace_id]
        for key in stale_keys:
            _CACHE.pop(key, None)


def _on_llm_assignment_changed(event: LlmAssignmentChanged) -> None:
    """Subscribe hook: drop the cache for the affected workspace.

    Whole-workspace invalidation (not per-capability) because the
    event payload does not name the affected capability: an edit to
    a :class:`LlmCapabilityInheritance` edge can silently change the
    chain of a capability two hops downstream, so narrowing the
    invalidation scope without a richer payload would miss cases.
    """
    invalidate_cache(workspace_id=event.workspace_id)


def _subscribe_to_bus(event_bus: EventBus) -> None:
    """Wire :func:`_on_llm_assignment_changed` onto ``event_bus`` once.

    Idempotent — re-subscribing the same bus during a test re-run
    would double-fire the handler, which would be harmless on a
    cache (the second drop is a no-op) but noisy in traces. Using
    ``id(event_bus)`` as the dedup key is exact under CPython and
    stable for the lifetime of the bus instance.
    """
    bus_id = id(event_bus)
    with _SUBSCRIBED_BUSES_LOCK:
        if bus_id in _SUBSCRIBED_BUSES:
            return
        _SUBSCRIBED_BUSES.add(bus_id)
    event_bus.subscribe(LlmAssignmentChanged)(_on_llm_assignment_changed)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _load_enabled_chain(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
) -> list[ModelPick]:
    """Read enabled assignments for ``capability`` in priority order.

    Returns an empty list when the capability has no enabled rows;
    the caller decides whether to walk inheritance from there.
    Relies on the ORM tenant filter (see
    :mod:`app.tenancy.orm_filter`) to pin ``workspace_id`` on the
    workspace-scoped ``model_assignment`` table; the deployment-
    scope ``llm_provider_model`` join target is intentionally NOT
    in the registry, so the filter leaves it alone — exactly what
    we want for a deployment-shared row. We still pass
    ``workspace_id`` through to keep the function self-documenting
    and to narrow the :class:`CapabilityUnassignedError` message in
    the caller.

    cd-4btd: the JOIN through ``llm_provider_model`` lets the
    resolver surface :attr:`LlmProviderModel.api_model_id` (the
    provider's wire form) on
    :class:`ModelPick.api_model_id` while keeping the registry id
    on :class:`ModelPick.provider_model_id`. We use a plain INNER
    join because :attr:`ModelAssignment.model_id` is now a NOT NULL
    FK — every assignment must point at a registry row, so a LEFT
    join would only mask a data integrity bug rather than recover
    from one.
    """
    stmt = (
        select(ModelAssignment, LlmProviderModel)
        .join(
            LlmProviderModel,
            ModelAssignment.model_id == LlmProviderModel.id,
        )
        .where(
            ModelAssignment.capability == capability,
            ModelAssignment.enabled.is_(True),
        )
        .order_by(ModelAssignment.priority.asc(), ModelAssignment.id.asc())
    )
    rows = session.execute(stmt).all()
    return [_to_pick(assignment, provider_model) for assignment, provider_model in rows]


def _to_pick(row: ModelAssignment, provider_model: LlmProviderModel) -> ModelPick:
    """Map an (assignment, provider_model) pair to a frozen :class:`ModelPick`.

    JSON columns round-trip as mutable ``dict`` / ``list`` — wrap
    the params in a :class:`~types.MappingProxyType` view over a
    defensive copy, and coerce the tags to a ``tuple``, so a caller
    that mutates the mapping or the list does not retroactively
    corrupt the cache bucket every subscriber shares. The copy is
    cheap at cache-miss time (hundreds of params are unheard of) and
    removes a whole class of aliasing bug from the downstream
    adapter.

    cd-4btd surfaces :attr:`LlmProviderModel.api_model_id` directly
    on the pick. Per-call tuning (``max_tokens``, ``temperature``,
    ``extra_api_params``) still comes from the assignment — the
    operator's per-workspace override beats the deployment-scope
    default. Promoting provider_model overrides to the pick is a
    follow-up once the spec pins the merge order; the
    :attr:`LlmProviderModel.max_tokens_override` /
    ``temperature_override`` / ``supports_*`` flags are the obvious
    candidates but every one has a "did the operator mean to
    override?" question that the v1 surface answers via the
    /admin/llm graph editor, not the resolver.
    """
    extra_copy: dict[str, Any] = (
        dict(row.extra_api_params) if row.extra_api_params else {}
    )
    extra: Mapping[str, Any] = MappingProxyType(extra_copy)
    required = tuple(row.required_capabilities or ())
    return ModelPick(
        provider_model_id=provider_model.id,
        api_model_id=provider_model.api_model_id,
        max_tokens=row.max_tokens,
        temperature=row.temperature,
        extra_api_params=extra,
        required_capabilities=required,
        assignment_id=row.id,
    )


def _lookup_parent_capability(
    session: Session,
    *,
    capability: str,
) -> str | None:
    """Return the parent capability via ``llm_capability_inheritance``.

    ``None`` means this capability has no inheritance edge; the
    caller raises :class:`CapabilityUnassignedError`. The ORM
    tenant filter scopes the read to the active workspace.

    Uniqueness of ``(workspace_id, capability)`` on the
    inheritance table (see model docstring) means at most one row
    matches — no tie-break rule needed.
    """
    stmt = select(LlmCapabilityInheritance.inherits_from).where(
        LlmCapabilityInheritance.capability == capability
    )
    return session.execute(stmt).scalar_one_or_none()


def _resolve_chain(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
) -> list[ModelPick]:
    """Walk the capability + inheritance tree until a chain is found.

    Returns the first non-empty priority-ordered chain encountered;
    an empty list if the walk terminates at a capability with no
    enabled assignments and no parent (or a cycle is detected).
    The caller turns an empty list into either a cache miss-sentinel
    or :class:`CapabilityUnassignedError` as appropriate.
    """
    visited: set[str] = set()
    current = capability
    hops = 0
    while True:
        if current in visited or hops >= _MAX_INHERITANCE_HOPS:
            # Cycle or runaway chain: treat as "no parent". The
            # write-path is supposed to reject cycles with
            # ``422 capability_inheritance_cycle``, so reaching
            # this branch means either a dirty-migration path or a
            # pathological inheritance tree; either way we fail
            # closed to :class:`CapabilityUnassignedError` rather
            # than hang.
            return []
        visited.add(current)
        hops += 1

        chain = _load_enabled_chain(
            session, workspace_id=workspace_id, capability=current
        )
        if chain:
            return chain

        parent = _lookup_parent_capability(session, capability=current)
        if parent is None:
            return []
        current = parent


def _cached_or_resolve(
    session: Session,
    *,
    ctx: WorkspaceContext,
    capability: str,
    clock: Clock,
) -> list[ModelPick]:
    """TTL-gated cache around :func:`_resolve_chain`.

    Returns a fresh list on each call so a caller mutating the
    returned list cannot clobber the cached tuple for the next
    caller. The cached value itself is immutable (tuple of frozen
    dataclasses).
    """
    key = (ctx.workspace_id, capability)
    now = clock.now()

    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None and entry.expires_at > now:
            return list(entry.chain)

    # Cache miss (or expired): resolve outside the lock — the DB
    # read can block, and other threads holding stale-but-valid
    # entries for different keys should not queue behind us.
    fresh = _resolve_chain(
        session, workspace_id=ctx.workspace_id, capability=capability
    )

    expires_at = now + timedelta(seconds=CACHE_TTL_SECONDS)
    with _CACHE_LOCK:
        # Last-writer-wins on race: two threads resolving the same
        # key simultaneously both write the same answer, so the race
        # is a small perf cost (double DB read) rather than a
        # correctness hazard. Using ``setdefault`` would strand the
        # later thread's fresher TTL; a plain assign keeps the
        # window predictable.
        _CACHE[key] = _CacheEntry(chain=tuple(fresh), expires_at=expires_at)

    return fresh


def resolve_model(
    session: Session,
    ctx: WorkspaceContext,
    capability: str,
    *,
    clock: Clock | None = None,
) -> list[ModelPick]:
    """Return the resolved fallback chain for ``capability``.

    Priority-ascending, enabled-only, with inheritance walked when
    the capability itself has no rows. Returns ``[]`` when no
    chain exists — prefer :func:`resolve_primary` when you want a
    single pick and fail-closed semantics.

    ``clock`` defaults to :class:`~app.util.clock.SystemClock`;
    tests thread a :class:`~app.util.clock.FrozenClock` through so
    TTL-advance cases are deterministic.
    """
    c = clock if clock is not None else SystemClock()
    return _cached_or_resolve(session, ctx=ctx, capability=capability, clock=c)


def resolve_primary(
    session: Session,
    ctx: WorkspaceContext,
    capability: str,
    *,
    clock: Clock | None = None,
) -> ModelPick:
    """Return the head of the resolved chain for ``capability``.

    Raises :class:`CapabilityUnassignedError` when the chain is
    empty — the caller (API layer) maps this to
    ``503 capability_unassigned`` and writes the ``CRITICAL`` audit
    row per §11 "Failure modes".
    """
    chain = resolve_model(session, ctx, capability, clock=clock)
    if not chain:
        raise CapabilityUnassignedError(capability, ctx.workspace_id)
    return chain[0]


# ---------------------------------------------------------------------------
# Production wire-up
# ---------------------------------------------------------------------------


# Subscribe the cache-invalidation handler to the production bus at
# import time. Tests that construct a fresh :class:`EventBus`
# instance (isolation fixture pattern) can call
# :func:`_subscribe_to_bus` against it explicitly.
_subscribe_to_bus(default_event_bus)


# Kept purely for test isolation: drops the subscription set so a
# test that monkey-patches the bus can re-subscribe after reset.
# Production code must not call this.
def _reset_subscriptions_for_tests() -> None:
    """Clear the subscribed-bus dedup set.

    Paired with the usual ``EventBus._reset_for_tests`` pattern: a
    test that flips the production bus back to "empty" needs to
    tell us we are no longer wired up, or the next
    :func:`_subscribe_to_bus` call would no-op.
    """
    with _SUBSCRIBED_BUSES_LOCK:
        _SUBSCRIBED_BUSES.clear()
