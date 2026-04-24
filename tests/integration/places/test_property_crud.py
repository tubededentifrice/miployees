"""Integration tests for :mod:`app.domain.places.property_service`.

Exercises the full create / update / soft-delete / list round-trip
against a real DB with the tenant filter installed so every domain
function walks the same code paths it will when called from a
FastAPI route handler.

Each test:

* Bootstraps a user + workspace via
  :func:`tests.factories.identity.bootstrap_workspace` (which seeds
  the ``owners`` permission group + the self-grant).
* Sets a :class:`WorkspaceContext` for that workspace so the ORM
  filter and the audit writer both see a live context.
* Calls the domain service and asserts the resulting rows + the
  matching ``audit_log`` entries.

See ``docs/specs/04-properties-and-stays.md`` §"Property" /
§"`address_json` canonical shape".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.domain.places.property_service import (
    AddressCountryMismatch,
    PropertyCreate,
    PropertyNotFound,
    PropertyUpdate,
    create_property,
    get_property,
    list_properties,
    soft_delete_property,
    update_property,
)
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"pr-crud-{_SLUG_COUNTER:05d}"


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register workspace-scoped tables this test module depends on.

    A sibling unit test (``tests/unit/test_tenancy_orm_filter.py``)
    resets the process-wide registry in its autouse fixture. Without
    re-registering here the filter silently no-ops on subsequent
    tests — a soft failure mode we want the test to prove it doesn't
    rely on.
    """
    registry.register("property_workspace")
    registry.register("audit_log")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to the given workspace."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLP",
    )


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext]]:
    """Yield a ``(session, ctx)`` pair bound to a fresh workspace.

    Builds on the parent conftest's ``db_session`` fixture — rolled
    back on teardown. Installs the tenant filter on the session
    directly so the ORM filter is active for every query the domain
    service runs, matching the production path.
    """
    install_tenant_filter(db_session)

    slug = _next_slug()
    clock = FrozenClock(_PINNED)

    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"User {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = _ctx_for(ws.id, ws.slug, user.id)

    token = set_current(ctx)
    try:
        yield db_session, ctx
    finally:
        reset_current(token)


def _create_body(
    *,
    name: str = "Villa Sud",
    address: str = "12 Chemin des Oliviers, Antibes",
    country: str = "FR",
    address_country: str | None = "FR",
    kind: str = "str",
    timezone: str = "Europe/Paris",
    **overrides: object,
) -> PropertyCreate:
    data: dict[str, object] = {
        "name": name,
        "address": address,
        "country": country,
        "kind": kind,
        "timezone": timezone,
        "address_json": {
            "line1": "12 Chemin des Oliviers",
            "line2": None,
            "city": "Antibes",
            "state_province": "Alpes-Maritimes",
            "postal_code": "06600",
            "country": address_country,
        },
    }
    data.update(overrides)
    return PropertyCreate.model_validate(data)


def _update_body(
    *,
    name: str = "Villa Sud",
    address: str = "12 Chemin des Oliviers, Antibes",
    country: str = "FR",
    address_country: str | None = "FR",
    kind: str = "str",
    timezone: str = "Europe/Paris",
    **overrides: object,
) -> PropertyUpdate:
    data: dict[str, object] = {
        "name": name,
        "address": address,
        "country": country,
        "kind": kind,
        "timezone": timezone,
        "address_json": {
            "line1": "12 Chemin des Oliviers",
            "line2": None,
            "city": "Antibes",
            "state_province": "Alpes-Maritimes",
            "postal_code": "06600",
            "country": address_country,
        },
    }
    data.update(overrides)
    return PropertyUpdate.model_validate(data)


# ---------------------------------------------------------------------------
# Round-trip CRUD
# ---------------------------------------------------------------------------


class TestCreate:
    """Create inserts property + owner_workspace junction atomically."""

    def test_round_trip_create(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        view = create_property(session, ctx, body=_create_body(), clock=clock)

        # Property row lands.
        row = session.get(Property, view.id)
        assert row is not None
        assert row.name == "Villa Sud"
        assert row.kind == "str"
        assert row.country == "FR"
        assert row.address_json["country"] == "FR"

        # Junction row links to the caller's workspace as owner.
        junctions = session.scalars(
            select(PropertyWorkspace).where(PropertyWorkspace.property_id == view.id)
        ).all()
        assert len(junctions) == 1
        assert junctions[0].workspace_id == ctx.workspace_id
        assert junctions[0].membership_role == "owner_workspace"
        assert junctions[0].label == "Villa Sud"

        # Audit row lands in the same transaction.
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        assert len(audits) == 1
        assert audits[0].action == "create"
        assert audits[0].entity_kind == "property"


class TestUpdate:
    """Update is workspace-scoped; mutable body is replaced."""

    def test_round_trip_update(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        view = create_property(session, ctx, body=_create_body(), clock=clock)

        later = FrozenClock(_PINNED.replace(hour=13))
        updated = update_property(
            session,
            ctx,
            property_id=view.id,
            body=_update_body(name="Villa Nord", kind="vacation"),
            clock=later,
        )
        assert updated.name == "Villa Nord"
        assert updated.kind == "vacation"

        # Row-level confirmation — the ORM tenant filter only applies
        # to workspace-scoped tables, but ``property`` is tenant-
        # agnostic; a bare read round-trips the new value.
        row = session.get(Property, view.id)
        assert row is not None
        assert row.name == "Villa Nord"

    def test_update_cross_tenant_denied(
        self, env: tuple[Session, WorkspaceContext], db_session: Session
    ) -> None:
        """A property not linked to workspace B is invisible from B."""
        session, ctx_a = env
        clock = FrozenClock(_PINNED)
        created = create_property(session, ctx_a, body=_create_body(), clock=clock)

        # Build a second workspace + ctx in the same DB session.
        slug_b = _next_slug()
        user_b = bootstrap_user(
            session,
            email=f"{slug_b}@example.com",
            display_name=f"User {slug_b}",
            clock=clock,
        )
        ws_b = bootstrap_workspace(
            session,
            slug=slug_b,
            name=f"WS {slug_b}",
            owner_user_id=user_b.id,
            clock=clock,
        )
        ctx_b = _ctx_for(ws_b.id, ws_b.slug, user_b.id)

        token = set_current(ctx_b)
        try:
            with pytest.raises(PropertyNotFound):
                update_property(
                    session,
                    ctx_b,
                    property_id=created.id,
                    body=_update_body(name="Injected"),
                    clock=clock,
                )
        finally:
            reset_current(token)

        # Row unchanged.
        token = set_current(ctx_a)
        try:
            unchanged = get_property(session, ctx_a, property_id=created.id)
        finally:
            reset_current(token)
        assert unchanged.name == "Villa Sud"


class TestSoftDelete:
    """Soft-delete stamps ``deleted_at`` + hides from default list."""

    def test_round_trip_soft_delete(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        view = create_property(session, ctx, body=_create_body(), clock=clock)

        later = FrozenClock(_PINNED.replace(hour=14))
        soft_delete_property(session, ctx, property_id=view.id, clock=later)

        # Hidden from default list.
        assert list_properties(session, ctx) == []
        retired = list_properties(session, ctx, deleted=True)
        assert len(retired) == 1
        assert retired[0].id == view.id
        assert retired[0].deleted_at is not None

        # Audit row lands.
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == view.id,
                AuditLog.action == "delete",
            )
        ).all()
        assert len(audits) == 1


class TestListFilters:
    """The list path joins through ``property_workspace`` per workspace."""

    def test_list_filters_by_workspace(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx_a = env
        clock = FrozenClock(_PINNED)
        create_property(session, ctx_a, body=_create_body(name="A-1"), clock=clock)
        create_property(session, ctx_a, body=_create_body(name="A-2"), clock=clock)

        # Second workspace with its own property.
        slug_b = _next_slug()
        user_b = bootstrap_user(
            session,
            email=f"{slug_b}@example.com",
            display_name=f"User {slug_b}",
            clock=clock,
        )
        ws_b = bootstrap_workspace(
            session,
            slug=slug_b,
            name=f"WS {slug_b}",
            owner_user_id=user_b.id,
            clock=clock,
        )
        ctx_b = _ctx_for(ws_b.id, ws_b.slug, user_b.id)

        token = set_current(ctx_b)
        try:
            create_property(session, ctx_b, body=_create_body(name="B-1"), clock=clock)
        finally:
            reset_current(token)

        # ctx_a only sees A-1 + A-2.
        listed_a = list_properties(session, ctx_a)
        assert sorted(v.name for v in listed_a) == ["A-1", "A-2"]

        # ctx_b only sees B-1.
        token = set_current(ctx_b)
        try:
            listed_b = list_properties(session, ctx_b)
        finally:
            reset_current(token)
        assert [v.name for v in listed_b] == ["B-1"]


class TestAddressBackfill:
    """Round-trip the §04 back-fill rule against a real DB."""

    def test_backfill_from_address_json(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        body = PropertyCreate.model_validate(
            {
                "name": "Only JSON country",
                "address": "Somewhere",
                "kind": "residence",
                "timezone": "Europe/Paris",
                "address_json": {"country": "PT"},
            }
        )
        view = create_property(session, ctx, body=body, clock=clock)
        assert view.country == "PT"
        assert view.address_json["country"] == "PT"

        row = session.get(Property, view.id)
        assert row is not None
        assert row.country == "PT"
        assert row.address_json["country"] == "PT"

    def test_backfill_from_country_column(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        body = PropertyCreate.model_validate(
            {
                "name": "Only column country",
                "address": "Somewhere",
                "country": "ES",
                "kind": "residence",
                "timezone": "Europe/Madrid",
                "address_json": {},
            }
        )
        view = create_property(session, ctx, body=body, clock=clock)
        assert view.country == "ES"
        assert view.address_json["country"] == "ES"

    def test_mismatched_country_raises(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        with pytest.raises(AddressCountryMismatch):
            create_property(
                session,
                ctx,
                body=_create_body(country="FR", address_country="IT"),
                clock=clock,
            )
