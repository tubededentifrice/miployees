"""Unit tests for :mod:`app.adapters.db.instructions.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, unique composites, index shape, tenancy-registry
membership). Integration coverage (migrations, FK cascade, CHECK /
UNIQUE violations against a real DB, version bump, lookup by scope,
cross-workspace isolation, tenant filter behaviour) lives in
``tests/integration/test_db_instructions.py``.

See ``docs/specs/02-domain-model.md`` §"instruction",
§"instruction_version" and ``docs/specs/07-instructions-kb.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.instructions import Instruction, InstructionVersion
from app.adapters.db.instructions import models as instructions_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestInstructionModel:
    """The ``Instruction`` mapped class constructs from the v1 slice."""

    def test_minimal_workspace_scope_construction(self) -> None:
        inst = Instruction(
            id="01HWA00000000000000000INSA",
            workspace_id="01HWA00000000000000000WSPA",
            slug="pool-closing",
            title="Pool closing",
            scope_kind="workspace",
            created_at=_PINNED,
        )
        assert inst.id == "01HWA00000000000000000INSA"
        assert inst.workspace_id == "01HWA00000000000000000WSPA"
        assert inst.slug == "pool-closing"
        assert inst.title == "Pool closing"
        assert inst.scope_kind == "workspace"
        # Workspace-scoped instructions carry a NULL scope_id.
        assert inst.scope_id is None
        # Version-bump domain layer writes this atomically — starts NULL.
        assert inst.current_version_id is None
        assert inst.created_by is None
        assert inst.created_at == _PINNED

    def test_property_scoped_construction(self) -> None:
        inst = Instruction(
            id="01HWA00000000000000000INSB",
            workspace_id="01HWA00000000000000000WSPA",
            slug="villa-cap-ferrat-pet",
            title="Pet rules for Villa Cap Ferrat",
            scope_kind="property",
            scope_id="01HWA00000000000000000PRPA",
            current_version_id="01HWA00000000000000000INVA",
            created_by="01HWA00000000000000000USRA",
            created_at=_PINNED,
        )
        assert inst.scope_kind == "property"
        assert inst.scope_id == "01HWA00000000000000000PRPA"
        assert inst.current_version_id == "01HWA00000000000000000INVA"
        assert inst.created_by == "01HWA00000000000000000USRA"

    def test_every_scope_kind_constructs(self) -> None:
        """Each of the seven v1 scope kinds builds a valid row."""
        kinds = (
            "template",
            "property",
            "area",
            "asset",
            "stay",
            "role",
            "workspace",
        )
        for index, kind in enumerate(kinds):
            scope_id = (
                None if kind == "workspace" else f"01HWA0000000000000000SC0{index}"
            )
            inst = Instruction(
                id=f"01HWA0000000000000000INS{index}",
                workspace_id="01HWA00000000000000000WSPA",
                slug=f"slug-{kind}",
                title=f"Title for {kind}",
                scope_kind=kind,
                scope_id=scope_id,
                created_at=_PINNED,
            )
            assert inst.scope_kind == kind

    def test_tablename(self) -> None:
        assert Instruction.__tablename__ == "instruction"

    def test_scope_kind_check_present(self) -> None:
        # Constraint name ``scope_kind`` on the model; the shared
        # naming convention rewrites it to ``ck_instruction_scope_kind``
        # on the bound column, so match by suffix (mirrors the sibling
        # ``tasks`` / ``time`` / ``payroll`` test pattern).
        checks = [
            c
            for c in Instruction.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("scope_kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in (
            "template",
            "property",
            "area",
            "asset",
            "stay",
            "role",
            "workspace",
        ):
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_unique_workspace_slug_present(self) -> None:
        """Key acceptance: UNIQUE ``(workspace_id, slug)``."""
        uniques = [
            u for u in Instruction.__table_args__ if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == ["workspace_id", "slug"]
        assert uniques[0].name == "uq_instruction_workspace_slug"

    def test_workspace_scope_index_present(self) -> None:
        """Index: ``(workspace_id, scope_kind, scope_id)`` for scope lookup."""
        indexes = [i for i in Instruction.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_instruction_workspace_scope" in names
        target = next(i for i in indexes if i.name == "ix_instruction_workspace_scope")
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "scope_kind",
            "scope_id",
        ]


class TestInstructionVersionModel:
    """The ``InstructionVersion`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        version = InstructionVersion(
            id="01HWA00000000000000000INVA",
            workspace_id="01HWA00000000000000000WSPA",
            instruction_id="01HWA00000000000000000INSA",
            version_num=1,
            body_md="# Pool closing\n\nLock the gate at dusk.",
            created_at=_PINNED,
        )
        assert version.id == "01HWA00000000000000000INVA"
        assert version.workspace_id == "01HWA00000000000000000WSPA"
        assert version.instruction_id == "01HWA00000000000000000INSA"
        assert version.version_num == 1
        assert version.body_md == "# Pool closing\n\nLock the gate at dusk."
        # author_id nullable — system-actor authors have no user id.
        assert version.author_id is None
        assert version.created_at == _PINNED

    def test_authored_construction(self) -> None:
        version = InstructionVersion(
            id="01HWA00000000000000000INVB",
            workspace_id="01HWA00000000000000000WSPA",
            instruction_id="01HWA00000000000000000INSA",
            version_num=2,
            body_md="# Pool closing v2\n\nNow with a photo step.",
            author_id="01HWA00000000000000000USRA",
            created_at=_LATER,
        )
        assert version.version_num == 2
        assert version.author_id == "01HWA00000000000000000USRA"
        assert version.created_at == _LATER

    def test_empty_body_allowed(self) -> None:
        """An empty body is legal — a draft with the body still TBD."""
        version = InstructionVersion(
            id="01HWA00000000000000000INVC",
            workspace_id="01HWA00000000000000000WSPA",
            instruction_id="01HWA00000000000000000INSA",
            version_num=1,
            body_md="",
            created_at=_PINNED,
        )
        assert version.body_md == ""

    def test_tablename(self) -> None:
        assert InstructionVersion.__tablename__ == "instruction_version"

    def test_version_num_positive_check_present(self) -> None:
        checks = [
            c
            for c in InstructionVersion.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("version_num_positive")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "version_num" in sql
        assert ">= 1" in sql

    def test_unique_instruction_version_num_present(self) -> None:
        """A single instruction cannot mint two v3 rows."""
        uniques = [
            u
            for u in InstructionVersion.__table_args__
            if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == [
            "instruction_id",
            "version_num",
        ]
        assert uniques[0].name == "uq_instruction_version_instruction_version_num"


class TestPackageReExports:
    """``app.adapters.db.instructions`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert Instruction is instructions_models.Instruction
        assert InstructionVersion is instructions_models.InstructionVersion


class TestRegistryIntent:
    """Every instructions table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.instructions``: a sibling
    ``test_tenancy_orm_filter`` autouse fixture calls
    :func:`registry._reset_for_tests` which wipes the process-wide set,
    so asserting presence after that reset would be flaky. The tests
    below encode the invariant — "every instructions table is scoped"
    — without over-coupling to import ordering. In particular
    ``instruction_version`` carries a denormalised ``workspace_id``
    (see the module docstring) so it is *directly* workspace-scoped
    rather than reached via a join through ``instruction``.
    """

    def test_every_instructions_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("instruction", "instruction_version"):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in ("instruction", "instruction_version"):
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("instruction", "instruction_version"):
            registry.register(table)
        for table in ("instruction", "instruction_version"):
            assert registry.is_scoped(table) is True
