"""Unit tests for :mod:`app.services.workspace.settings_service` (cd-n6p).

In-memory SQLite bootstrap mirrors
:file:`tests/unit/services/test_service_employees.py`: pull every
sibling ``models`` module onto the shared ``Base.metadata``, run
``Base.metadata.create_all``, drive the domain code with a
:class:`FrozenClock`. Authorisation seam (``owners`` group
membership) is provided by
:func:`app.adapters.db.authz.bootstrap.seed_owners_system_group`.

Coverage matrix (cd-n6p acceptance criteria):

* Valid full update — every field accepted; row + audit reflect the
  change.
* Partial update — only the supplied non-``None`` field is written.
* Invalid timezone → :class:`WorkspaceFieldInvalid` with
  ``field='timezone'`` (DB untouched).
* Invalid locale → :class:`WorkspaceFieldInvalid` with
  ``field='locale'``.
* Invalid currency → :class:`WorkspaceFieldInvalid` with
  ``field='currency'``.
* Non-owner caller → :class:`OwnersOnlyError` (DB untouched).
* Audit row carries the per-field old / new values for every change.
* ``updated_at`` bumped on a real change.
* Empty-update no-op — no audit row, ``updated_at`` not bumped.
* Same-value-only update — no audit row, ``updated_at`` not bumped.

See ``docs/specs/02-domain-model.md`` §"workspaces" /
§"Settings cascade", ``docs/specs/05-employees-and-roles.md``
§"Surface grants at a glance", ``docs/specs/14-web-frontend.md``
§"Workspace settings".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db as adapters_db_pkg
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.services.workspace import (
    OwnersOnlyError,
    WorkspaceFieldInvalid,
    update_basics,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    for modinfo in pkgutil.iter_modules(
        adapters_db_pkg.__path__, prefix=f"{adapters_db_pkg.__name__}."
    ):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine_workspace")
def fixture_engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_workspace")
def fixture_session(engine_workspace: Engine) -> Iterator[Session]:
    """Fresh session per test; tenant filter not installed (unit scope)."""
    factory = sessionmaker(
        bind=engine_workspace, expire_on_commit=False, class_=Session
    )
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str, *, actor_id: str, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(
    session: Session,
    *,
    slug: str = "acme",
    name: str = "Acme",
    default_timezone: str = "UTC",
    default_locale: str = "en",
    default_currency: str = "USD",
) -> Workspace:
    """Insert a fresh :class:`Workspace` and return it.

    All four basics fields are explicit so each test can pin a
    starting state and assert the change deterministically.
    """
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=name,
        plan="free",
        quota_json={},
        settings_json={},
        default_timezone=default_timezone,
        default_locale=default_locale,
        default_currency=default_currency,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _seed_user(
    session: Session, *, user_id: str, email: str, display_name: str
) -> User:
    """Insert a :class:`User` row needed for the FK on ``permission_group_member``."""
    user = User(
        id=user_id,
        email=email,
        email_lower=email.lower(),
        display_name=display_name,
        locale=None,
        timezone=None,
        created_at=_PINNED,
    )
    session.add(user)
    session.flush()
    return user


def _seed_owner(
    session: Session, *, ws: Workspace, owner_user_id: str, clock: FrozenClock
) -> WorkspaceContext:
    """Seed the owners group + return a ctx with ``owner_user_id`` as actor.

    Inserts a :class:`User` row first so the FK on
    ``permission_group_member.user_id`` resolves; tests that need
    multiple owners across workspaces call this once per (ws, owner)
    pair.
    """
    _seed_user(
        session,
        user_id=owner_user_id,
        email=f"{owner_user_id.lower()}@example.com",
        display_name=f"Owner-{owner_user_id[-4:]}",
    )
    ctx = _ctx(ws.id, actor_id=owner_user_id, slug=ws.slug)
    seed_owners_system_group(
        session,
        ctx,
        workspace_id=ws.id,
        owner_user_id=owner_user_id,
        clock=clock,
    )
    session.flush()
    return ctx


def _basics_audit_rows(session: Session, *, workspace_id: str) -> list[AuditLog]:
    """Return only the ``workspace.basics_updated`` rows for a workspace."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_id == workspace_id,
                AuditLog.action == "workspace.basics_updated",
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )


# ---------------------------------------------------------------------------
# Tests — happy paths
# ---------------------------------------------------------------------------


class TestUpdateBasicsHappyPath:
    """Owner-side happy paths: full update, partial update, audit shape."""

    def test_full_update_writes_all_fields_and_audit(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        # Bump the clock so ``updated_at`` lands on a different
        # instant than the seed so the bump is observable.
        write_clock = FrozenClock(_PINNED + timedelta(minutes=5))

        result = update_basics(
            session,
            ctx,
            name="Acme Vacation Rentals",
            timezone="Europe/Paris",
            locale="fr-FR",
            currency="EUR",
            actor_user_id=owner_id,
            clock=write_clock,
        )

        assert result.name == "Acme Vacation Rentals"
        assert result.default_timezone == "Europe/Paris"
        assert result.default_locale == "fr-FR"
        assert result.default_currency == "EUR"
        assert result.updated_at == write_clock.now()

        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.name == "Acme Vacation Rentals"
        assert refreshed.default_timezone == "Europe/Paris"
        assert refreshed.default_locale == "fr-FR"
        assert refreshed.default_currency == "EUR"
        assert refreshed.updated_at == write_clock.now()

        audit = _basics_audit_rows(session, workspace_id=ws.id)
        assert len(audit) == 1
        diff = audit[0].diff
        assert diff["before"] == {
            "name": "Acme",
            "default_timezone": "UTC",
            "default_locale": "en",
            "default_currency": "USD",
        }
        assert diff["after"] == {
            "name": "Acme Vacation Rentals",
            "default_timezone": "Europe/Paris",
            "default_locale": "fr-FR",
            "default_currency": "EUR",
        }

    def test_partial_update_writes_only_supplied_field(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """Only ``name`` is sent; the other three columns stay untouched."""
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)
        write_clock = FrozenClock(_PINNED + timedelta(minutes=5))

        result = update_basics(
            session,
            ctx,
            name="Acme Renamed",
            actor_user_id=owner_id,
            clock=write_clock,
        )
        assert result.name == "Acme Renamed"
        assert result.default_timezone == "UTC"
        assert result.default_locale == "en"
        assert result.default_currency == "USD"

        audit = _basics_audit_rows(session, workspace_id=ws.id)
        assert len(audit) == 1
        diff = audit[0].diff
        # Only the changed field appears in the diff.
        assert diff["before"] == {"name": "Acme"}
        assert diff["after"] == {"name": "Acme Renamed"}

    def test_updated_at_is_bumped_on_real_change(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)
        original_updated_at = ws.updated_at

        write_clock = FrozenClock(_PINNED + timedelta(hours=1))
        result = update_basics(
            session,
            ctx,
            name="Acme Renamed",
            actor_user_id=owner_id,
            clock=write_clock,
        )
        assert result.updated_at != original_updated_at
        assert result.updated_at == write_clock.now()

    def test_currency_normalised_to_uppercase(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """A lowercase currency is normalised before persistence."""
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)
        write_clock = FrozenClock(_PINNED + timedelta(minutes=5))

        result = update_basics(
            session,
            ctx,
            currency="eur",
            actor_user_id=owner_id,
            clock=write_clock,
        )
        assert result.default_currency == "EUR"

    def test_locale_region_normalised_to_uppercase(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """A ``fr-fr`` (lowercase region) is normalised to ``fr-FR``."""
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)
        write_clock = FrozenClock(_PINNED + timedelta(minutes=5))

        result = update_basics(
            session,
            ctx,
            locale="fr-fr",
            actor_user_id=owner_id,
            clock=write_clock,
        )
        assert result.default_locale == "fr-FR"


# ---------------------------------------------------------------------------
# Tests — no-op paths
# ---------------------------------------------------------------------------


class TestUpdateBasicsNoOp:
    """Empty + same-value paths must not bump ``updated_at`` or audit."""

    def test_empty_update_is_noop(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)
        original_updated_at = ws.updated_at

        write_clock = FrozenClock(_PINNED + timedelta(hours=1))
        result = update_basics(
            session,
            ctx,
            actor_user_id=owner_id,
            clock=write_clock,
        )
        # Returned projection echoes current state; ``updated_at``
        # NOT bumped to the write clock.
        assert result.updated_at == original_updated_at
        # No audit row written.
        assert _basics_audit_rows(session, workspace_id=ws.id) == []

        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.updated_at == original_updated_at

    def test_same_value_only_is_noop(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """Every supplied field equals the current value — no DB write."""
        session = session_workspace
        ws = _bootstrap_workspace(session, name="Acme", default_currency="USD")
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)
        original_updated_at = ws.updated_at

        write_clock = FrozenClock(_PINNED + timedelta(hours=1))
        result = update_basics(
            session,
            ctx,
            name="Acme",
            currency="USD",
            actor_user_id=owner_id,
            clock=write_clock,
        )
        assert result.updated_at == original_updated_at
        assert _basics_audit_rows(session, workspace_id=ws.id) == []


# ---------------------------------------------------------------------------
# Tests — validation
# ---------------------------------------------------------------------------


class TestUpdateBasicsValidation:
    """Each invalid field surfaces a per-field 422-equivalent."""

    def test_invalid_timezone_raises(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        with pytest.raises(WorkspaceFieldInvalid) as exc:
            update_basics(
                session,
                ctx,
                timezone="Atlantis/Capital",
                actor_user_id=owner_id,
                clock=clock,
            )
        assert exc.value.field == "timezone"

        # DB row untouched.
        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.default_timezone == "UTC"
        assert _basics_audit_rows(session, workspace_id=ws.id) == []

    def test_invalid_locale_raises(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        # Well-shaped BCP-47 tag we don't ship.
        with pytest.raises(WorkspaceFieldInvalid) as exc:
            update_basics(
                session,
                ctx,
                locale="ja-JP",
                actor_user_id=owner_id,
                clock=clock,
            )
        assert exc.value.field == "locale"

        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.default_locale == "en"

    def test_invalid_locale_shape_raises(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """Malformed BCP-47 tag (underscore, not dash) → 422."""
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        with pytest.raises(WorkspaceFieldInvalid) as exc:
            update_basics(
                session,
                ctx,
                locale="en_US",
                actor_user_id=owner_id,
                clock=clock,
            )
        assert exc.value.field == "locale"

    def test_invalid_currency_raises(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        # ``EURO`` is a common typo of ``EUR`` — must surface as 422.
        with pytest.raises(WorkspaceFieldInvalid) as exc:
            update_basics(
                session,
                ctx,
                currency="EURO",
                actor_user_id=owner_id,
                clock=clock,
            )
        assert exc.value.field == "currency"

        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.default_currency == "USD"

    def test_blank_name_raises(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """Empty / whitespace-only name → 422."""
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        with pytest.raises(WorkspaceFieldInvalid) as exc:
            update_basics(
                session,
                ctx,
                name="   ",
                actor_user_id=owner_id,
                clock=clock,
            )
        assert exc.value.field == "name"

    def test_validation_short_circuits_before_db_write(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """A bad field aborts the call BEFORE other valid fields land."""
        session = session_workspace
        ws = _bootstrap_workspace(session)
        owner_id = new_ulid()
        ctx = _seed_owner(session, ws=ws, owner_user_id=owner_id, clock=clock)

        with pytest.raises(WorkspaceFieldInvalid):
            update_basics(
                session,
                ctx,
                # Valid + valid + INVALID — every field must validate
                # before any DB write.
                name="Acme New",
                currency="EUR",
                timezone="Atlantis/Capital",
                actor_user_id=owner_id,
                clock=clock,
            )

        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        # None of the supplied fields landed.
        assert refreshed.name == "Acme"
        assert refreshed.default_currency == "USD"
        assert refreshed.default_timezone == "UTC"
        assert _basics_audit_rows(session, workspace_id=ws.id) == []


# ---------------------------------------------------------------------------
# Tests — authorisation
# ---------------------------------------------------------------------------


class TestUpdateBasicsAuthorisation:
    """Non-owner callers must collapse to 403 without touching the DB."""

    def test_non_owner_caller_raises_owners_only(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        session = session_workspace
        ws = _bootstrap_workspace(session)
        # Seed an owners group with someone OTHER than the actor.
        real_owner_id = new_ulid()
        _seed_owner(session, ws=ws, owner_user_id=real_owner_id, clock=clock)

        # Caller is not in the owners group; ctx claims manager grant
        # but the service ignores ``actor_was_owner_member`` and
        # re-resolves at write time.
        intruder_id = new_ulid()
        intruder_ctx = _ctx(ws.id, actor_id=intruder_id, slug=ws.slug)

        with pytest.raises(OwnersOnlyError):
            update_basics(
                session,
                intruder_ctx,
                name="Pwned Workspace",
                actor_user_id=intruder_id,
                clock=clock,
            )

        refreshed = session.get(Workspace, ws.id)
        assert refreshed is not None
        assert refreshed.name == "Acme"
        assert _basics_audit_rows(session, workspace_id=ws.id) == []

    def test_owner_membership_in_other_workspace_does_not_grant(
        self, session_workspace: Session, clock: FrozenClock
    ) -> None:
        """Cross-workspace owner membership is not transitive."""
        session = session_workspace
        ws_a = _bootstrap_workspace(session, slug="ws-a", name="Alpha")
        ws_b = _bootstrap_workspace(session, slug="ws-b", name="Bravo")

        # Actor owns ws_a but not ws_b.
        actor_id = new_ulid()
        _seed_owner(session, ws=ws_a, owner_user_id=actor_id, clock=clock)
        # Seed ws_b with a different owner.
        _seed_owner(session, ws=ws_b, owner_user_id=new_ulid(), clock=clock)

        # Try to mutate ws_b under the actor's identity.
        ctx_for_b = _ctx(ws_b.id, actor_id=actor_id, slug=ws_b.slug)
        with pytest.raises(OwnersOnlyError):
            update_basics(
                session,
                ctx_for_b,
                name="Stolen",
                actor_user_id=actor_id,
                clock=clock,
            )

        refreshed_b = session.get(Workspace, ws_b.id)
        assert refreshed_b is not None
        assert refreshed_b.name == "Bravo"
