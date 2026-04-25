"""Integration tests for :mod:`app.audit` against a real DB.

Covers the transaction-boundary contract (§01 "Key runtime
invariants" #3), the tenant-filter behaviour on ``audit_log``
reads (§01 "Tenant filter enforcement"), and the post-migration
schema shape (indexes, column nullability, ``scope_kind`` CHECKs).

The sibling ``tests/unit/test_audit_writer.py`` and
``tests/unit/audit/`` cover field-copy and diff/clock/ULID surface
without the migration harness.

See ``docs/specs/02-domain-model.md`` §"audit_log" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TypedDict

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.audit import write_audit, write_deployment_audit
from app.tenancy import ActorGrantRole, ActorKind, registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter

pytestmark = pytest.mark.integration


_CTX = WorkspaceContext(
    workspace_id="01HWA00000000000000000WSPA",
    workspace_slug="workspace-a",
    actor_id="01HWA00000000000000000USRA",
    actor_kind="user",
    actor_grant_role="manager",
    actor_was_owner_member=True,
    audit_correlation_id="01HWA00000000000000000CRLA",
)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Scoped to the module so SQLAlchemy's per-sessionmaker event
    dispatch doesn't churn — building a fresh ``sessionmaker`` each
    test and re-installing the hook hits a known SQLAlchemy quirk
    where ``event.contains`` reports the listener but the new
    sessions' ``dispatch.do_orm_execute`` list comes back empty
    (observed after two prior sessions on the same engine chain).

    The top-level ``db_session`` fixture is filterless (it binds
    directly to a raw connection for SAVEPOINT isolation, bypassing
    the default sessionmaker). Tests that need to observe the
    ``TenantFilterMissing`` behaviour build their own factory here
    and install the hook explicitly.
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
def _ensure_audit_log_registered() -> None:
    """Re-register ``audit_log`` as workspace-scoped before each test.

    ``app.adapters.db.audit`` registers the table at import time, but a
    sibling unit test (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the import-
    time registration would lose the race and our tenant-filter
    enforcement assertions would pass in isolation and silently drop
    the filter under the full suite.
    """
    registry.register("audit_log")


class TestMigrationShape:
    """The migration lands the ``audit_log`` table + both composite indexes."""

    def test_audit_log_table_exists(self, engine: Engine) -> None:
        assert "audit_log" in inspect(engine).get_table_names()

    def test_audit_log_columns_match_spec(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("audit_log")}
        expected = {
            "id",
            "workspace_id",
            "actor_id",
            "actor_kind",
            "actor_grant_role",
            "actor_was_owner_member",
            "entity_kind",
            "entity_id",
            "action",
            "diff",
            "correlation_id",
            "scope_kind",
            "created_at",
        }
        assert set(cols) == expected
        # ``workspace_id`` is NULLABLE post-cd-kgcc — the biconditional
        # CHECK below pins ``NULL ⇔ scope_kind = 'deployment'``. Every
        # other column stays NOT NULL — audit rows are complete by
        # construction.
        nullable_cols = {"workspace_id"}
        for name, col in cols.items():
            expected_nullable = name in nullable_cols
            assert col["nullable"] is expected_nullable, (
                f"{name}: expected nullable={expected_nullable}, got {col['nullable']}"
            )
        pk = inspect(engine).get_pk_constraint("audit_log")
        assert pk["constrained_columns"] == ["id"]

    def test_composite_indexes_are_present(self, engine: Engine) -> None:
        """Every composite index the spec calls out must exist."""
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("audit_log")}
        assert "ix_audit_log_workspace_created" in indexes
        assert indexes["ix_audit_log_workspace_created"]["column_names"] == [
            "workspace_id",
            "created_at",
        ]
        assert "ix_audit_log_workspace_entity" in indexes
        assert indexes["ix_audit_log_workspace_entity"]["column_names"] == [
            "workspace_id",
            "entity_kind",
            "entity_id",
        ]
        # cd-kgcc — backs ``GET /admin/api/v1/audit`` (§12 "Admin
        # surface" → "Deployment audit"). The workspace-keyed index
        # above does not cover the deployment partition because every
        # deployment row carries ``workspace_id IS NULL``.
        assert "ix_audit_log_scope_kind_created" in indexes
        assert indexes["ix_audit_log_scope_kind_created"]["column_names"] == [
            "scope_kind",
            "created_at",
        ]

    def test_scope_kind_check_constraints_are_present(self, engine: Engine) -> None:
        """Both CHECK constraints (enum + biconditional pairing) land on the table."""
        checks = {
            ck.get("name") for ck in inspect(engine).get_check_constraints("audit_log")
        }
        # The shared naming convention prefixes with ``ck_<table>_``;
        # constraint names rendered through ``base.py`` come out
        # deterministic so a missing CHECK on either side is the diff
        # we want this test to catch.
        assert "ck_audit_log_scope_kind" in checks
        assert "ck_audit_log_scope_kind_workspace_pairing" in checks


class TestTransactionBoundary:
    """``write_audit`` lands / rolls back with the caller's UoW."""

    def test_commit_persists_the_row(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """A write inside ``with session.begin():`` lands on commit."""
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000T1",
                    action="created",
                    diff={"title": "new"},
                )

            # Reopen a fresh session and confirm the row is visible.
            with filtered_factory() as reader:
                rows = reader.scalars(select(AuditLog)).all()
                # Drop any rows seeded by a sibling test that also pinned
                # _CTX.workspace_id; the row we just wrote is the one
                # whose entity_id is the fixed value we passed in.
                ours = [r for r in rows if r.entity_id == "01HWATASK000000000000000T1"]
                assert len(ours) == 1
                assert ours[0].action == "created"
                assert ours[0].diff == {"title": "new"}
        finally:
            reset_current(token)

    def test_rollback_drops_the_row(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """A raise inside ``with session.begin():`` rolls back the row."""

        class _Boom(Exception):
            pass

        token = set_current(_CTX)
        try:
            writer = filtered_factory()
            try:
                with pytest.raises(_Boom), writer.begin():
                    write_audit(
                        writer,
                        _CTX,
                        entity_kind="task",
                        entity_id="01HWATASK000000000000000T2",
                        action="created",
                    )
                    raise _Boom
            finally:
                writer.close()

            # Fresh session — the rolled-back row is gone.
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id == "01HWATASK000000000000000T2"
                    )
                ).all()
                assert rows == []
        finally:
            reset_current(token)

    def test_diff_none_round_trips_as_empty_dict(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``diff=None`` persists as ``{}`` and reloads as ``{}``.

        The unit suite checks the in-memory attribute before flush;
        this test closes the loop by committing, reopening a fresh
        session, and confirming the JSON column round-trips the
        empty-dict payload (§02 "audit_log" NOT NULL contract).
        """
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000T3",
                    action="deleted",
                    diff=None,
                )

            with filtered_factory() as reader:
                row = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id == "01HWATASK000000000000000T3"
                    )
                ).one()
                assert row.diff == {}
                assert isinstance(row.diff, dict)
        finally:
            reset_current(token)


class TestTenantFilterEnforcement:
    """Reads against ``audit_log`` without a ctx raise :class:`TenantFilterMissing`."""

    def test_select_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """Registered as scoped ⇒ SELECT without a ctx fails closed."""
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(AuditLog))
        assert exc.value.table == "audit_log"

    def test_select_with_ctx_only_returns_current_workspace(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """With a ctx active, the filter scopes reads to ``workspace_id``."""
        other_ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSPB",
            workspace_slug="workspace-b",
            actor_id="01HWA00000000000000000USRB",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="01HWA00000000000000000CRLB",
        )
        # Seed one row per workspace.
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000WA",
                    action="created",
                )
        finally:
            reset_current(token)

        token = set_current(other_ctx)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    other_ctx,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000WB",
                    action="created",
                )
        finally:
            reset_current(token)

        # Reading from workspace A sees only the A-scoped row among the
        # two we just seeded (other tests may have added their own).
        token = set_current(_CTX)
        try:
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id.in_(
                            [
                                "01HWATASK000000000000000WA",
                                "01HWATASK000000000000000WB",
                            ]
                        )
                    )
                ).all()
                assert [r.entity_id for r in rows] == ["01HWATASK000000000000000WA"]
        finally:
            reset_current(token)

        # Reading from workspace B sees only the B-scoped row.
        token = set_current(other_ctx)
        try:
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id.in_(
                            [
                                "01HWATASK000000000000000WA",
                                "01HWATASK000000000000000WB",
                            ]
                        )
                    )
                ).all()
                assert [r.entity_id for r in rows] == ["01HWATASK000000000000000WB"]
        finally:
            reset_current(token)


class _DeploymentActorKW(TypedDict):
    """Reusable kwargs for the deployment-scope writer.

    Spelled as a TypedDict (rather than a plain dict literal) so
    ``**_DEPLOYMENT_KW`` keeps the typed kwargs of
    :func:`write_deployment_audit` strict under ``mypy --strict`` —
    a plain ``dict[str, object]`` widens every value and erases the
    ``Literal`` enums on ``actor_kind`` / ``actor_grant_role``.
    """

    actor_id: str
    actor_kind: ActorKind
    actor_grant_role: ActorGrantRole
    actor_was_owner_member: bool
    correlation_id: str


_DEPLOYMENT_KW: _DeploymentActorKW = {
    "actor_id": "01HWA00000000000000000ADM1",
    "actor_kind": "user",
    "actor_grant_role": "manager",
    "actor_was_owner_member": False,
    "correlation_id": "01HWA00000000000000000CRLD",
}


class TestDeploymentScopeWriter:
    """cd-kgcc: :func:`write_deployment_audit` lands deployment-scope rows.

    Deployment-scope rows carry ``workspace_id IS NULL`` and
    ``scope_kind = 'deployment'`` and feed the
    ``GET /admin/api/v1/audit`` admin surface (§12). Persisting them
    bypasses the ORM tenant filter via :func:`tenant_agnostic` — there
    is no workspace context to pin reads to, and the writer is
    explicit about the scope it represents.

    The integration tests below run against the migration-built
    schema so every CHECK fires on both SQLite and Postgres, mirroring
    the cd-wchi ``role_grant`` pattern.
    """

    def test_deployment_audit_persists_with_null_workspace(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """A deployment-scope row lands with ``workspace_id IS NULL``."""
        with (
            filtered_factory() as writer,
            tenant_agnostic(),
            writer.begin(),
        ):
            row = write_deployment_audit(
                writer,
                **_DEPLOYMENT_KW,
                entity_kind="api_token",
                entity_id="01HWADEP00000000000000T01",
                action="api_token.created",
                diff={"after": {"name": "ops-rotator"}},
            )
            row_id = row.id

        with filtered_factory() as reader, tenant_agnostic():
            loaded = reader.scalars(select(AuditLog).where(AuditLog.id == row_id)).one()
        assert loaded.workspace_id is None
        assert loaded.scope_kind == "deployment"
        assert loaded.entity_id == "01HWADEP00000000000000T01"
        assert loaded.action == "api_token.created"

    def test_deployment_audit_carries_scope_kind_deployment(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``scope_kind`` is set to ``'deployment'`` for the new entry point."""
        with (
            filtered_factory() as writer,
            tenant_agnostic(),
            writer.begin(),
        ):
            row = write_deployment_audit(
                writer,
                **_DEPLOYMENT_KW,
                entity_kind="deployment_setting",
                entity_id="01HWADEP00000000000000S01",
                action="deployment_setting.updated",
            )
            row_id = row.id

        with filtered_factory() as reader, tenant_agnostic():
            loaded = reader.scalars(select(AuditLog).where(AuditLog.id == row_id)).one()
        assert loaded.scope_kind == "deployment"

    def test_deployment_audit_with_workspace_id_rejected_by_check(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """The biconditional CHECK rejects ``scope_kind='deployment' + workspace_id``.

        The writer never produces this shape — it always pins
        ``workspace_id=None`` — but the DB-level CHECK is the
        defence-in-depth line. We bypass the writer here to exercise
        the constraint directly: a hand-built ``AuditLog`` with
        ``scope_kind='deployment'`` carrying a workspace_id MUST
        raise :class:`IntegrityError` on flush.
        """
        with (
            filtered_factory() as writer,
            tenant_agnostic(),
            writer.begin(),
        ):
            writer.add(
                AuditLog(
                    id="01HWADEP00000000000000R01",
                    workspace_id="01HWA00000000000000000WSPA",
                    actor_id="01HWA00000000000000000ADM1",
                    actor_kind="user",
                    actor_grant_role="manager",
                    actor_was_owner_member=False,
                    entity_kind="deployment_setting",
                    entity_id="01HWADEP00000000000000S02",
                    action="deployment_setting.updated",
                    diff={},
                    correlation_id="01HWA00000000000000000CRLD",
                    scope_kind="deployment",
                    created_at=_ctx_now(),
                )
            )
            with pytest.raises(IntegrityError):
                writer.flush()
            writer.rollback()

    def test_workspace_audit_without_workspace_id_rejected_by_check(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """The biconditional CHECK rejects ``scope_kind='workspace' + NULL``.

        Symmetric guard for the other half of the pairing — a
        workspace-scoped row that omits ``workspace_id`` is the bug we
        want the CHECK to catch on both backends.
        """
        with (
            filtered_factory() as writer,
            tenant_agnostic(),
            writer.begin(),
        ):
            writer.add(
                AuditLog(
                    id="01HWADEP00000000000000R02",
                    workspace_id=None,
                    actor_id="01HWA00000000000000000USRA",
                    actor_kind="user",
                    actor_grant_role="manager",
                    actor_was_owner_member=True,
                    entity_kind="task",
                    entity_id="01HWATASK00000000000000W1",
                    action="task.created",
                    diff={},
                    correlation_id="01HWA00000000000000000CRLA",
                    scope_kind="workspace",
                    created_at=_ctx_now(),
                )
            )
            with pytest.raises(IntegrityError):
                writer.flush()
            writer.rollback()

    def test_workspace_audit_unchanged_by_kgcc(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """Existing workspace-scope writes still land with NOT-NULL workspace_id.

        cd-kgcc widens the table; existing call sites must keep
        producing the same shape they always did.
        """
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                row = write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK00000000000000WK",
                    action="task.created",
                )
                row_id = row.id

            with filtered_factory() as reader:
                loaded = reader.scalars(
                    select(AuditLog).where(AuditLog.id == row_id)
                ).one()
            assert loaded.workspace_id == _CTX.workspace_id
            assert loaded.scope_kind == "workspace"
        finally:
            reset_current(token)

    def test_workspace_read_does_not_surface_deployment_rows(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """A workspace-scoped read excludes ``scope_kind='deployment'`` rows.

        Deployment rows carry ``workspace_id IS NULL``; the ORM
        tenant filter rewrites every workspace-scoped read with
        ``WHERE workspace_id = :ctx``, so a NULL never matches.
        Regression guard: deployment audit must never leak into a
        tenant timeline (§15 "Audit log").
        """
        # Seed one workspace row + one deployment row.
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK00000000000000IS",
                    action="task.created",
                )
        finally:
            reset_current(token)

        with (
            filtered_factory() as writer,
            tenant_agnostic(),
            writer.begin(),
        ):
            write_deployment_audit(
                writer,
                **_DEPLOYMENT_KW,
                entity_kind="api_token",
                entity_id="01HWADEP00000000000000IS",
                action="api_token.created",
            )

        # Read under the workspace ctx; only the workspace row surfaces.
        token = set_current(_CTX)
        try:
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id.in_(
                            [
                                "01HWATASK00000000000000IS",
                                "01HWADEP00000000000000IS",
                            ]
                        )
                    )
                ).all()
                assert [r.entity_id for r in rows] == ["01HWATASK00000000000000IS"]
        finally:
            reset_current(token)

        # Read under tenant_agnostic; both rows surface.
        with filtered_factory() as reader, tenant_agnostic():
            rows = reader.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id.in_(
                        [
                            "01HWATASK00000000000000IS",
                            "01HWADEP00000000000000IS",
                        ]
                    )
                )
            ).all()
            assert sorted(r.entity_id for r in rows) == [
                "01HWADEP00000000000000IS",
                "01HWATASK00000000000000IS",
            ]


def _ctx_now() -> datetime:
    """Return a UTC-aware ``datetime`` for direct ``AuditLog`` construction.

    The two CHECK-firing tests above bypass :func:`write_deployment_audit`
    to exercise the DB-level constraint, which means they need their
    own ``created_at`` value (the writer would normally supply it via
    :class:`~app.util.clock.SystemClock`). Pinned so two rows in the
    same test sort deterministically.
    """
    return datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
