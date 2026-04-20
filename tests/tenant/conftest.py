"""Cross-tenant regression matrix — shared fixtures.

Seeds two workspaces ``A`` and ``B`` on the shared integration engine
with deliberately colliding rows (same property names, same emails
where uniqueness allows, same slug shapes) so an accidental
cross-tenant read surfaces as a wrong row rather than an empty one.

Each fixture is session- or function-scoped as tight as the shared
state allows: workspace seeding is session-scoped because the tenancy
middleware resolves slugs → workspace rows before every scoped HTTP
request and flipping them per-function would 10x the suite. Per-test
mutations go through the function-scoped ``db_session`` rollback
fixture in :mod:`tests.integration.conftest` (which this module
re-exports).

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" for the full contract.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.session import issue as issue_session
from app.auth.tokens import mint as mint_token
from app.config import Settings
from app.tenancy import WorkspaceContext
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

# Pull the integration-layer conftest into scope. ``pytest`` autoloads
# the parent ``tests/integration/conftest.py`` only for its own
# directory; re-importing here makes the ``engine``, ``db_url`` and
# ``migrate_once`` fixtures available under ``tests/tenant/``.
from tests.integration.conftest import (
    db_session as _db_session_fixture,
)
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

# Re-export the shared engine / db_url / migrate_once / rollback
# session fixtures so pytest can discover them under this package.
# These are the **same objects** the integration suite uses — no new
# engine, no new Alembic run.
db_url = _db_url_fixture
engine = _engine_fixture
migrate_once = _migrate_once_fixture
db_session = _db_session_fixture


# Pin the suite to a deterministic UTC moment so ULID ordering and
# session / token issue timestamps match across re-runs.
_PINNED_NOW: datetime = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seeded world — two workspaces with colliding rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TenantSeed:
    """One seeded workspace + owner + outsider, plus live session + token.

    Carries everything an HTTP / worker / repository test needs to
    probe the workspace without re-seeding per case:

    * ``slug`` / ``workspace_id`` — the tenancy anchor.
    * ``owner_user_id`` — the first user, member of this workspace.
    * ``outsider_user_id`` — a user NOT a member of this workspace
      (crucial for the HTTP surface test: an authenticated-but-
      non-member probe, §15 "Constant-time cross-tenant responses").
    * ``owner_session_cookie`` — a live session cookie for the owner,
      issued via the production :func:`app.auth.session.issue`.
    * ``owner_token`` — a workspace-scoped bearer token for the
      owner, minted via :func:`app.auth.tokens.mint`.
    * ``ctx`` — a :class:`WorkspaceContext` for direct domain /
      repository calls without a request cycle.
    """

    slug: str
    workspace_id: str
    owner_user_id: str
    outsider_user_id: str
    owner_session_cookie: str
    outsider_session_cookie: str
    owner_token: str
    ctx: WorkspaceContext


@pytest.fixture(scope="session")
def tenant_settings() -> Settings:
    """Pinned :class:`Settings` for the tenant matrix.

    ``phase0_stub_enabled=False`` so the real slug / session / token
    resolver runs — the ``X-Test-Workspace-Id`` stub would bypass the
    exact code path we're trying to exercise.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("tenant-matrix-root-key-do-not-reuse"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        phase0_stub_enabled=False,
    )


@pytest.fixture(scope="session")
def tenant_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session-scoped ``sessionmaker`` with the tenant filter installed.

    Shared across the entire tenant suite: the filter installer is
    idempotent on the same target, and reusing the factory keeps the
    seed / middleware / test clients on the exact same seam. Per-test
    isolation comes from the function-scoped :func:`db_session`
    rollback fixture (re-exported from
    :mod:`tests.integration.conftest`).
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


# Deliberately-colliding seed constants. Same property-name /
# user-email / slug shape across A and B so a wrong-workspace read
# produces a wrong row, not an empty set (§17 "Cross-tenant
# regression test" — "an accidental cross-tenant read is visible as
# a wrong row, not merely an empty one").
_COLLIDING_EMAIL_LOCAL = "owner"
_COLLIDING_OUTSIDER_LOCAL = "outsider"
_COLLIDING_EMAIL_DOMAIN = "example.com"
_SEEDED_SLUGS: tuple[str, str] = ("tenant-a", "tenant-b")


def _seed_one_workspace(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    slug: str,
) -> TenantSeed:
    """Seed a workspace + owner + outsider + session + token.

    The outsider is a real user with a **live session** but NOT a
    member of this workspace — so a probe under their session
    cookie models "a logged-in user authenticated to a different
    tenancy", the exact cross-tenant attacker shape.

    Emails deliberately collide on the local part
    (``owner-<slug>@example.com``) but diverge on the slug segment so
    the ``user`` table's unique constraint still accepts both rows —
    we want colliding *display* shape, not actually-duplicate emails.
    """
    with factory() as s:
        owner = bootstrap_user(
            s,
            email=f"{_COLLIDING_EMAIL_LOCAL}-{slug}@{_COLLIDING_EMAIL_DOMAIN}",
            display_name=f"Owner {slug}",
        )
        workspace = bootstrap_workspace(
            s,
            slug=slug,
            # Deliberate collision: both workspaces share the exact
            # human-facing name. A cross-tenant read that accidentally
            # landed on the peer's row would not stand out from the
            # "right" row on the name column alone.
            name="Shared Name Corp",
            owner_user_id=owner.id,
        )
        # Outsider: a separate user that will NOT be added as a member
        # of this workspace. Session issuing is tenant-agnostic at
        # issue time, so the cookie simply authenticates the user —
        # the membership miss happens inside the tenancy middleware
        # when the outsider probes a /w/<slug>/... path.
        outsider = bootstrap_user(
            s,
            email=f"{_COLLIDING_OUTSIDER_LOCAL}-{slug}@{_COLLIDING_EMAIL_DOMAIN}",
            display_name=f"Outsider {slug}",
        )
        # Issue a session + bearer token for the owner so HTTP tests
        # can hit scoped routes through the real middleware.
        owner_session = issue_session(
            s,
            user_id=owner.id,
            has_owner_grant=True,
            ua="testclient",
            ip="127.0.0.1",
            accept_language="",
            now=_PINNED_NOW,
            settings=settings,
        )
        outsider_session = issue_session(
            s,
            user_id=outsider.id,
            has_owner_grant=True,
            ua="testclient",
            ip="127.0.0.1",
            accept_language="",
            now=_PINNED_NOW,
            settings=settings,
        )
        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=slug,
            actor_id=owner.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        token = mint_token(
            s,
            ctx,
            user_id=owner.id,
            label=f"tenant-matrix-{slug}",
            scopes={"tasks.read": True, "time.read": True},
            expires_at=None,
            now=_PINNED_NOW,
        )
        s.commit()

        return TenantSeed(
            slug=slug,
            workspace_id=workspace.id,
            owner_user_id=owner.id,
            outsider_user_id=outsider.id,
            owner_session_cookie=owner_session.cookie_value,
            outsider_session_cookie=outsider_session.cookie_value,
            owner_token=token.token,
            ctx=ctx,
        )


@pytest.fixture(scope="session")
def tenants(
    tenant_session_factory: sessionmaker[Session],
    tenant_settings: Settings,
    migrate_once: None,
) -> tuple[TenantSeed, TenantSeed]:
    """Session-scoped ``(A, B)`` seed pair.

    Idempotent on re-entry: Alembic's ``upgrade head`` runs once per
    session via :func:`migrate_once`, and the two seed inserts are a
    one-time event per test process. Per-test mutations happen in a
    nested SAVEPOINT via the function-scoped :func:`db_session`
    fixture so rollback discards them without dropping the seed.
    """
    a_slug, b_slug = _SEEDED_SLUGS
    a = _seed_one_workspace(
        tenant_session_factory,
        tenant_settings,
        slug=a_slug,
    )
    b = _seed_one_workspace(
        tenant_session_factory,
        tenant_settings,
        slug=b_slug,
    )
    return (a, b)


@pytest.fixture(scope="session")
def tenant_a(tenants: tuple[TenantSeed, TenantSeed]) -> TenantSeed:
    """Alias for the first seed — ergonomic access in tests."""
    return tenants[0]


@pytest.fixture(scope="session")
def tenant_b(tenants: tuple[TenantSeed, TenantSeed]) -> TenantSeed:
    """Alias for the second seed — ergonomic access in tests."""
    return tenants[1]


# ---------------------------------------------------------------------------
# Backend knobs — testcontainers availability
# ---------------------------------------------------------------------------


def _testcontainers_available() -> bool:
    """Return ``True`` iff the ``testcontainers`` PG driver is importable.

    Used by :mod:`tests.tenant.test_http_surface` and
    :mod:`tests.tenant.test_repository_parity` to guard the PG-only
    RLS-clearing case. Docker-less dev machines skip the PG variant
    cleanly; CI runs both shards by setting
    ``CREWDAY_TEST_DB=postgres``.

    We use :func:`importlib.util.find_spec` rather than an import
    statement so ``mypy --strict`` doesn't trip on the
    ``testcontainers`` package lacking a ``py.typed`` marker — a
    spec lookup is a pure runtime check, no import hit.
    """
    if os.environ.get("CREWDAY_TEST_DB", "").lower() == "postgres":
        return True
    import importlib.util

    return importlib.util.find_spec("testcontainers.postgres") is not None


@pytest.fixture(scope="session")
def testcontainers_available() -> bool:
    """Expose :func:`_testcontainers_available` to tests."""
    return _testcontainers_available()


# ---------------------------------------------------------------------------
# Wire the tenancy middleware to the shared engine per test module
# ---------------------------------------------------------------------------


@pytest.fixture
def wire_uow_to_tenant_engine(
    engine: Engine,
    tenant_session_factory: sessionmaker[Session],
    tenant_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Redirect :func:`app.adapters.db.session.make_uow` to the tenant engine.

    The tenancy middleware opens a fresh UoW per request via
    :func:`app.adapters.db.session.make_uow`; we swap the module-level
    defaults so that UoW lands on the session-scoped engine the
    tenant seed lives in. Also replaces
    :func:`app.tenancy.middleware.get_settings` with the stub-off
    fixture so the real resolver runs.
    """
    import app.adapters.db.session as _session_mod

    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = tenant_session_factory
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: tenant_settings)
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory
