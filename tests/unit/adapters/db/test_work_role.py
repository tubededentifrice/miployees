"""Unit tests for :class:`WorkRole` + :class:`UserWorkRole` (cd-5kv4).

Covers the SQLAlchemy mapped classes from
:mod:`app.adapters.db.workspace.models`:

* construction defaults (``description_md``, ``default_settings_json``,
  ``icon_name`` all sensible-default to empty values);
* tablename + ``__table_args__`` shape (UNIQUE constraints, indexes,
  registry membership);
* in-memory SQLite round-trip for both tables, including UNIQUE +
  soft-delete behaviour;
* cross-workspace insert isolation so a row owned by workspace B is
  invisible to a SELECT scoped to workspace A.

Integration coverage (FK cascade, schema fingerprint, CHECK
violations against PG, tenant-filter behaviour) is delegated to
``tests/integration/test_db_workspace.py`` (extended in a follow-up
turn) and ``tests/integration/test_schema_parity.py``.

See ``docs/specs/05-employees-and-roles.md`` §"Work role" / §"User
work role".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, Index, UniqueConstraint, create_engine, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base

# Importing the package (not just ``.models``) is critical so the
# tenancy-registry side effect fires; the cross-workspace test then
# observes the table is registered.
from app.adapters.db.workspace import (
    UserWorkRole,
    UserWorkspace,
    WorkRole,
    Workspace,
)
from app.tenancy import registry

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 24)


# ---------------------------------------------------------------------------
# Engine fixture — in-memory SQLite shared across the test
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so cross-package
    FKs (e.g. ``schedule.property_id`` → ``property.id``) resolve on a
    bare ``Base.metadata.create_all``.

    Copied from ``tests/unit/places/test_property_service.py`` —
    without this step the workspace test, when run after a sibling
    test that imports :mod:`app.adapters.db.tasks.models`, sees a
    partial metadata tree (``schedule`` + FK to ``property``) and
    ``create_all`` raises :class:`NoReferencedTableError`.
    """
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            # Only swallow "this context has no models module yet" —
            # any other import-time failure must surface.
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every ORM table created.

    StaticPool keeps the same underlying SQLite DB across checkouts —
    without it every connection opens a fresh in-memory DB and the
    fixture's ``create_all`` would be invisible to the session the
    test opens. :func:`_load_all_models` forces every per-context
    ``models`` module to import so cross-package FKs resolve before
    ``create_all`` runs.
    """
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh :class:`Session` bound to the in-memory engine.

    Sessions skip the tenant filter — the unit slice exercises the
    schema shape, not the filter wiring (which is covered in
    ``tests/integration/test_db_workspace.py`` and
    ``tests/tenant/test_repository_parity.py``).
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


def _seed_workspace(session: Session, workspace_id: str, slug: str) -> Workspace:
    """Insert a minimal workspace so the FK constraints land cleanly."""
    ws = Workspace(
        id=workspace_id,
        slug=slug,
        name=slug.title(),
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestWorkRoleModelShape:
    """The ``WorkRole`` mapped class carries the cd-5kv4 v1 slice."""

    def test_minimal_construction(self) -> None:
        row = WorkRole(
            id="01HWA00000000000000000WRA1",
            workspace_id="01HWA00000000000000000WSPA",
            key="maid",
            name="Maid",
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000WRA1"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.key == "maid"
        assert row.name == "Maid"
        assert row.created_at == _PINNED
        # ``deleted_at`` is nullable; live rows carry ``None``.
        assert row.deleted_at is None

    def test_tablename(self) -> None:
        assert WorkRole.__tablename__ == "work_role"

    def test_workspace_key_unique_present(self) -> None:
        """``__table_args__`` carries the ``(workspace_id, key)`` unique."""
        uniques = [
            uc for uc in WorkRole.__table_args__ if isinstance(uc, UniqueConstraint)
        ]
        names = [uc.name for uc in uniques]
        assert "uq_work_role_workspace_key" in names
        target = next(uc for uc in uniques if uc.name == "uq_work_role_workspace_key")
        assert [c.name for c in target.columns] == ["workspace_id", "key"]

    def test_live_list_index_present(self) -> None:
        """``ix_work_role_workspace_deleted`` backs the live-list path."""
        indexes = [i for i in WorkRole.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_work_role_workspace_deleted" in names
        target = next(i for i in indexes if i.name == "ix_work_role_workspace_deleted")
        assert target.unique is False
        assert [c.name for c in target.columns] == ["workspace_id", "deleted_at"]


class TestUserWorkRoleModelShape:
    """The ``UserWorkRole`` mapped class carries the cd-5kv4 v1 slice."""

    def test_minimal_construction(self) -> None:
        row = UserWorkRole(
            id="01HWA00000000000000000UWR1",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            work_role_id="01HWA00000000000000000WRA1",
            started_on=_TODAY,
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000UWR1"
        assert row.user_id == "01HWA00000000000000000USRA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.work_role_id == "01HWA00000000000000000WRA1"
        assert row.started_on == _TODAY
        assert row.created_at == _PINNED
        # Optional columns default to ``None`` until set.
        assert row.ended_on is None
        assert row.pay_rule_id is None
        assert row.deleted_at is None

    def test_tablename(self) -> None:
        assert UserWorkRole.__tablename__ == "user_work_role"

    def test_identity_unique_present(self) -> None:
        """``(user_id, workspace_id, work_role_id, started_on)`` UNIQUE.

        This is §05 "User work role"'s identity key — guards against
        a future refactor accidentally widening the rule.
        """
        uniques = [
            uc for uc in UserWorkRole.__table_args__ if isinstance(uc, UniqueConstraint)
        ]
        names = [uc.name for uc in uniques]
        assert "uq_user_work_role_identity" in names
        target = next(uc for uc in uniques if uc.name == "uq_user_work_role_identity")
        assert [c.name for c in target.columns] == [
            "user_id",
            "workspace_id",
            "work_role_id",
            "started_on",
        ]

    def test_hot_path_indexes_present(self) -> None:
        """Both ``(workspace_id, user_id)`` and ``(workspace_id, work_role_id)``."""
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in UserWorkRole.__table_args__
            if isinstance(i, Index)
        }
        assert indexes["ix_user_work_role_workspace_user"] == [
            "workspace_id",
            "user_id",
        ]
        assert indexes["ix_user_work_role_workspace_role"] == [
            "workspace_id",
            "work_role_id",
        ]


class TestRegistryMembership:
    """``work_role`` and ``user_work_role`` are registered as scoped."""

    def test_work_role_registered(self) -> None:
        assert registry.is_scoped("work_role")

    def test_user_work_role_registered(self) -> None:
        assert registry.is_scoped("user_work_role")

    def test_workspace_table_not_registered(self) -> None:
        """Sanity: the tenancy anchor stays agnostic — verifies the
        new registrations did not accidentally widen the set."""
        assert not registry.is_scoped("workspace")


# ---------------------------------------------------------------------------
# Idempotent re-import — landing the module twice must not redefine tables
# ---------------------------------------------------------------------------


class TestModuleReimportIdempotent:
    """A second ``import`` of the package does not redefine the tables.

    Re-importing a SQLAlchemy module that already populated
    :attr:`Base.metadata` raises ``InvalidRequestError: Table 'work_role'
    is already defined for this MetaData instance.`` if the module
    body re-declares the class on top of an existing one. Our package
    relies on Python's normal import-cache so the second import is a
    no-op; this test guards against a refactor that would inadvertently
    re-define the tables (e.g. by collapsing to a ``del`` + reload).
    """

    def test_reimport_does_not_raise(self) -> None:
        import importlib

        import app.adapters.db.workspace as ws_pkg

        # Force-reimport via importlib so the cached module is
        # re-executed; a redefinition error on the mapped class would
        # raise inside ``importlib.reload``.
        importlib.reload(ws_pkg)


# ---------------------------------------------------------------------------
# Round-trip + UNIQUE + soft-delete on the real engine
# ---------------------------------------------------------------------------


class TestWorkRoleRoundTrip:
    """Insert + reload exercises the round-trip path on SQLite."""

    def test_insert_then_read_back(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "alpha")
        row = WorkRole(
            id="01HWA00000000000000000WRRT",
            workspace_id="01HWA00000000000000000WSPA",
            key="cook",
            name="Cook",
            description_md="# Cook\nPrepares meals.",
            default_settings_json={"evidence.policy": "optional"},
            icon_name="ChefHat",
            created_at=_PINNED,
        )
        session.add(row)
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(WorkRole).where(WorkRole.id == "01HWA00000000000000000WRRT")
        ).one()
        assert loaded.key == "cook"
        assert loaded.name == "Cook"
        assert loaded.description_md == "# Cook\nPrepares meals."
        assert loaded.default_settings_json == {"evidence.policy": "optional"}
        assert loaded.icon_name == "ChefHat"
        assert loaded.deleted_at is None

    def test_text_columns_default_to_empty_on_omitted_writes(
        self, session: Session
    ) -> None:
        """The server defaults populate ``description_md`` / ``icon_name``
        / ``default_settings_json`` when the writer omits them.

        Without this guarantee a partial seeder (or a CSV import that
        skips an optional column) would land NULL into a NOT NULL
        column and the INSERT would fail. The defaults make the
        contract "if you omit it, the empty / null-shape value lands"
        explicit.
        """
        _seed_workspace(session, "01HWA00000000000000000WSDF", "default-shape")
        # Construct without ORM-side defaults — drop straight to the
        # core insert so SQLAlchemy doesn't fill in ``""`` / ``{}``
        # before the round-trip. The server defaults must populate the
        # omitted columns or the INSERT would fail with NOT NULL.
        session.execute(
            insert(WorkRole).values(
                id="01HWA00000000000000000WRDF",
                workspace_id="01HWA00000000000000000WSDF",
                key="driver",
                name="Driver",
                created_at=_PINNED,
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(WorkRole).where(WorkRole.id == "01HWA00000000000000000WRDF")
        ).one()
        assert loaded.description_md == ""
        assert loaded.icon_name == ""
        assert loaded.default_settings_json == {}

    def test_soft_delete_roundtrips(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPB", "bravo")
        row = WorkRole(
            id="01HWA00000000000000000WRSD",
            workspace_id="01HWA00000000000000000WSPB",
            key="gardener",
            name="Gardener",
            created_at=_PINNED,
        )
        session.add(row)
        session.flush()

        row.deleted_at = _LATER
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(WorkRole).where(WorkRole.id == "01HWA00000000000000000WRSD")
        ).one()
        # SQLite ``DateTime(timezone=True)`` strips tzinfo on read with
        # the default driver; PG keeps it. Compare wall-clock components.
        assert loaded.deleted_at is not None
        assert loaded.deleted_at.replace(tzinfo=None) == _LATER.replace(tzinfo=None)


class TestWorkRoleUnique:
    """``(workspace_id, key)`` enforces one slug per workspace."""

    def test_duplicate_key_in_same_workspace_raises(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSDU", "dup")
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRD1",
                workspace_id="01HWA00000000000000000WSDU",
                key="cook",
                name="Cook",
                created_at=_PINNED,
            )
        )
        session.flush()

        session.add(
            WorkRole(
                id="01HWA00000000000000000WRD2",
                workspace_id="01HWA00000000000000000WSDU",
                key="cook",
                name="Cook 2",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_same_key_in_different_workspaces_allowed(self, session: Session) -> None:
        """Two workspaces independently own a ``maid`` slug."""
        _seed_workspace(session, "01HWA00000000000000000WSXA", "x-alpha")
        _seed_workspace(session, "01HWA00000000000000000WSXB", "x-bravo")
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRXA",
                workspace_id="01HWA00000000000000000WSXA",
                key="maid",
                name="Maid",
                created_at=_PINNED,
            )
        )
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRXB",
                workspace_id="01HWA00000000000000000WSXB",
                key="maid",
                name="Maid",
                created_at=_PINNED,
            )
        )
        session.flush()  # No IntegrityError — two workspaces, two slugs.


# ---------------------------------------------------------------------------
# UserWorkRole: round-trip + UNIQUE
# ---------------------------------------------------------------------------


class TestUserWorkRoleRoundTrip:
    """Insert + reload + UNIQUE behaviour on the real engine."""

    def test_insert_then_read_back(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSUR", "user-role")
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRUR",
                workspace_id="01HWA00000000000000000WSUR",
                key="cook",
                name="Cook",
                created_at=_PINNED,
            )
        )
        session.flush()

        link = UserWorkRole(
            id="01HWA00000000000000000UWRR",
            user_id="01HWA00000000000000000USRR",
            workspace_id="01HWA00000000000000000WSUR",
            work_role_id="01HWA00000000000000000WRUR",
            started_on=_TODAY,
            pay_rule_id="01HWA00000000000000000PAYZ",
            created_at=_PINNED,
        )
        session.add(link)
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserWorkRole).where(UserWorkRole.id == "01HWA00000000000000000UWRR")
        ).one()
        assert loaded.user_id == "01HWA00000000000000000USRR"
        assert loaded.work_role_id == "01HWA00000000000000000WRUR"
        assert loaded.started_on == _TODAY
        assert loaded.pay_rule_id == "01HWA00000000000000000PAYZ"
        assert loaded.ended_on is None
        assert loaded.deleted_at is None

    def test_identity_unique_enforced(self, session: Session) -> None:
        """A second row with the same identity tuple is rejected."""
        _seed_workspace(session, "01HWA00000000000000000WSUU", "user-uniq")
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRUU",
                workspace_id="01HWA00000000000000000WSUU",
                key="driver",
                name="Driver",
                created_at=_PINNED,
            )
        )
        session.flush()

        first = UserWorkRole(
            id="01HWA00000000000000000UWU1",
            user_id="01HWA00000000000000000USUU",
            workspace_id="01HWA00000000000000000WSUU",
            work_role_id="01HWA00000000000000000WRUU",
            started_on=_TODAY,
            created_at=_PINNED,
        )
        session.add(first)
        session.flush()

        dup = UserWorkRole(
            id="01HWA00000000000000000UWU2",
            user_id="01HWA00000000000000000USUU",
            workspace_id="01HWA00000000000000000WSUU",
            work_role_id="01HWA00000000000000000WRUU",
            started_on=_TODAY,  # same identity tuple
            created_at=_PINNED,
        )
        session.add(dup)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_same_user_role_on_different_started_on_allowed(
        self, session: Session
    ) -> None:
        """A rehire on a different date mints a fresh row.

        Per §05 — the identity tuple includes ``started_on`` precisely
        so a rehired worker can carry an audit-friendly history of
        engagements rather than a single mutable row.
        """
        _seed_workspace(session, "01HWA00000000000000000WSUH", "user-rehire")
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRUH",
                workspace_id="01HWA00000000000000000WSUH",
                key="nanny",
                name="Nanny",
                created_at=_PINNED,
            )
        )
        session.flush()

        session.add(
            UserWorkRole(
                id="01HWA00000000000000000UWH1",
                user_id="01HWA00000000000000000USUH",
                workspace_id="01HWA00000000000000000WSUH",
                work_role_id="01HWA00000000000000000WRUH",
                started_on=date(2025, 1, 1),
                ended_on=date(2025, 12, 31),
                created_at=_PINNED,
            )
        )
        session.add(
            UserWorkRole(
                id="01HWA00000000000000000UWH2",
                user_id="01HWA00000000000000000USUH",
                workspace_id="01HWA00000000000000000WSUH",
                work_role_id="01HWA00000000000000000WRUH",
                started_on=date(2026, 4, 1),
                created_at=_PINNED,
            )
        )
        session.flush()  # No IntegrityError — different ``started_on``.

    def test_soft_delete_roundtrips(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSUS", "user-soft")
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRUS",
                workspace_id="01HWA00000000000000000WSUS",
                key="handyman",
                name="Handyman",
                created_at=_PINNED,
            )
        )
        session.flush()

        link = UserWorkRole(
            id="01HWA00000000000000000UWUS",
            user_id="01HWA00000000000000000USUS",
            workspace_id="01HWA00000000000000000WSUS",
            work_role_id="01HWA00000000000000000WRUS",
            started_on=_TODAY,
            created_at=_PINNED,
        )
        session.add(link)
        session.flush()

        link.deleted_at = _LATER
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserWorkRole).where(UserWorkRole.id == "01HWA00000000000000000UWUS")
        ).one()
        # SQLite ``DateTime(timezone=True)`` strips tzinfo on read with
        # the default driver; PG keeps it. Match either shape.
        assert loaded.deleted_at is not None
        assert loaded.deleted_at.replace(tzinfo=None) == _LATER.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Cross-workspace isolation — manual SELECT shows row scoped by workspace
# ---------------------------------------------------------------------------


class TestCrossWorkspaceIsolation:
    """A row owned by workspace B is invisible to a SELECT for A.

    This unit test exercises the schema-level workspace_id discriminator
    rather than the ORM tenant filter (which is integration-tested in
    ``tests/tenant/test_repository_parity.py``). The point here is that
    the column is populated correctly and a manual
    ``WHERE workspace_id = A`` returns only A-owned rows.
    """

    def test_b_row_invisible_under_a_filter(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "iso-a")
        _seed_workspace(session, "01HWA00000000000000000WSPB", "iso-b")
        # One ``maid`` per workspace — same slug, different tenancy.
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRA1",
                workspace_id="01HWA00000000000000000WSPA",
                key="maid",
                name="Maid (A)",
                created_at=_PINNED,
            )
        )
        session.add(
            WorkRole(
                id="01HWA00000000000000000WRB1",
                workspace_id="01HWA00000000000000000WSPB",
                key="maid",
                name="Maid (B)",
                created_at=_PINNED,
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(WorkRole).where(
                WorkRole.workspace_id == "01HWA00000000000000000WSPA"
            )
        ).all()
        ids_a = {r.id for r in rows_a}
        assert ids_a == {"01HWA00000000000000000WRA1"}, (
            "A-scoped SELECT returned a B-owned row — workspace_id is "
            "not discriminating correctly"
        )

        rows_b = session.scalars(
            select(WorkRole).where(
                WorkRole.workspace_id == "01HWA00000000000000000WSPB"
            )
        ).all()
        ids_b = {r.id for r in rows_b}
        assert ids_b == {"01HWA00000000000000000WRB1"}


# ---------------------------------------------------------------------------
# Soft import-time guarantee on UserWorkspace too
# ---------------------------------------------------------------------------


def test_userworkspace_still_importable() -> None:
    """The package still re-exports :class:`UserWorkspace` post cd-5kv4.

    cd-5kv4 expanded :data:`__all__` from two to four classes; this
    sanity check guards against a future refactor accidentally
    dropping the older exports.
    """
    assert UserWorkspace.__tablename__ == "user_workspace"
