"""Unit tests for :mod:`app.domain.places.property_service`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_oneoff.py``: a fresh engine per test, pull
every sibling ``models`` module onto the shared ``Base.metadata``,
run ``Base.metadata.create_all``, drive the domain code with a
:class:`FrozenClock`.

Covers cd-8u5:

* Happy-path create — ``property`` + ``property_workspace`` land in
  one transaction with ``membership_role = 'owner_workspace'``;
  label defaults to ``name`` when the caller omits it.
* Happy-path update — mutable body is replaced, ``updated_at`` is
  bumped, audit diff carries before + after.
* Happy-path soft-delete — ``deleted_at`` is stamped, the row is
  hidden from the default list, audit row is written.
* List + get — workspace-scoped via the junction, soft-deleted rows
  excluded by default, ``deleted=True`` surfaces retired rows.
* Address back-fill — ``address_json.country`` → ``property.country``
  and the reverse direction; a mismatched body raises 422.
* Cross-tenant denial — a property linked only to workspace A is
  invisible to workspace B (``get`` / ``update`` / ``soft_delete``
  all raise :class:`PropertyNotFound`).
* Audit on every mutation.

See ``docs/specs/04-properties-and-stays.md`` §"Property" and
§"`address_json` canonical shape".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.places.property_service import (
    AddressCountryMismatch,
    AddressPayload,
    PropertyCreate,
    PropertyNotFound,
    PropertyUpdate,
    PropertyView,
    create_property,
    get_property,
    list_properties,
    soft_delete_property,
    update_property,
)
from app.services.places import create_property as public_create
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"


def _same_moment(loaded: datetime, expected: datetime) -> bool:
    """Return ``True`` if the two datetimes represent the same UTC instant.

    SQLite strips ``tzinfo`` off ``DateTime(timezone=True)`` columns on
    read; Postgres preserves it. Rather than coerce in every assertion,
    this helper coerces at comparison time.
    """
    if loaded.tzinfo is None:
        loaded = loaded.replace(tzinfo=UTC)
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    return loaded == expected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture(name="engine_places")
def fixture_engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_places")
def fixture_session(engine_places: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed here.

    The tenancy filter's ORM hook only applies to explicitly-registered
    scoped tables; this service's reads join through
    ``property_workspace`` (which IS registered at module import), but
    the per-workspace predicate is also threaded manually by
    :func:`_load_row` — so the filter's absence doesn't leak a
    cross-tenant read.
    """
    factory = sessionmaker(bind=engine_places, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str, *, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _body(
    *,
    name: str = "Villa Sud",
    kind: str = "str",
    address: str = "12 Chemin des Oliviers, Antibes",
    country: str | None = "FR",
    address_country: str | None = "FR",
    timezone: str = "Europe/Paris",
    label: str | None = None,
    **overrides: object,
) -> PropertyCreate:
    """Build a ``PropertyCreate`` payload with sensible defaults."""
    data: dict[str, object] = {
        "name": name,
        "kind": kind,
        "address": address,
        "address_json": {
            "line1": "12 Chemin des Oliviers",
            "line2": None,
            "city": "Antibes",
            "state_province": "Alpes-Maritimes",
            "postal_code": "06600",
            "country": address_country,
        },
        "country": country,
        "timezone": timezone,
    }
    if label is not None:
        data["label"] = label
    data.update(overrides)
    return PropertyCreate.model_validate(data)


def _update_body(
    *,
    name: str = "Villa Sud",
    kind: str = "str",
    address: str = "12 Chemin des Oliviers, Antibes",
    country: str | None = "FR",
    address_country: str | None = "FR",
    timezone: str = "Europe/Paris",
    **overrides: object,
) -> PropertyUpdate:
    data: dict[str, object] = {
        "name": name,
        "kind": kind,
        "address": address,
        "address_json": {
            "line1": "12 Chemin des Oliviers",
            "line2": None,
            "city": "Antibes",
            "state_province": "Alpes-Maritimes",
            "postal_code": "06600",
            "country": address_country,
        },
        "country": country,
        "timezone": timezone,
    }
    data.update(overrides)
    return PropertyUpdate.model_validate(data)


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


class TestDTO:
    """``PropertyCreate`` / ``PropertyUpdate`` reject bad shapes on ingress."""

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "address": "y",
                    "timezone": "Europe/Paris",
                    "bogus": "nope",
                }
            )

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "   ",
                    "address": "y",
                    "timezone": "Europe/Paris",
                }
            )

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "kind": "yacht",
                    "address": "y",
                    "timezone": "Europe/Paris",
                }
            )

    def test_country_must_be_two_letters(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "address": "y",
                    "country": "FRA",
                    "timezone": "Europe/Paris",
                }
            )

    def test_country_must_be_letters(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "address": "y",
                    "country": "12",
                    "timezone": "Europe/Paris",
                }
            )

    def test_country_upper_cased(self) -> None:
        body = PropertyCreate.model_validate(
            {
                "name": "x",
                "address": "y",
                "country": "fr",
                "timezone": "Europe/Paris",
            }
        )
        assert body.country == "FR"

    def test_default_currency_upper_cased(self) -> None:
        body = PropertyCreate.model_validate(
            {
                "name": "x",
                "address": "y",
                "country": "FR",
                "default_currency": "eur",
                "timezone": "Europe/Paris",
            }
        )
        assert body.default_currency == "EUR"

    def test_duplicate_tags_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "address": "y",
                    "country": "FR",
                    "timezone": "Europe/Paris",
                    "tags_json": ["a", "a"],
                }
            )

    def test_blank_tag_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "address": "y",
                    "country": "FR",
                    "timezone": "Europe/Paris",
                    "tags_json": ["a", "   "],
                }
            )

    def test_address_payload_country_upper_cased(self) -> None:
        payload = AddressPayload.model_validate({"country": "fr"})
        assert payload.country == "FR"

    def test_address_payload_accepts_blank_fields(self) -> None:
        payload = AddressPayload()
        assert payload.country is None
        assert payload.line1 is None

    def test_bogus_timezone_rejected(self) -> None:
        """§04 "Property" — ``timezone`` must be a real IANA zone.

        Surfacing the bad value at write time keeps the failure close
        to the caller; without this check the string survives the DB
        round-trip and silently coerces to UTC at task-generation time
        (``app.domain.tasks.oneoff._resolve_property_zone``).
        """
        with pytest.raises(ValueError, match="valid IANA zone"):
            PropertyCreate.model_validate(
                {
                    "name": "x",
                    "address": "y",
                    "country": "FR",
                    "timezone": "Not/AZone",
                }
            )

    def test_valid_timezone_accepted(self) -> None:
        body = PropertyCreate.model_validate(
            {
                "name": "x",
                "address": "y",
                "country": "FR",
                "timezone": "America/New_York",
            }
        )
        assert body.timezone == "America/New_York"


# ---------------------------------------------------------------------------
# Create happy path
# ---------------------------------------------------------------------------


class TestCreate:
    """``create_property`` inserts property + junction in one flush."""

    def test_create_inserts_property_and_junction(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="create-ok")
        ctx = _ctx(ws, slug="create-ok")

        view = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        assert isinstance(view, PropertyView)
        assert view.name == "Villa Sud"
        assert view.kind == "str"
        assert view.country == "FR"
        assert view.address_json["country"] == "FR"
        assert view.deleted_at is None
        assert _same_moment(view.created_at, _PINNED)
        assert view.updated_at is not None
        assert _same_moment(view.updated_at, _PINNED)

        # Junction row landed in the same transaction.
        junctions = session_places.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == view.id,
            )
        ).all()
        assert len(junctions) == 1
        junction = junctions[0]
        assert junction.workspace_id == ws
        assert junction.membership_role == "owner_workspace"
        assert junction.label == view.name
        assert _same_moment(junction.created_at, _PINNED)

    def test_create_uses_explicit_label(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="create-label")
        ctx = _ctx(ws, slug="create-label")

        view = create_property(
            session_places,
            ctx,
            body=_body(label="VS — agency A"),
            clock=frozen_clock,
        )

        junction = session_places.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == view.id,
            )
        ).one()
        assert junction.label == "VS — agency A"

    def test_create_writes_audit_row(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="create-audit")
        ctx = _ctx(ws, slug="create-audit")

        view = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        audits = session_places.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.entity_kind == "property"
        assert audit.action == "create"
        assert audit.workspace_id == ws
        assert audit.diff["after"]["id"] == view.id
        assert audit.diff["after"]["name"] == "Villa Sud"
        assert audit.diff["after"]["country"] == "FR"

    def test_create_public_surface_matches_domain(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """The ``app.services.places`` re-export is a drop-in for the domain fn."""
        ws = _bootstrap_workspace(session_places, slug="public-surface")
        ctx = _ctx(ws, slug="public-surface")

        view = public_create(session_places, ctx, body=_body(), clock=frozen_clock)
        assert isinstance(view, PropertyView)
        assert view.name == "Villa Sud"


# ---------------------------------------------------------------------------
# Address-country back-fill
# ---------------------------------------------------------------------------


class TestAddressBackfill:
    """§04 "`address_json` canonical shape" — country stays in sync."""

    def test_backfill_from_address_json(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """``address_json.country`` set + ``country`` absent → copy to column."""
        ws = _bootstrap_workspace(session_places, slug="bf-json")
        ctx = _ctx(ws, slug="bf-json")

        view = create_property(
            session_places,
            ctx,
            body=_body(country=None, address_country="IT"),
            clock=frozen_clock,
        )

        assert view.country == "IT"
        assert view.address_json["country"] == "IT"

    def test_backfill_from_country_column(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """``country`` set + ``address_json.country`` absent → fill into JSON."""
        ws = _bootstrap_workspace(session_places, slug="bf-col")
        ctx = _ctx(ws, slug="bf-col")

        view = create_property(
            session_places,
            ctx,
            body=_body(country="DE", address_country=None),
            clock=frozen_clock,
        )

        assert view.country == "DE"
        assert view.address_json["country"] == "DE"

    def test_both_absent_rejected(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """Neither column set → ``ValueError`` before flush."""
        ws = _bootstrap_workspace(session_places, slug="bf-empty")
        ctx = _ctx(ws, slug="bf-empty")

        with pytest.raises(ValueError, match="country is required"):
            create_property(
                session_places,
                ctx,
                body=_body(country=None, address_country=None),
                clock=frozen_clock,
            )

    def test_mismatched_country_raises(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """Both set, different values → :class:`AddressCountryMismatch`."""
        ws = _bootstrap_workspace(session_places, slug="bf-mismatch")
        ctx = _ctx(ws, slug="bf-mismatch")

        with pytest.raises(AddressCountryMismatch):
            create_property(
                session_places,
                ctx,
                body=_body(country="FR", address_country="IT"),
                clock=frozen_clock,
            )

    def test_matching_countries_accepted(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """Both set, same value → both stored (case-normalised)."""
        ws = _bootstrap_workspace(session_places, slug="bf-match")
        ctx = _ctx(ws, slug="bf-match")

        view = create_property(
            session_places,
            ctx,
            body=_body(country="fr", address_country="FR"),
            clock=frozen_clock,
        )

        assert view.country == "FR"
        assert view.address_json["country"] == "FR"

    def test_update_backfill_preserves_existing_when_body_silent(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """Update body with no country → keep the row's existing value."""
        ws = _bootstrap_workspace(session_places, slug="bf-update-silent")
        ctx = _ctx(ws, slug="bf-update-silent")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        # The row has country="FR". Submit an update whose body + JSON
        # both omit country.
        body = _update_body(country=None, address_country=None)
        updated = update_property(
            session_places,
            ctx,
            property_id=created.id,
            body=body,
            clock=frozen_clock,
        )
        assert updated.country == "FR"
        assert updated.address_json["country"] == "FR"

    def test_update_country_change_back_fills_json(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """Update that changes ``country`` propagates to ``address_json``."""
        ws = _bootstrap_workspace(session_places, slug="bf-update-change")
        ctx = _ctx(ws, slug="bf-update-change")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        updated = update_property(
            session_places,
            ctx,
            property_id=created.id,
            body=_update_body(country="ES", address_country=None),
            clock=frozen_clock,
        )
        assert updated.country == "ES"
        assert updated.address_json["country"] == "ES"

    def test_update_mismatched_country_raises(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """Update body with disagreeing ``country`` + ``address_json.country``.

        The mismatch invariant applies uniformly on every write — the
        create path has the same rule (``test_mismatched_country_raises``)
        and the update path MUST fail the same way rather than silently
        pick one side.
        """
        ws = _bootstrap_workspace(session_places, slug="bf-update-mismatch")
        ctx = _ctx(ws, slug="bf-update-mismatch")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        with pytest.raises(AddressCountryMismatch):
            update_property(
                session_places,
                ctx,
                property_id=created.id,
                body=_update_body(country="FR", address_country="IT"),
                clock=frozen_clock,
            )
        # The row is unchanged — mismatch fires before the flush.
        unchanged = get_property(session_places, ctx, property_id=created.id)
        assert unchanged.country == "FR"
        assert unchanged.address_json["country"] == "FR"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    """Workspace-scoped; the whole mutable body is replaced."""

    def test_update_replaces_body(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="up-replace")
        ctx = _ctx(ws, slug="up-replace")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        later = FrozenClock(_PINNED.replace(hour=13))
        updated = update_property(
            session_places,
            ctx,
            property_id=created.id,
            body=_update_body(
                name="Villa Nord",
                kind="vacation",
                address="3 Chemin du Lac, Annecy",
                country="FR",
                address_country="FR",
                timezone="Europe/Paris",
                property_notes_md="Renamed after renovation.",
                tags_json=["riviera", "private-pool"],
            ),
            clock=later,
        )

        assert updated.name == "Villa Nord"
        assert updated.kind == "vacation"
        assert updated.address == "3 Chemin du Lac, Annecy"
        assert updated.property_notes_md == "Renamed after renovation."
        assert updated.tags_json == ("riviera", "private-pool")
        assert updated.updated_at is not None
        assert _same_moment(updated.updated_at, later.now())
        # ``created_at`` survives the update untouched.
        assert _same_moment(updated.created_at, created.created_at)

    def test_update_writes_audit_with_before_after(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="up-audit")
        ctx = _ctx(ws, slug="up-audit")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        update_property(
            session_places,
            ctx,
            property_id=created.id,
            body=_update_body(name="Villa Nord"),
            clock=frozen_clock,
        )

        audits = session_places.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == created.id,
                AuditLog.action == "update",
            )
        ).all()
        assert len(audits) == 1
        diff = audits[0].diff
        assert diff["before"]["name"] == "Villa Sud"
        assert diff["after"]["name"] == "Villa Nord"

    def test_update_missing_raises_not_found(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="up-missing")
        ctx = _ctx(ws, slug="up-missing")

        with pytest.raises(PropertyNotFound):
            update_property(
                session_places,
                ctx,
                property_id="01HWA00000000000000000PRPZ",
                body=_update_body(),
                clock=frozen_clock,
            )


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


class TestSoftDelete:
    """Soft-delete hides the row from the default list; audit records it."""

    def test_soft_delete_stamps_timestamp(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="del-ok")
        ctx = _ctx(ws, slug="del-ok")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        later = FrozenClock(_PINNED.replace(hour=14))
        deleted = soft_delete_property(
            session_places,
            ctx,
            property_id=created.id,
            clock=later,
        )

        assert deleted.deleted_at is not None
        assert _same_moment(deleted.deleted_at, later.now())
        assert deleted.updated_at is not None
        assert _same_moment(deleted.updated_at, later.now())

    def test_soft_delete_hides_from_default_list(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="del-list")
        ctx = _ctx(ws, slug="del-list")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        soft_delete_property(
            session_places, ctx, property_id=created.id, clock=frozen_clock
        )

        live = list_properties(session_places, ctx)
        assert live == []
        retired = list_properties(session_places, ctx, deleted=True)
        assert len(retired) == 1
        assert retired[0].id == created.id

    def test_soft_delete_get_hides_by_default(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="del-get")
        ctx = _ctx(ws, slug="del-get")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        soft_delete_property(
            session_places, ctx, property_id=created.id, clock=frozen_clock
        )

        with pytest.raises(PropertyNotFound):
            get_property(session_places, ctx, property_id=created.id)

        # ``include_deleted`` surfaces the retired row.
        view = get_property(
            session_places,
            ctx,
            property_id=created.id,
            include_deleted=True,
        )
        assert view.deleted_at is not None

    def test_soft_delete_writes_audit(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="del-audit")
        ctx = _ctx(ws, slug="del-audit")
        created = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        soft_delete_property(
            session_places, ctx, property_id=created.id, clock=frozen_clock
        )

        audits = session_places.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == created.id,
                AuditLog.action == "delete",
            )
        ).all()
        assert len(audits) == 1
        diff = audits[0].diff
        assert diff["before"]["deleted_at"] is None
        assert diff["after"]["deleted_at"] is not None

    def test_soft_delete_missing_raises_not_found(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="del-404")
        ctx = _ctx(ws, slug="del-404")

        with pytest.raises(PropertyNotFound):
            soft_delete_property(
                session_places,
                ctx,
                property_id="01HWA00000000000000000PRPX",
                clock=frozen_clock,
            )


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestCrossTenant:
    """A property linked only to workspace A is invisible to workspace B."""

    def test_get_cross_tenant_hidden(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _bootstrap_workspace(session_places, slug="ct-a")
        ws_b = _bootstrap_workspace(session_places, slug="ct-b")
        ctx_a = _ctx(ws_a, slug="ct-a")
        ctx_b = _ctx(ws_b, slug="ct-b")

        created = create_property(
            session_places, ctx_a, body=_body(), clock=frozen_clock
        )
        # Confirm A can see it.
        visible = get_property(session_places, ctx_a, property_id=created.id)
        assert visible.id == created.id

        # B gets a 404.
        with pytest.raises(PropertyNotFound):
            get_property(session_places, ctx_b, property_id=created.id)

    def test_update_cross_tenant_denied(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _bootstrap_workspace(session_places, slug="ct-update-a")
        ws_b = _bootstrap_workspace(session_places, slug="ct-update-b")
        ctx_a = _ctx(ws_a, slug="ct-update-a")
        ctx_b = _ctx(ws_b, slug="ct-update-b")

        created = create_property(
            session_places, ctx_a, body=_body(), clock=frozen_clock
        )

        with pytest.raises(PropertyNotFound):
            update_property(
                session_places,
                ctx_b,
                property_id=created.id,
                body=_update_body(name="Injected"),
                clock=frozen_clock,
            )

        # Row unchanged — confirm A still sees the original.
        unchanged = get_property(session_places, ctx_a, property_id=created.id)
        assert unchanged.name == "Villa Sud"

    def test_soft_delete_cross_tenant_denied(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _bootstrap_workspace(session_places, slug="ct-del-a")
        ws_b = _bootstrap_workspace(session_places, slug="ct-del-b")
        ctx_a = _ctx(ws_a, slug="ct-del-a")
        ctx_b = _ctx(ws_b, slug="ct-del-b")

        created = create_property(
            session_places, ctx_a, body=_body(), clock=frozen_clock
        )

        with pytest.raises(PropertyNotFound):
            soft_delete_property(
                session_places,
                ctx_b,
                property_id=created.id,
                clock=frozen_clock,
            )

        # Still alive for A.
        live = get_property(session_places, ctx_a, property_id=created.id)
        assert live.deleted_at is None

    def test_list_workspace_scoped(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _bootstrap_workspace(session_places, slug="ct-list-a")
        ws_b = _bootstrap_workspace(session_places, slug="ct-list-b")
        ctx_a = _ctx(ws_a, slug="ct-list-a")
        ctx_b = _ctx(ws_b, slug="ct-list-b")

        create_property(
            session_places,
            ctx_a,
            body=_body(name="Villa Sud"),
            clock=frozen_clock,
        )
        create_property(
            session_places,
            ctx_b,
            body=_body(name="Chalet Nord"),
            clock=frozen_clock,
        )

        listed_a = list_properties(session_places, ctx_a)
        listed_b = list_properties(session_places, ctx_b)
        assert [v.name for v in listed_a] == ["Villa Sud"]
        assert [v.name for v in listed_b] == ["Chalet Nord"]

    def test_multi_belonging_property_visible_from_both(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        """A property with junction rows in two workspaces shows up in both.

        The sharing flow (cd-hsk) materialises the second junction
        row — this service owns only the ``owner_workspace`` seed.
        We simulate the outcome by inserting the second junction by
        hand, then confirm both workspaces see the property and can
        read its state. Update is still only allowed from workspaces
        whose junction exists (both, in this case).
        """
        ws_a = _bootstrap_workspace(session_places, slug="multi-a")
        ws_b = _bootstrap_workspace(session_places, slug="multi-b")
        ctx_a = _ctx(ws_a, slug="multi-a")
        ctx_b = _ctx(ws_b, slug="multi-b")
        created = create_property(
            session_places, ctx_a, body=_body(), clock=frozen_clock
        )

        # Share with B as a managed workspace.
        session_places.add(
            PropertyWorkspace(
                property_id=created.id,
                workspace_id=ws_b,
                label="Shared with B",
                membership_role="managed_workspace",
                created_at=_PINNED,
            )
        )
        session_places.flush()

        # Both workspaces see the property.
        view_a = get_property(session_places, ctx_a, property_id=created.id)
        view_b = get_property(session_places, ctx_b, property_id=created.id)
        assert view_a.id == view_b.id == created.id

        # Both lists include it.
        assert len(list_properties(session_places, ctx_a)) == 1
        assert len(list_properties(session_places, ctx_b)) == 1


# ---------------------------------------------------------------------------
# List filters
# ---------------------------------------------------------------------------


class TestList:
    """``list_properties`` filters and ordering."""

    def test_list_orders_by_created_at(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="list-order")
        ctx = _ctx(ws, slug="list-order")
        first = create_property(
            session_places,
            ctx,
            body=_body(name="Villa A"),
            clock=FrozenClock(_PINNED),
        )
        second = create_property(
            session_places,
            ctx,
            body=_body(name="Villa B"),
            clock=FrozenClock(_PINNED.replace(hour=13)),
        )

        listed = list_properties(session_places, ctx)
        assert [v.id for v in listed] == [first.id, second.id]

    def test_list_kind_filter(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="list-kind")
        ctx = _ctx(ws, slug="list-kind")
        create_property(
            session_places,
            ctx,
            body=_body(name="STR-1", kind="str"),
            clock=frozen_clock,
        )
        create_property(
            session_places,
            ctx,
            body=_body(name="RES-1", kind="residence"),
            clock=frozen_clock,
        )

        only_str = list_properties(session_places, ctx, kind="str")
        assert [v.name for v in only_str] == ["STR-1"]

    def test_list_q_matches_name_and_address(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="list-q")
        ctx = _ctx(ws, slug="list-q")
        create_property(
            session_places,
            ctx,
            body=_body(name="Villa Sud", address="12 Chemin Antibes"),
            clock=frozen_clock,
        )
        create_property(
            session_places,
            ctx,
            body=_body(name="Chalet Cœur", address="3 Rue Megève"),
            clock=frozen_clock,
        )

        listed = list_properties(session_places, ctx, q="antibes")
        assert [v.name for v in listed] == ["Villa Sud"]

    def test_list_q_case_insensitive(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="list-qci")
        ctx = _ctx(ws, slug="list-qci")
        create_property(
            session_places,
            ctx,
            body=_body(name="VILLA SUD"),
            clock=frozen_clock,
        )

        listed = list_properties(session_places, ctx, q="villa")
        assert len(listed) == 1


# ---------------------------------------------------------------------------
# Row projection / view shape
# ---------------------------------------------------------------------------


class TestView:
    """The :class:`PropertyView` is a frozen / slotted dataclass."""

    def test_view_is_frozen(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="view-frozen")
        ctx = _ctx(ws, slug="view-frozen")

        view = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            view.name = "changed"  # type: ignore[misc]

    def test_view_carries_tags_as_tuple(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="view-tags")
        ctx = _ctx(ws, slug="view-tags")

        view = create_property(
            session_places,
            ctx,
            body=_body(tags_json=["riviera", "off-season"]),
            clock=frozen_clock,
        )

        assert isinstance(view.tags_json, tuple)
        assert view.tags_json == ("riviera", "off-season")


# ---------------------------------------------------------------------------
# Row-shape sanity checks
# ---------------------------------------------------------------------------


class TestRowShape:
    """Sanity-check the ``Property`` row the service writes."""

    def test_row_carries_canonical_address_json(
        self, session_places: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session_places, slug="row-address")
        ctx = _ctx(ws, slug="row-address")

        view = create_property(session_places, ctx, body=_body(), clock=frozen_clock)

        row = session_places.scalars(
            select(Property).where(Property.id == view.id)
        ).one()
        assert row.address_json == {
            "line1": "12 Chemin des Oliviers",
            "line2": None,
            "city": "Antibes",
            "state_province": "Alpes-Maritimes",
            "postal_code": "06600",
            "country": "FR",
        }
        assert row.country == "FR"
        assert row.name == "Villa Sud"
        assert row.deleted_at is None
