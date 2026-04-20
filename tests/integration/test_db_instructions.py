"""Integration tests for :mod:`app.adapters.db.instructions` against a real DB.

Covers the post-migration schema shape (tables, unique composites,
FKs, CHECK constraints, indexes), the referential-integrity contract
(``workspace_id`` CASCADE on both tables; ``instruction_id`` CASCADE
on ``instruction_version``; ``current_version_id`` is a soft-ref with
no FK), happy-path round-trip of every model, the version-bump
scenario (insert Instruction + v1, update ``current_version_id``,
insert v2, update again), the "instructions for this scope" lookup,
cross-workspace isolation (slug may repeat across workspaces),
CASCADE on Instruction delete (sweeps its versions), CHECK + UNIQUE
violations, and tenant-filter behaviour (both tables scoped; SELECT
without a :class:`WorkspaceContext` raises
:class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_instructions.py`` covers
pure-Python model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"instruction",
§"instruction_version" and ``docs/specs/07-instructions-kb.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.instructions.models import Instruction, InstructionVersion
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = _PINNED + timedelta(hours=1)


_INSTRUCTIONS_TABLES: tuple[str, ...] = ("instruction", "instruction_version")


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_instructions_registered() -> None:
    """Re-register the two instructions tables as workspace-scoped.

    ``app.adapters.db.instructions.__init__`` registers them at import
    time, but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite.
    """
    for table in _INSTRUCTIONS_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _bootstrap(
    session: Session, *, email: str, display: str, slug: str, name: str
) -> tuple[Workspace, User]:
    """Seed a user + workspace pair for a test."""
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(session, email=email, display_name=display, clock=clock)
    workspace = bootstrap_workspace(
        session, slug=slug, name=name, owner_user_id=user.id, clock=clock
    )
    return workspace, user


class TestMigrationShape:
    """The migration lands both tables with correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _INSTRUCTIONS_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_instruction_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("instruction")}
        expected = {
            "id",
            "workspace_id",
            "slug",
            "title",
            "scope_kind",
            "scope_id",
            "current_version_id",
            "created_by",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("scope_id", "current_version_id", "created_by"):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {"scope_id", "current_version_id", "created_by"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_instruction_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("instruction")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # ``scope_id``, ``current_version_id``, ``created_by`` are
        # soft-refs; no FK. ``current_version_id`` sidesteps the
        # circular dependency with ``instruction_version`` — the
        # domain layer writes it atomically on version bump.
        assert ("scope_id",) not in fks
        assert ("current_version_id",) not in fks
        assert ("created_by",) not in fks

    def test_instruction_unique_workspace_slug(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u for u in inspect(engine).get_unique_constraints("instruction")
        }
        assert "uq_instruction_workspace_slug" in uniques
        assert uniques["uq_instruction_workspace_slug"]["column_names"] == [
            "workspace_id",
            "slug",
        ]

    def test_instruction_scope_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("instruction")}
        assert "ix_instruction_workspace_scope" in indexes
        assert indexes["ix_instruction_workspace_scope"]["column_names"] == [
            "workspace_id",
            "scope_kind",
            "scope_id",
        ]

    def test_instruction_version_columns(self, engine: Engine) -> None:
        cols = {
            c["name"]: c for c in inspect(engine).get_columns("instruction_version")
        }
        expected = {
            "id",
            "workspace_id",
            "instruction_id",
            "version_num",
            "body_md",
            "author_id",
            "created_at",
        }
        assert set(cols) == expected
        assert cols["author_id"]["nullable"] is True
        for notnull in expected - {"author_id"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_instruction_version_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("instruction_version")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("instruction_id",)]["referred_table"] == "instruction"
        # CASCADE — deleting an instruction drops every version row.
        assert fks[("instruction_id",)]["options"].get("ondelete") == "CASCADE"
        # ``author_id`` is a soft-ref; no FK.
        assert ("author_id",) not in fks

    def test_instruction_version_unique_instruction_version_num(
        self, engine: Engine
    ) -> None:
        uniques = {
            u["name"]: u
            for u in inspect(engine).get_unique_constraints("instruction_version")
        }
        assert "uq_instruction_version_instruction_version_num" in uniques
        assert uniques["uq_instruction_version_instruction_version_num"][
            "column_names"
        ] == ["instruction_id", "version_num"]


class TestInstructionCrud:
    """Insert + select + update + delete round-trip on :class:`Instruction`."""

    def test_round_trip_and_scope_lookup(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="inst-crud@example.com",
            display="InstCrud",
            slug="inst-crud-ws",
            name="InstCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            property_inst = Instruction(
                id="01HWA00000000000000000INSA",
                workspace_id=workspace.id,
                slug="villa-cap-ferrat-pet",
                title="Pet rules: Villa Cap Ferrat",
                scope_kind="property",
                scope_id="01HWA00000000000000000PRPA",
                created_by=user.id,
                created_at=_PINNED,
            )
            area_inst = Instruction(
                id="01HWA00000000000000000INSB",
                workspace_id=workspace.id,
                slug="pool-safety",
                title="Pool safety checklist",
                scope_kind="area",
                scope_id="01HWA00000000000000000ARAA",
                created_at=_PINNED,
            )
            workspace_inst = Instruction(
                id="01HWA00000000000000000INSC",
                workspace_id=workspace.id,
                slug="brand-voice",
                title="Brand voice guide",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add_all([property_inst, area_inst, workspace_inst])
            db_session.flush()

            # "Instructions that apply to this area" — the
            # (workspace_id, scope_kind, scope_id) index's target.
            rows = db_session.scalars(
                select(Instruction)
                .where(Instruction.workspace_id == workspace.id)
                .where(Instruction.scope_kind == "area")
                .where(Instruction.scope_id == "01HWA00000000000000000ARAA")
            ).all()
            assert [r.id for r in rows] == ["01HWA00000000000000000INSB"]

            # Workspace-scope lookup: NULL scope_id filters correctly.
            workspace_rows = db_session.scalars(
                select(Instruction)
                .where(Instruction.workspace_id == workspace.id)
                .where(Instruction.scope_kind == "workspace")
            ).all()
            assert [r.id for r in workspace_rows] == ["01HWA00000000000000000INSC"]
            # And its scope_id is NULL.
            assert workspace_rows[0].scope_id is None

            # Update: retitle the area instruction.
            loaded = db_session.get(Instruction, area_inst.id)
            assert loaded is not None
            loaded.title = "Pool safety + depth rules"
            db_session.flush()
            db_session.expire_all()
            reloaded = db_session.get(Instruction, area_inst.id)
            assert reloaded is not None
            assert reloaded.title == "Pool safety + depth rules"

            # Delete the workspace-scoped instruction directly.
            db_session.delete(workspace_inst)
            db_session.flush()
            assert db_session.get(Instruction, workspace_inst.id) is None
        finally:
            reset_current(token)


class TestVersionBumpScenario:
    """The canonical version-bump flow — key acceptance for cd-bce.

    Steps:

    1. Insert :class:`Instruction` (``current_version_id = NULL``).
    2. Insert v1 :class:`InstructionVersion`.
    3. UPDATE ``Instruction.current_version_id`` to v1's id.
    4. Insert v2 :class:`InstructionVersion`.
    5. UPDATE ``Instruction.current_version_id`` to v2's id.

    The pair is mutually dependent (a version FK-points at its
    instruction; the instruction soft-ref points at the current
    version), and this ordering is the one the domain layer will
    use — see ``docs/specs/07-instructions-kb.md`` §"Versions" and
    the module docstring on why ``current_version_id`` is a soft-ref.
    """

    def test_version_bump_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="vbump@example.com",
            display="VBump",
            slug="vbump-ws",
            name="VBumpWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INBP",
                workspace_id=workspace.id,
                slug="daily-opening",
                title="Daily opening checklist",
                scope_kind="property",
                scope_id="01HWA00000000000000000PRPA",
                created_by=user.id,
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()
            assert inst.current_version_id is None

            v1 = InstructionVersion(
                id="01HWA00000000000000000INV1",
                workspace_id=workspace.id,
                instruction_id=inst.id,
                version_num=1,
                body_md="# Daily opening\n\n- Unlock gates.",
                author_id=user.id,
                created_at=_PINNED,
            )
            db_session.add(v1)
            db_session.flush()

            # Bump: the app writes current_version_id atomically.
            inst.current_version_id = v1.id
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(Instruction, inst.id)
            assert reloaded is not None
            assert reloaded.current_version_id == v1.id

            # v2 — the edit path.
            v2 = InstructionVersion(
                id="01HWA00000000000000000INV2",
                workspace_id=workspace.id,
                instruction_id=inst.id,
                version_num=2,
                body_md="# Daily opening v2\n\n- Unlock gates\n- Check CCTV.",
                author_id=user.id,
                created_at=_LATER,
            )
            db_session.add(v2)
            db_session.flush()

            reloaded.current_version_id = v2.id
            db_session.flush()
            db_session.expire_all()

            re_reloaded = db_session.get(Instruction, inst.id)
            assert re_reloaded is not None
            assert re_reloaded.current_version_id == v2.id

            # The v1 row still exists — versions are immutable history.
            v1_survivor = db_session.get(InstructionVersion, v1.id)
            assert v1_survivor is not None
            assert v1_survivor.version_num == 1
            assert v1_survivor.body_md.startswith("# Daily opening\n")

            # Fetch all versions of the instruction, newest first.
            versions = db_session.scalars(
                select(InstructionVersion)
                .where(InstructionVersion.instruction_id == inst.id)
                .order_by(InstructionVersion.version_num.desc())
            ).all()
            assert [v.version_num for v in versions] == [2, 1]
        finally:
            reset_current(token)


class TestInstructionVersionCrud:
    """Insert + select + update + delete round-trip on :class:`InstructionVersion`."""

    def test_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="inv-crud@example.com",
            display="InvCrud",
            slug="inv-crud-ws",
            name="InvCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INVC",
                workspace_id=workspace.id,
                slug="supplier-contacts",
                title="Preferred supplier contacts",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()

            version = InstructionVersion(
                id="01HWA00000000000000000IVC1",
                workspace_id=workspace.id,
                instruction_id=inst.id,
                version_num=1,
                body_md="Plumber: +33 6 00 00 00 01",
                author_id=user.id,
                created_at=_PINNED,
            )
            db_session.add(version)
            db_session.flush()

            loaded = db_session.get(InstructionVersion, version.id)
            assert loaded is not None
            assert loaded.version_num == 1
            assert loaded.body_md == "Plumber: +33 6 00 00 00 01"
            assert loaded.author_id == user.id

            db_session.delete(loaded)
            db_session.flush()
            assert db_session.get(InstructionVersion, version.id) is None
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums / bounds."""

    def test_bogus_scope_kind_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-scope@example.com",
            display="BogusScope",
            slug="bogus-scope-ws",
            name="BogusScopeWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INBX",
                    workspace_id=workspace.id,
                    slug="bogus",
                    title="Bogus",
                    scope_kind="planet",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_version_num_zero_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="vzero@example.com",
            display="Vzero",
            slug="vzero-ws",
            name="VzeroWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INZR",
                workspace_id=workspace.id,
                slug="zero-test",
                title="Zero test",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()

            db_session.add(
                InstructionVersion(
                    id="01HWA00000000000000000IVZR",
                    workspace_id=workspace.id,
                    instruction_id=inst.id,
                    version_num=0,
                    body_md="no body",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_version_num_negative_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="vneg@example.com",
            display="Vneg",
            slug="vneg-ws",
            name="VnegWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INNG",
                workspace_id=workspace.id,
                slug="neg-test",
                title="Neg test",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()

            db_session.add(
                InstructionVersion(
                    id="01HWA00000000000000000IVNG",
                    workspace_id=workspace.id,
                    instruction_id=inst.id,
                    version_num=-3,
                    body_md="no body",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestUniqueConstraints:
    """UNIQUE composites enforce the v1 invariants."""

    def test_duplicate_workspace_slug_rejected(self, db_session: Session) -> None:
        """Key acceptance: a workspace cannot mint two ``slug``-equal instructions."""
        workspace, user = _bootstrap(
            db_session,
            email="slug-dup@example.com",
            display="SlugDup",
            slug="slug-dup-ws",
            name="SlugDupWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INSD",
                    workspace_id=workspace.id,
                    slug="pool-safety",
                    title="Pool safety",
                    scope_kind="workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INSE",
                    workspace_id=workspace.id,
                    slug="pool-safety",  # same slug same workspace
                    title="Pool safety v2",
                    scope_kind="property",
                    scope_id="01HWA00000000000000000PRPA",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_duplicate_instruction_version_num_rejected(
        self, db_session: Session
    ) -> None:
        """Same instruction cannot mint two v3 rows."""
        workspace, user = _bootstrap(
            db_session,
            email="vdup@example.com",
            display="Vdup",
            slug="vdup-ws",
            name="VdupWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INVD",
                workspace_id=workspace.id,
                slug="vdup",
                title="VDup",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()

            db_session.add(
                InstructionVersion(
                    id="01HWA00000000000000000IVD1",
                    workspace_id=workspace.id,
                    instruction_id=inst.id,
                    version_num=1,
                    body_md="first",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                InstructionVersion(
                    id="01HWA00000000000000000IVD2",
                    workspace_id=workspace.id,
                    instruction_id=inst.id,
                    version_num=1,  # duplicate version number
                    body_md="second",
                    created_at=_LATER,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_same_slug_different_workspaces_allowed(self, db_session: Session) -> None:
        """Two workspaces may each mint an instruction with the same slug.

        Uniqueness is on the ``(workspace_id, slug)`` pair, not on
        ``slug`` alone — the typical multi-tenant isolation story.
        Both inserts must succeed.
        """
        ws_a, user_a = _bootstrap(
            db_session,
            email="slug-iso-a@example.com",
            display="SlugIsoA",
            slug="slug-iso-a-ws",
            name="SlugIsoAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="slug-iso-b@example.com",
            display="SlugIsoB",
            slug="slug-iso-b-ws",
            name="SlugIsoBWS",
        )

        token = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INAX",
                    workspace_id=ws_a.id,
                    slug="brand-voice",
                    title="Brand voice (A)",
                    scope_kind="workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INBX",
                    workspace_id=ws_b.id,
                    slug="brand-voice",
                    title="Brand voice (B)",
                    scope_kind="workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            # Each workspace's row exists and is independent.
            rows = db_session.scalars(
                select(Instruction).where(Instruction.slug == "brand-voice")
            ).all()
            # Under the B ctx the ORM filter would mask A's row; reading
            # back from the raw-SELECT bypass of the ``db_session``
            # fixture (no filter installed) gives both.
            assert {r.workspace_id for r in rows} == {ws_a.id, ws_b.id}
        finally:
            reset_current(token)


class TestCrossWorkspaceIsolation:
    """A workspace's instructions do not leak to a sibling workspace."""

    def test_sibling_workspace_sees_own_rows_only(self, db_session: Session) -> None:
        ws_a, user_a = _bootstrap(
            db_session,
            email="xws-a@example.com",
            display="XwsA",
            slug="xws-a-ws",
            name="XwsAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="xws-b@example.com",
            display="XwsB",
            slug="xws-b-ws",
            name="XwsBWS",
        )

        token = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INXA",
                    workspace_id=ws_a.id,
                    slug="only-a",
                    title="Only A",
                    scope_kind="workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                Instruction(
                    id="01HWA00000000000000000INXB",
                    workspace_id=ws_b.id,
                    slug="only-b",
                    title="Only B",
                    scope_kind="workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            # Filter on workspace_id directly (the plain db_session
            # fixture does not install the ORM tenant filter — we
            # express isolation via an explicit predicate).
            b_only = db_session.scalars(
                select(Instruction).where(Instruction.workspace_id == ws_b.id)
            ).all()
            assert {r.slug for r in b_only} == {"only-b"}

            a_only = db_session.scalars(
                select(Instruction).where(Instruction.workspace_id == ws_a.id)
            ).all()
            assert {r.slug for r in a_only} == {"only-a"}
        finally:
            reset_current(token)


class TestCascadeOnInstructionDelete:
    """Deleting an ``instruction`` sweeps every version row with it."""

    def test_delete_instruction_cascades_to_versions(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cascade-inst@example.com",
            display="CascadeInst",
            slug="cascade-inst-ws",
            name="CascadeInstWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INCD",
                workspace_id=workspace.id,
                slug="cascade-me",
                title="Cascade me",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()

            v1 = InstructionVersion(
                id="01HWA00000000000000000IVC1",
                workspace_id=workspace.id,
                instruction_id=inst.id,
                version_num=1,
                body_md="v1",
                created_at=_PINNED,
            )
            v2 = InstructionVersion(
                id="01HWA00000000000000000IVC2",
                workspace_id=workspace.id,
                instruction_id=inst.id,
                version_num=2,
                body_md="v2",
                created_at=_LATER,
            )
            db_session.add_all([v1, v2])
            db_session.flush()

            v1_id, v2_id = v1.id, v2.id
            db_session.delete(inst)
            db_session.flush()
            # The cascade swept both version rows at the DB level. The
            # ORM identity map still references the stale instances;
            # drop them before re-querying so ``get`` doesn't
            # refresh-raise, and observe absence via a fresh SELECT.
            db_session.expunge(v1)
            db_session.expunge(v2)
            survivors = db_session.scalars(
                select(InstructionVersion).where(
                    InstructionVersion.id.in_([v1_id, v2_id])
                )
            ).all()
            assert survivors == []
            assert db_session.get(Instruction, inst.id) is None
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps every instructions row belonging to it."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="cascade-ws@example.com",
            display="CascadeWs",
            slug="cascade-ws-ws",
            name="CascadeWsWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INWS",
                workspace_id=workspace.id,
                slug="ws-cascade",
                title="WS cascade",
                scope_kind="workspace",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()
            db_session.add(
                InstructionVersion(
                    id="01HWA00000000000000000IVWS",
                    workspace_id=workspace.id,
                    instruction_id=inst.id,
                    version_num=1,
                    body_md="ws-cascade body",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # justification: workspace delete is a platform-level op; no
        # :class:`WorkspaceContext` applies once the tenant itself is
        # the target.
        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        token = set_current(_ctx_for(workspace, user.id))
        try:
            assert (
                db_session.scalars(
                    select(Instruction).where(Instruction.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(InstructionVersion).where(
                        InstructionVersion.workspace_id == workspace.id
                    )
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestSoftRefCurrentVersion:
    """``current_version_id`` is a soft-ref — the DB accepts anything.

    The column sidesteps the circular dependency with
    ``instruction_version`` (the version FK-points at its
    instruction). A hard FK would force a two-phase write on insert;
    the domain layer writes the pointer atomically and guards against
    dangling refs. The test documents the schema contract — the DB
    does **not** enforce the pointer's validity — so the domain layer
    owns the invariant.
    """

    def test_dangling_current_version_id_accepted(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="dangling@example.com",
            display="Dangling",
            slug="dangling-ws",
            name="DanglingWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            inst = Instruction(
                id="01HWA00000000000000000INDG",
                workspace_id=workspace.id,
                slug="dangling",
                title="Dangling",
                scope_kind="workspace",
                # No matching version row — a soft-ref, no FK, no
                # error. Domain layer guards against this in practice.
                current_version_id="01HWA00000000000000000XXXX",
                created_at=_PINNED,
            )
            db_session.add(inst)
            db_session.flush()
            reloaded = db_session.get(Instruction, inst.id)
            assert reloaded is not None
            assert reloaded.current_version_id == "01HWA00000000000000000XXXX"
        finally:
            reset_current(token)


class TestTenantFilter:
    """Both instructions tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize("model", [Instruction, InstructionVersion])
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[Instruction] | type[InstructionVersion],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
