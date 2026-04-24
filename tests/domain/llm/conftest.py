"""Fixtures for :mod:`app.domain.llm.router` tests (cd-k0qf).

Binds onto the shared integration engine + migration harness so the
``CREWDAY_TEST_DB={sqlite,postgres}`` shard selector reaches this
package — pytest autoloads the sibling ``tests/integration/conftest.py``
only for its own directory, so we re-import the relevant fixtures
here (same pattern as ``tests/tenant/conftest.py``).

The router tests need:

* the real migrated schema (so ``model_assignment`` has its
  cd-u84y columns and ``llm_capability_inheritance`` exists);
* the ORM tenant filter installed on the sessionmaker (so the
  resolver's SELECTs are scoped to the active
  :class:`WorkspaceContext`);
* the router's cache + bus subscriptions reset between cases so
  TTL / invalidation assertions don't bleed into one another.

See ``docs/specs/11-llm-and-agents.md`` §"Model assignment",
§"Capability inheritance", ``docs/specs/17-testing-quality.md``
§"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.llm.models import (
    LlmCapabilityInheritance,
    ModelAssignment,
)
from app.adapters.db.workspace.models import Workspace
from app.domain.llm import router as router_module
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.tenancy import WorkspaceContext, registry
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

# Re-export integration-layer fixtures so the shared engine /
# migrate_once / db_url machinery reaches this package. Pytest only
# autoloads the parent ``tests/integration/conftest.py`` inside its
# own directory; without these re-imports the fixtures below would
# resolve as "not found".
from tests.integration.conftest import (
    db_url as _db_url_fixture,
)
from tests.integration.conftest import (
    engine as _engine_fixture,
)
from tests.integration.conftest import (
    migrate_once as _migrate_once_fixture,
)
from tests.integration.conftest import (
    pytest_collection_modifyitems as pytest_collection_modifyitems,  # re-export
)

# Session-scoped shared engine + URL + alembic upgrade, honouring
# ``CREWDAY_TEST_DB={sqlite,postgres}`` per the integration harness.
db_url = _db_url_fixture
engine = _engine_fixture
migrate_once = _migrate_once_fixture


# Pinned wall-clock so TTL / ULID assertions stay deterministic.
_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# LLM tables must stay in the workspace-scoped registry. A sibling
# unit test (``tests/unit/test_tenancy_orm_filter.py``) wipes the
# process-wide registry in an autouse fixture; without this repair
# the tenant filter silently drops off our LLM queries when the
# full suite runs. Same pattern as
# ``tests/integration/test_db_llm.py::_ensure_llm_registered``.
_LLM_TABLES: tuple[str, ...] = (
    "model_assignment",
    "llm_capability_inheritance",
)


@pytest.fixture(autouse=True)
def _ensure_llm_registered() -> None:
    for table in _LLM_TABLES:
        registry.register(table)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessionmaker with the ORM tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across cases. The filter installer is idempotent
    on a given target, so reusing the factory is safe.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Function-scoped tenant-filtered session with SAVEPOINT rollback.

    Opens a raw connection + outer transaction, binds a session with
    ``join_transaction_mode="create_savepoint"`` so nested
    ``commit()`` calls from the service under test turn into
    SAVEPOINTs that the outer ``rollback()`` sweeps away at teardown
    — identical to the integration-layer ``db_session`` fixture.

    The filter is installed directly on the session (not the
    sessionmaker) because each case owns its own session instance.
    """
    with engine.connect() as raw_connection:
        outer = raw_connection.begin()
        factory = sessionmaker(
            bind=raw_connection,
            expire_on_commit=False,
            class_=Session,
            join_transaction_mode="create_savepoint",
        )
        install_tenant_filter(factory)
        session = factory()
        try:
            yield session
        finally:
            session.close()
            if outer.is_active:
                outer.rollback()


@pytest.fixture
def clock() -> FrozenClock:
    """Frozen clock pinned to :data:`_PINNED`; tests advance it by hand."""
    return FrozenClock(_PINNED)


@pytest.fixture
def bus() -> EventBus:
    """Fresh in-process bus per test; subscribed to router invalidation.

    Use this when asserting that an event published on the bus
    invalidates the router cache. The production bus is also wired
    up at router import time — the ``_reset_router_state`` fixture
    below re-subscribes to it between cases.
    """
    b = EventBus()
    router_module._subscribe_to_bus(b)
    return b


@pytest.fixture(autouse=True)
def _reset_router_state() -> Iterator[None]:
    """Drop cache + subscriptions between cases.

    Without this fixture the first case's cache entries would leak
    into the second, and a test that unsubscribes the bus would
    leave the dedup set stale for the next one.

    After the reset, re-subscribe the production bus so import-time
    semantics stay correct — a test that publishes on
    :data:`app.events.bus.bus` still sees invalidation fire.
    """
    router_module.invalidate_cache()
    router_module._reset_subscriptions_for_tests()
    default_event_bus._reset_for_tests()
    router_module._subscribe_to_bus(default_event_bus)
    try:
        yield
    finally:
        router_module.invalidate_cache()
        router_module._reset_subscriptions_for_tests()


@pytest.fixture(autouse=True)
def _reset_tenancy_context() -> Iterator[None]:
    """Every test starts without an active :class:`WorkspaceContext`.

    Mirrors the pattern in :mod:`tests.integration.test_db_llm`;
    prevents one case's leaked context from silently satisfying the
    tenant filter in another.
    """
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Helpers — concise row factories
# ---------------------------------------------------------------------------


def build_context(workspace_id: str, *, slug: str = "ws-test") -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace_id``."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def seed_workspace(session: Session, *, slug: str | None = None) -> Workspace:
    """Insert a workspace row tenancy-agnostic (bootstrap path).

    The full ``bootstrap_workspace`` helper in
    :mod:`tests.factories.identity` also seeds the ``owners``
    permission group + a membership row; the router tests don't
    exercise authz at all, so a plain workspace insert keeps the
    setup surface narrow. A bare workspace row still carries the
    CASCADE sweep the LLM rows depend on.

    ``slug`` is auto-generated when the caller doesn't pin one so
    concurrent cases under the shared integration engine do not
    collide on the ``workspace.slug`` UNIQUE — the SAVEPOINT rolls
    back at teardown but the slug check fires at flush time.
    """
    from app.tenancy import tenant_agnostic

    ws_slug = slug or f"ws-{new_ulid().lower()[:12]}"
    ws = Workspace(
        id=new_ulid(),
        slug=ws_slug,
        name=f"Workspace {ws_slug}",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    # justification: bootstrap seeding the tenancy anchor itself; no
    # WorkspaceContext exists yet for the filter to bind to.
    with tenant_agnostic():
        session.add(ws)
        session.flush()
    return ws


def seed_assignment(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
    model_id: str | None = None,
    provider: str = "openrouter",
    priority: int = 0,
    enabled: bool = True,
    max_tokens: int | None = None,
    temperature: float | None = None,
    extra_api_params: dict[str, object] | None = None,
    required_capabilities: list[str] | None = None,
) -> ModelAssignment:
    """Insert a :class:`ModelAssignment` with sensible defaults."""
    row = ModelAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability=capability,
        model_id=model_id or new_ulid(),
        provider=provider,
        priority=priority,
        enabled=enabled,
        max_tokens=max_tokens,
        temperature=temperature,
        extra_api_params=dict(extra_api_params or {}),
        required_capabilities=list(required_capabilities or []),
        created_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def seed_inheritance(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
    inherits_from: str,
) -> LlmCapabilityInheritance:
    """Insert a child → parent inheritance edge."""
    row = LlmCapabilityInheritance(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability=capability,
        inherits_from=inherits_from,
        created_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row
