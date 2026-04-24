"""Integration tests for :mod:`app.adapters.db.llm` against a real DB.

Covers the post-migration schema shape (tables, FK targets, CHECK
constraints, indexes), the referential-integrity contract (workspace
CASCADE sweeps every row; user SET NULL on
``agent_token.delegating_user_id`` /
``approval_request.requester_actor_id`` / ``approval_request.decided_by``;
no user FK on ``llm_usage``; no FK on ``model_id`` — soft reference),
happy-path CRUD round-trip of every model, the pending-queue hot-path
query, the per-capability usage breakdown query, the unique-index
(model_assignment, budget_ledger), CHECK violations, cross-workspace
isolation, and tenant-filter behaviour (all five tables scoped; SELECT
without a :class:`WorkspaceContext` raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_llm.py`` covers pure-Python model
construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import (
    AgentToken,
    ApprovalRequest,
    BudgetLedger,
    LlmCapabilityInheritance,
    LlmUsage,
    ModelAssignment,
)
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
_PERIOD_END = _PINNED + timedelta(days=30)


_LLM_TABLES: tuple[str, ...] = (
    "model_assignment",
    "agent_token",
    "approval_request",
    "llm_usage",
    "budget_ledger",
    "llm_capability_inheritance",
)


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
def _ensure_llm_registered() -> None:
    """Re-register the LLM tables as workspace-scoped.

    ``app.adapters.db.llm.__init__`` registers them at import time,
    but a sibling unit test (``tests/unit/test_tenancy_orm_filter.py``)
    calls :func:`registry._reset_for_tests` in an autouse fixture,
    which wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite. Mirrors the pattern in
    ``tests/integration/test_db_messaging.py``.
    """
    for table in _LLM_TABLES:
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
    """The migration lands all five tables with the correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _LLM_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_model_assignment_columns(self, engine: Engine) -> None:
        """cd-u84y: ``model_assignment`` carries the full v1 tuning shape.

        Includes the priority / enabled / tuning columns the §11
        resolver depends on; ``max_tokens`` and ``temperature`` are
        nullable (NULL = inherit the provider-model default).
        """
        cols = {c["name"]: c for c in inspect(engine).get_columns("model_assignment")}
        expected = {
            "id",
            "workspace_id",
            "capability",
            "model_id",
            "provider",
            "priority",
            "enabled",
            "max_tokens",
            "temperature",
            "extra_api_params",
            "required_capabilities",
            "created_at",
        }
        assert set(cols) == expected
        nullable = {"max_tokens", "temperature"}
        for col in nullable:
            assert cols[col]["nullable"] is True, f"{col} must be NULLABLE"
        for notnull in expected - nullable:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_model_assignment_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("model_assignment")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # ``model_id`` is a soft reference — no FK per the model
        # docstring (``llm_model`` registry lands later).
        assert ("model_id",) not in fks

    def test_model_assignment_priority_index(self, engine: Engine) -> None:
        """cd-u84y: non-unique ``(workspace_id, capability, priority)`` index.

        Replaces the cd-cm5 unique ``(workspace_id, capability)`` index.
        The non-uniqueness is the feature: the §11 router's fallback
        chain is multiple rows per capability, ordered by priority.
        Leading ``workspace_id`` carries the tenant filter; per-
        capability lookup rides the ``(workspace_id, capability)``
        prefix of the same index.
        """
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("model_assignment")
        }
        assert "ix_model_assignment_workspace_capability_priority" in indexes
        ix = indexes["ix_model_assignment_workspace_capability_priority"]
        assert ix["column_names"] == ["workspace_id", "capability", "priority"]
        # SQLite's inspector returns 1/0, PG returns True/False — coerce.
        assert bool(ix["unique"]) is False
        # The cd-cm5 unique index is gone — guard against re-introducing
        # the "one row per capability" rule the resolver relies on being
        # absent.
        assert "uq_model_assignment_workspace_capability" not in indexes

    def test_agent_token_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("agent_token")}
        expected = {
            "id",
            "workspace_id",
            "delegating_user_id",
            "label",
            "prefix",
            "hash",
            "scope_json",
            "expires_at",
            "created_at",
            "revoked_at",
            "last_used_at",
        }
        assert set(cols) == expected
        nullable = {"delegating_user_id", "revoked_at", "last_used_at"}
        for col in nullable:
            assert cols[col]["nullable"] is True, f"{col} must be NULLABLE"
        for notnull in expected - nullable:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_agent_token_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("agent_token")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("delegating_user_id",)]["referred_table"] == "user"
        # SET NULL so audit trail survives user hard-delete.
        assert fks[("delegating_user_id",)]["options"].get("ondelete") == "SET NULL"

    def test_agent_token_workspace_prefix_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("agent_token")}
        assert "ix_agent_token_workspace_prefix" in indexes
        assert indexes["ix_agent_token_workspace_prefix"]["column_names"] == [
            "workspace_id",
            "prefix",
        ]

    def test_agent_token_hash_is_unique(self, engine: Engine) -> None:
        """``agent_token.hash`` is a unique constraint at the DB layer.

        Matches the sibling ``api_token.hash`` invariant. The
        ``inspect(engine).get_unique_constraints`` surface returns
        the explicit ``uq_agent_token_hash`` constraint on PG; on
        SQLite the column-level ``unique=True`` may materialise as
        either a constraint or a unique index depending on the
        emitter, so we probe both surfaces and require one of them
        to cover ``hash``.
        """
        insp = inspect(engine)
        uqs = insp.get_unique_constraints("agent_token")
        indexes = insp.get_indexes("agent_token")
        hash_uq = any(uq["column_names"] == ["hash"] for uq in uqs) or any(
            ix.get("unique") and ix["column_names"] == ["hash"] for ix in indexes
        )
        assert hash_uq, "agent_token.hash must be unique"

    def test_approval_request_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("approval_request")}
        expected = {
            "id",
            "workspace_id",
            "requester_actor_id",
            "action_json",
            "status",
            "decided_by",
            "decided_at",
            "rationale_md",
            "created_at",
        }
        assert set(cols) == expected
        nullable = {"requester_actor_id", "decided_by", "decided_at", "rationale_md"}
        for col in nullable:
            assert cols[col]["nullable"] is True, f"{col} must be NULLABLE"
        for notnull in expected - nullable:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_approval_request_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("approval_request")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("requester_actor_id",)]["referred_table"] == "user"
        assert fks[("requester_actor_id",)]["options"].get("ondelete") == "SET NULL"
        assert fks[("decided_by",)]["referred_table"] == "user"
        assert fks[("decided_by",)]["options"].get("ondelete") == "SET NULL"

    def test_approval_request_pending_queue_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("approval_request")
        }
        assert "ix_approval_request_workspace_status_created" in indexes
        assert indexes["ix_approval_request_workspace_status_created"][
            "column_names"
        ] == ["workspace_id", "status", "created_at"]

    def test_llm_usage_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("llm_usage")}
        expected = {
            "id",
            "workspace_id",
            "capability",
            "model_id",
            "tokens_in",
            "tokens_out",
            "cost_cents",
            "latency_ms",
            "status",
            "correlation_id",
            "created_at",
        }
        assert set(cols) == expected
        for notnull in expected:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_llm_usage_fks(self, engine: Engine) -> None:
        """Only the workspace FK exists — ``model_id`` is a soft ref."""
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("llm_usage")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # No FK on ``model_id`` — soft reference, see model docstring.
        assert ("model_id",) not in fks

    def test_llm_usage_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("llm_usage")}
        assert "ix_llm_usage_workspace_created" in indexes
        assert indexes["ix_llm_usage_workspace_created"]["column_names"] == [
            "workspace_id",
            "created_at",
        ]
        assert "ix_llm_usage_workspace_capability_created" in indexes
        assert indexes["ix_llm_usage_workspace_capability_created"]["column_names"] == [
            "workspace_id",
            "capability",
            "created_at",
        ]

    def test_budget_ledger_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("budget_ledger")}
        expected = {
            "id",
            "workspace_id",
            "period_start",
            "period_end",
            "spent_cents",
            "cap_cents",
            "updated_at",
        }
        assert set(cols) == expected
        for notnull in expected:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_budget_ledger_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("budget_ledger")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"

    def test_budget_ledger_unique_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("budget_ledger")
        }
        assert "uq_budget_ledger_workspace_period" in indexes
        uq = indexes["uq_budget_ledger_workspace_period"]
        assert uq["column_names"] == [
            "workspace_id",
            "period_start",
            "period_end",
        ]
        assert bool(uq["unique"]) is True

    def test_llm_capability_inheritance_columns(self, engine: Engine) -> None:
        """cd-u84y: ``llm_capability_inheritance`` shape."""
        cols = {
            c["name"]: c
            for c in inspect(engine).get_columns("llm_capability_inheritance")
        }
        expected = {
            "id",
            "workspace_id",
            "capability",
            "inherits_from",
            "created_at",
        }
        assert set(cols) == expected
        for notnull in expected:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_llm_capability_inheritance_fks(self, engine: Engine) -> None:
        """``workspace_id`` CASCADE — sweeping a workspace sweeps its edges."""
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("llm_capability_inheritance")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"

    def test_llm_capability_inheritance_unique_index(self, engine: Engine) -> None:
        """Unique ``(workspace_id, capability)`` — one parent per child per ws."""
        indexes = {
            ix["name"]: ix
            for ix in inspect(engine).get_indexes("llm_capability_inheritance")
        }
        assert "uq_llm_capability_inheritance_workspace_capability" in indexes
        uq = indexes["uq_llm_capability_inheritance_workspace_capability"]
        assert uq["column_names"] == ["workspace_id", "capability"]
        assert bool(uq["unique"]) is True


class TestModelAssignmentCrud:
    """Insert + select + update round-trip on :class:`ModelAssignment`."""

    def test_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="ma-crud@example.com",
            display="MaCrud",
            slug="ma-crud-ws",
            name="MaCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = ModelAssignment(
                id="01HWA00000000000000000MAAA",
                workspace_id=workspace.id,
                capability="staff_chat",
                model_id="01HWA00000000000000000MDLA",
                provider="openrouter",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            loaded = db_session.get(ModelAssignment, row.id)
            assert loaded is not None
            assert loaded.capability == "staff_chat"
            assert loaded.model_id == "01HWA00000000000000000MDLA"
            assert loaded.provider == "openrouter"
            # SQLite drops tzinfo on round-trip; compare naive UTC form.
            assert loaded.created_at.replace(tzinfo=UTC) == _PINNED
        finally:
            reset_current(ctx_token)

    def test_same_capability_different_priority_coexist(
        self, db_session: Session
    ) -> None:
        """cd-u84y acceptance: two rows with same ``(workspace_id,
        capability)`` but different ``priority`` coexist.

        This is the regression test the §11 resolver's fallback chain
        depends on: a primary (``priority=0``) and a fallback
        (``priority=1``) for the same capability in the same workspace
        are both valid rows, ordered by priority at read time.
        """
        workspace, user = _bootstrap(
            db_session,
            email="ma-prio@example.com",
            display="MaPrio",
            slug="ma-prio-ws",
            name="MaPrioWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    ModelAssignment(
                        id="01HWA00000000000000000MAPA",
                        workspace_id=workspace.id,
                        capability="staff_chat",
                        model_id="01HWA00000000000000000MDLA",
                        provider="openrouter",
                        priority=0,
                        created_at=_PINNED,
                    ),
                    ModelAssignment(
                        id="01HWA00000000000000000MAPB",
                        workspace_id=workspace.id,
                        capability="staff_chat",  # same (workspace, capability)
                        model_id="01HWA00000000000000000MDLB",
                        provider="openrouter",
                        priority=1,
                        created_at=_LATER,
                    ),
                ]
            )
            # Both rows land — no unique constraint collides.
            db_session.flush()

            chain = db_session.scalars(
                select(ModelAssignment)
                .where(ModelAssignment.workspace_id == workspace.id)
                .where(ModelAssignment.capability == "staff_chat")
                .order_by(ModelAssignment.priority.asc())
            ).all()
            assert [r.priority for r in chain] == [0, 1]
            assert [r.model_id for r in chain] == [
                "01HWA00000000000000000MDLA",
                "01HWA00000000000000000MDLB",
            ]
        finally:
            reset_current(ctx_token)

    def test_defaults_on_bare_insert(self, db_session: Session) -> None:
        """cd-u84y acceptance: ``priority=0``, ``enabled=True``,
        ``extra_api_params={}``, ``required_capabilities=[]`` on the
        hydrated ORM row after a bare insert.
        """
        workspace, user = _bootstrap(
            db_session,
            email="ma-def@example.com",
            display="MaDef",
            slug="ma-def-ws",
            name="MaDefWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            # Construct without the cd-u84y tuning fields — the Python-
            # side ORM defaults kick on insert; the migration's
            # ``server_default`` backstops raw SQL.
            row = ModelAssignment(
                id="01HWA00000000000000000MADF",
                workspace_id=workspace.id,
                capability="staff_chat",
                model_id="01HWA00000000000000000MDLA",
                provider="openrouter",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(ModelAssignment, row.id)
            assert reloaded is not None
            assert reloaded.priority == 0
            assert reloaded.enabled is True
            assert reloaded.extra_api_params == {}
            assert reloaded.required_capabilities == []
            # Tuning fields stay NULL when the caller doesn't set them.
            assert reloaded.max_tokens is None
            assert reloaded.temperature is None
        finally:
            reset_current(ctx_token)

    def test_negative_priority_rejected(self, db_session: Session) -> None:
        """CHECK ``priority >= 0`` rejects a negative sort key.

        A negative would silently sort ahead of the primary and break
        every downstream reorder invariant. The defensive CHECK covers
        a buggy direct-insert path the API doesn't own.
        """
        workspace, user = _bootstrap(
            db_session,
            email="ma-neg@example.com",
            display="MaNeg",
            slug="ma-neg-ws",
            name="MaNegWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ModelAssignment(
                    id="01HWA00000000000000000MANG",
                    workspace_id=workspace.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    provider="openrouter",
                    priority=-1,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_tuning_fields_round_trip(self, db_session: Session) -> None:
        """cd-u84y tuning fields round-trip through a flush / reload."""
        workspace, user = _bootstrap(
            db_session,
            email="ma-tune@example.com",
            display="MaTune",
            slug="ma-tune-ws",
            name="MaTuneWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            extra = {"top_p": 0.95, "tool_choice": "auto"}
            req_caps = ["vision", "json_mode"]
            row = ModelAssignment(
                id="01HWA00000000000000000MATU",
                workspace_id=workspace.id,
                capability="documents.ocr",
                model_id="01HWA00000000000000000MDLA",
                provider="openrouter",
                priority=3,
                enabled=False,
                max_tokens=8192,
                temperature=0.15,
                extra_api_params=extra,
                required_capabilities=req_caps,
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(ModelAssignment, row.id)
            assert reloaded is not None
            assert reloaded.priority == 3
            assert reloaded.enabled is False
            assert reloaded.max_tokens == 8192
            assert reloaded.temperature == 0.15
            assert reloaded.extra_api_params == extra
            assert reloaded.required_capabilities == req_caps
        finally:
            reset_current(ctx_token)

    def test_different_capability_allowed(self, db_session: Session) -> None:
        """Different capabilities coexist in the same workspace."""
        workspace, user = _bootstrap(
            db_session,
            email="ma-diff@example.com",
            display="MaDiff",
            slug="ma-diff-ws",
            name="MaDiffWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    ModelAssignment(
                        id="01HWA00000000000000000MADA",
                        workspace_id=workspace.id,
                        capability="staff_chat",
                        model_id="01HWA00000000000000000MDLA",
                        provider="openrouter",
                        created_at=_PINNED,
                    ),
                    ModelAssignment(
                        id="01HWA00000000000000000MADB",
                        workspace_id=workspace.id,
                        capability="daily_digest",
                        model_id="01HWA00000000000000000MDLA",
                        provider="openrouter",
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()

            rows = db_session.scalars(
                select(ModelAssignment)
                .where(ModelAssignment.workspace_id == workspace.id)
                .order_by(ModelAssignment.capability)
            ).all()
            assert [r.capability for r in rows] == ["daily_digest", "staff_chat"]
        finally:
            reset_current(ctx_token)


class TestAgentTokenCrud:
    """Insert + select + revoke round-trip on :class:`AgentToken`."""

    def test_round_trip_and_listing(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="at-crud@example.com",
            display="AtCrud",
            slug="at-crud-ws",
            name="AtCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            token_a = AgentToken(
                id="01HWA00000000000000000ATKA",
                workspace_id=workspace.id,
                delegating_user_id=user.id,
                label="manager-chat-agent",
                prefix="mip_abc",
                hash="0" * 64,
                scope_json={"actions": ["expenses:write"]},
                expires_at=_LATER,
                created_at=_PINNED,
            )
            token_b = AgentToken(
                id="01HWA00000000000000000ATKB",
                workspace_id=workspace.id,
                delegating_user_id=user.id,
                label="worker-chat-agent",
                prefix="mip_xyz",
                hash="1" * 64,
                scope_json={},
                expires_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add_all([token_a, token_b])
            db_session.flush()

            # Listing: ``(workspace_id, prefix)`` hot path.
            rows = db_session.scalars(
                select(AgentToken)
                .where(AgentToken.workspace_id == workspace.id)
                .order_by(AgentToken.prefix)
            ).all()
            assert [r.prefix for r in rows] == ["mip_abc", "mip_xyz"]
            assert rows[0].scope_json == {"actions": ["expenses:write"]}
            assert rows[1].scope_json == {}
        finally:
            reset_current(ctx_token)

    def test_revoke_flow(self, db_session: Session) -> None:
        """Setting ``revoked_at`` preserves the row (no delete)."""
        workspace, user = _bootstrap(
            db_session,
            email="at-rev@example.com",
            display="AtRev",
            slug="at-rev-ws",
            name="AtRevWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            token = AgentToken(
                id="01HWA00000000000000000ATRA",
                workspace_id=workspace.id,
                delegating_user_id=user.id,
                label="worker-chat-agent",
                prefix="mip_rev",
                hash="2" * 64,
                expires_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add(token)
            db_session.flush()

            loaded = db_session.get(AgentToken, token.id)
            assert loaded is not None
            loaded.revoked_at = _LATER
            db_session.flush()
            db_session.expire_all()

            final = db_session.get(AgentToken, token.id)
            assert final is not None
            assert final.revoked_at is not None
            assert final.revoked_at.replace(tzinfo=UTC) == _LATER
            # Row is still materialised — revocation is soft.
            assert final.hash == "2" * 64
        finally:
            reset_current(ctx_token)

    def test_duplicate_hash_rejected(self, db_session: Session) -> None:
        """Acceptance: ``agent_token.hash`` must be unique per table.

        Mirrors the sibling ``api_token.hash`` invariant — the auth
        layer keys lookups off the sha256 hex and two rows carrying
        the same digest would be undisambiguatable. The DB enforces
        the rule regardless of which codepath minted the row.
        """
        workspace, user = _bootstrap(
            db_session,
            email="at-uq@example.com",
            display="AtUq",
            slug="at-uq-ws",
            name="AtUqWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                AgentToken(
                    id="01HWA00000000000000000ATUA",
                    workspace_id=workspace.id,
                    delegating_user_id=user.id,
                    label="dup-a",
                    prefix="mip_dua",
                    hash="f" * 64,  # same sha256 as below
                    expires_at=_LATER,
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                AgentToken(
                    id="01HWA00000000000000000ATUB",
                    workspace_id=workspace.id,
                    delegating_user_id=user.id,
                    label="dup-b",
                    prefix="mip_dub",
                    hash="f" * 64,  # collision
                    expires_at=_LATER,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_scope_json_default_empty(self, db_session: Session) -> None:
        """``scope_json`` defaults to ``{}`` via ``server_default``."""
        workspace, user = _bootstrap(
            db_session,
            email="at-sd@example.com",
            display="AtSd",
            slug="at-sd-ws",
            name="AtSdWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            # Construct without ``scope_json`` — the Python-side
            # default kicks at the ORM layer; the DB default backstops
            # it on raw SQL inserts.
            token = AgentToken(
                id="01HWA00000000000000000ATSD",
                workspace_id=workspace.id,
                delegating_user_id=user.id,
                label="empty-scope",
                prefix="mip_sd",
                hash="3" * 64,
                expires_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add(token)
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(AgentToken, token.id)
            assert reloaded is not None
            assert reloaded.scope_json == {}
        finally:
            reset_current(ctx_token)


class TestAgentTokenUserSetNull:
    """``AgentToken.delegating_user_id`` FK uses SET NULL — history survives."""

    def test_deleting_user_nulls_delegating_user_id(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, owner = _bootstrap(
            db_session,
            email="at-snull@example.com",
            display="AtSnull",
            slug="at-snull-ws",
            name="AtSnullWS",
        )
        # Seed a delegating user distinct from the workspace owner.
        clock = FrozenClock(_PINNED)
        delegator = bootstrap_user(
            db_session,
            email="delegator@example.com",
            display_name="Delegator",
            clock=clock,
        )

        ctx_token = set_current(_ctx_for(workspace, owner.id))
        try:
            token = AgentToken(
                id="01HWA00000000000000000ATSN",
                workspace_id=workspace.id,
                delegating_user_id=delegator.id,
                label="delegator-agent",
                prefix="mip_del",
                hash="4" * 64,
                expires_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add(token)
            db_session.flush()
        finally:
            reset_current(ctx_token)

        # User delete is a platform-level op that predates the
        # ``WorkspaceContext`` — user is a cross-tenant row.
        with tenant_agnostic():
            db_session.delete(delegator)
            db_session.flush()

        ctx_token = set_current(_ctx_for(workspace, owner.id))
        try:
            db_session.expire_all()
            reloaded = db_session.get(AgentToken, token.id)
            assert reloaded is not None
            # SET NULL — the token survives; delegator pointer is NULL.
            assert reloaded.delegating_user_id is None
            # Denormalised identity columns survive.
            assert reloaded.label == "delegator-agent"
            assert reloaded.hash == "4" * 64
        finally:
            reset_current(ctx_token)


class TestApprovalRequestCrud:
    """Insert + select + decision round-trip on :class:`ApprovalRequest`."""

    def test_pending_to_approved(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="ar-crud@example.com",
            display="ArCrud",
            slug="ar-crud-ws",
            name="ArCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            action = {
                "method": "POST",
                "path": "/api/v1/expenses",
                "body": {"vendor": "Marché Provence", "amount_minor": 2210},
                "idempotency_key": "01HWA00000000000000000IDMP",
            }
            row = ApprovalRequest(
                id="01HWA00000000000000000ARRA",
                workspace_id=workspace.id,
                requester_actor_id=user.id,
                action_json=action,
                status="pending",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            # Reviewer approves.
            loaded = db_session.get(ApprovalRequest, row.id)
            assert loaded is not None
            loaded.status = "approved"
            loaded.decided_by = user.id
            loaded.decided_at = _LATER
            loaded.rationale_md = "Looks right — approving."
            db_session.flush()
            db_session.expire_all()

            final = db_session.get(ApprovalRequest, row.id)
            assert final is not None
            assert final.status == "approved"
            assert final.decided_by == user.id
            assert final.rationale_md == "Looks right — approving."
            assert final.action_json == action
            assert final.decided_at is not None
            assert final.decided_at.replace(tzinfo=UTC) == _LATER
        finally:
            reset_current(ctx_token)

    def test_pending_queue_hot_path(self, db_session: Session) -> None:
        """``(workspace_id, status, created_at)`` serves pending pagination."""
        workspace, user = _bootstrap(
            db_session,
            email="ar-queue@example.com",
            display="ArQueue",
            slug="ar-queue-ws",
            name="ArQueueWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            older_pending = ApprovalRequest(
                id="01HWA00000000000000000ARQA",
                workspace_id=workspace.id,
                requester_actor_id=user.id,
                status="pending",
                created_at=_PINNED,
            )
            newer_pending = ApprovalRequest(
                id="01HWA00000000000000000ARQB",
                workspace_id=workspace.id,
                requester_actor_id=user.id,
                status="pending",
                created_at=_LATER,
            )
            already_approved = ApprovalRequest(
                id="01HWA00000000000000000ARQC",
                workspace_id=workspace.id,
                requester_actor_id=user.id,
                status="approved",
                decided_by=user.id,
                decided_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add_all([older_pending, newer_pending, already_approved])
            db_session.flush()

            # Pending queue: oldest first — only the two pending rows.
            pending = db_session.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.workspace_id == workspace.id)
                .where(ApprovalRequest.status == "pending")
                .order_by(ApprovalRequest.created_at.asc())
            ).all()
            assert [r.id for r in pending] == [
                "01HWA00000000000000000ARQA",
                "01HWA00000000000000000ARQB",
            ]
        finally:
            reset_current(ctx_token)

    def test_timed_out_status(self, db_session: Session) -> None:
        """The ``timed_out`` terminal state is acceptable per CHECK."""
        workspace, user = _bootstrap(
            db_session,
            email="ar-to@example.com",
            display="ArTo",
            slug="ar-to-ws",
            name="ArToWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = ApprovalRequest(
                id="01HWA00000000000000000ARTA",
                workspace_id=workspace.id,
                requester_actor_id=user.id,
                status="timed_out",
                rationale_md="auto-expired",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            loaded = db_session.get(ApprovalRequest, row.id)
            assert loaded is not None
            assert loaded.status == "timed_out"
            assert loaded.rationale_md == "auto-expired"
        finally:
            reset_current(ctx_token)


class TestLlmUsageCrud:
    """Insert + select round-trip on :class:`LlmUsage`."""

    def test_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="lu-crud@example.com",
            display="LuCrud",
            slug="lu-crud-ws",
            name="LuCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = LlmUsage(
                id="01HWA00000000000000000LUSA",
                workspace_id=workspace.id,
                capability="staff_chat",
                model_id="01HWA00000000000000000MDLA",
                tokens_in=1200,
                tokens_out=340,
                cost_cents=18,
                latency_ms=942,
                status="ok",
                correlation_id="01HWA00000000000000000CRLA",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            loaded = db_session.get(LlmUsage, row.id)
            assert loaded is not None
            assert loaded.capability == "staff_chat"
            assert loaded.tokens_in == 1200
            assert loaded.tokens_out == 340
            assert loaded.cost_cents == 18
            assert loaded.latency_ms == 942
            assert loaded.status == "ok"
            assert loaded.correlation_id == "01HWA00000000000000000CRLA"
        finally:
            reset_current(ctx_token)

    def test_per_capability_feed(self, db_session: Session) -> None:
        """``(workspace_id, capability, created_at)`` serves per-capability queries."""
        workspace, user = _bootstrap(
            db_session,
            email="lu-cap@example.com",
            display="LuCap",
            slug="lu-cap-ws",
            name="LuCapWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            rows = [
                LlmUsage(
                    id="01HWA00000000000000000LUPA",
                    workspace_id=workspace.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    cost_cents=10,
                    status="ok",
                    correlation_id="01HWA00000000000000000CR01",
                    created_at=_PINNED,
                ),
                LlmUsage(
                    id="01HWA00000000000000000LUPB",
                    workspace_id=workspace.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    cost_cents=20,
                    status="ok",
                    correlation_id="01HWA00000000000000000CR02",
                    created_at=_LATER,
                ),
                LlmUsage(
                    id="01HWA00000000000000000LUPC",
                    workspace_id=workspace.id,
                    capability="daily_digest",
                    model_id="01HWA00000000000000000MDLA",
                    cost_cents=5,
                    status="ok",
                    correlation_id="01HWA00000000000000000CR03",
                    created_at=_PINNED,
                ),
            ]
            db_session.add_all(rows)
            db_session.flush()

            chats = db_session.scalars(
                select(LlmUsage)
                .where(LlmUsage.workspace_id == workspace.id)
                .where(LlmUsage.capability == "staff_chat")
                .order_by(LlmUsage.created_at.asc())
            ).all()
            assert [r.id for r in chats] == [
                "01HWA00000000000000000LUPA",
                "01HWA00000000000000000LUPB",
            ]

            digests = db_session.scalars(
                select(LlmUsage)
                .where(LlmUsage.workspace_id == workspace.id)
                .where(LlmUsage.capability == "daily_digest")
            ).all()
            assert len(digests) == 1
        finally:
            reset_current(ctx_token)

    def test_refused_status_records(self, db_session: Session) -> None:
        """The ``refused`` status carries pre-flight + safety refusals.

        Documenting the §11 "budget_exceeded" rationale for the
        closed enum — the spec is silent on a DB-level ``status``
        taxonomy, so the adapter picks ``{ok, error, refused,
        timeout}``; ``refused`` is the bucket that covers both
        envelope-refused calls and provider content-safety
        refusals.
        """
        workspace, user = _bootstrap(
            db_session,
            email="lu-ref@example.com",
            display="LuRef",
            slug="lu-ref-ws",
            name="LuRefWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = LlmUsage(
                id="01HWA00000000000000000LURA",
                workspace_id=workspace.id,
                capability="staff_chat",
                model_id="01HWA00000000000000000MDLA",
                # Pre-flight refusal: no tokens, no latency — the call
                # never left the client.
                tokens_in=0,
                tokens_out=0,
                cost_cents=0,
                latency_ms=0,
                status="refused",
                correlation_id="01HWA00000000000000000CRLR",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            loaded = db_session.get(LlmUsage, row.id)
            assert loaded is not None
            assert loaded.status == "refused"
        finally:
            reset_current(ctx_token)

    def test_soft_ref_model_id_accepts_any_ulid(self, db_session: Session) -> None:
        """``model_id`` has no FK — any ULID lands without lookup."""
        workspace, user = _bootstrap(
            db_session,
            email="lu-soft@example.com",
            display="LuSoft",
            slug="lu-soft-ws",
            name="LuSoftWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = LlmUsage(
                id="01HWA00000000000000000LUSF",
                workspace_id=workspace.id,
                capability="staff_chat",
                # A ULID for an ``llm_model`` row that doesn't exist
                # yet — the registry table lands in a later slice.
                model_id="01HWA0000000000000000GHOST",
                status="ok",
                correlation_id="01HWA00000000000000000CRSF",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            loaded = db_session.get(LlmUsage, row.id)
            assert loaded is not None
            assert loaded.model_id == "01HWA0000000000000000GHOST"
        finally:
            reset_current(ctx_token)


class TestBudgetLedgerCrud:
    """Insert + select + update round-trip on :class:`BudgetLedger`."""

    def test_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bl-crud@example.com",
            display="BlCrud",
            slug="bl-crud-ws",
            name="BlCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = BudgetLedger(
                id="01HWA00000000000000000BLRA",
                workspace_id=workspace.id,
                period_start=_PINNED,
                period_end=_PERIOD_END,
                spent_cents=0,
                cap_cents=500,
                updated_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            # Worker refreshes the aggregate.
            loaded = db_session.get(BudgetLedger, row.id)
            assert loaded is not None
            loaded.spent_cents = 234
            loaded.updated_at = _LATER
            db_session.flush()
            db_session.expire_all()

            final = db_session.get(BudgetLedger, row.id)
            assert final is not None
            assert final.spent_cents == 234
            assert final.cap_cents == 500
            assert final.updated_at is not None
            assert final.updated_at.replace(tzinfo=UTC) == _LATER
        finally:
            reset_current(ctx_token)

    def test_unique_period_rejects_duplicate(self, db_session: Session) -> None:
        """Acceptance: unique ``(workspace_id, period_start, period_end)``."""
        workspace, user = _bootstrap(
            db_session,
            email="bl-uq@example.com",
            display="BlUq",
            slug="bl-uq-ws",
            name="BlUqWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000BLUA",
                    workspace_id=workspace.id,
                    period_start=_PINNED,
                    period_end=_PERIOD_END,
                    cap_cents=500,
                    updated_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000BLUB",
                    workspace_id=workspace.id,
                    period_start=_PINNED,  # same triple
                    period_end=_PERIOD_END,
                    cap_cents=1000,
                    updated_at=_LATER,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_overlapping_but_not_identical_period_allowed(
        self, db_session: Session
    ) -> None:
        """Uniqueness is on the exact triple — overlapping windows coexist.

        The unique index covers ``(workspace_id, period_start,
        period_end)`` exactly; a row whose window overlaps but is not
        identical to an existing row's (e.g. rolling-30d refreshed
        mid-window, or a nested window for a finer-grained cap) is
        permitted by the schema. Downstream layers decide whether
        overlapping ledgers are semantically meaningful; at the DB
        layer, we only reject exact duplicates.
        """
        workspace, user = _bootstrap(
            db_session,
            email="bl-over@example.com",
            display="BlOver",
            slug="bl-over-ws",
            name="BlOverWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            # First: the classic 30-day window.
            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000BLOA",
                    workspace_id=workspace.id,
                    period_start=_PINNED,
                    period_end=_PERIOD_END,
                    cap_cents=500,
                    updated_at=_PINNED,
                )
            )
            # Second: a window that shares ``period_start`` but ends
            # earlier — overlaps the first row's range without being
            # identical.
            shorter_end = _PINNED + timedelta(days=15)
            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000BLOB",
                    workspace_id=workspace.id,
                    period_start=_PINNED,
                    period_end=shorter_end,
                    cap_cents=500,
                    updated_at=_PINNED,
                )
            )
            # Third: a window that shares ``period_end`` but starts
            # later — overlaps the first row's range from the other side.
            later_start = _PINNED + timedelta(days=15)
            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000BLOC",
                    workspace_id=workspace.id,
                    period_start=later_start,
                    period_end=_PERIOD_END,
                    cap_cents=500,
                    updated_at=_PINNED,
                )
            )
            # All three land — the unique is on the exact triple, not
            # on a range-overlap predicate.
            db_session.flush()

            rows = db_session.scalars(
                select(BudgetLedger).where(BudgetLedger.workspace_id == workspace.id)
            ).all()
            assert len(rows) == 3
        finally:
            reset_current(ctx_token)

    def test_different_period_allowed(self, db_session: Session) -> None:
        """Two rows with different periods coexist."""
        workspace, user = _bootstrap(
            db_session,
            email="bl-diff@example.com",
            display="BlDiff",
            slug="bl-diff-ws",
            name="BlDiffWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            next_period_end = _PERIOD_END + timedelta(days=30)
            db_session.add_all(
                [
                    BudgetLedger(
                        id="01HWA00000000000000000BLDA",
                        workspace_id=workspace.id,
                        period_start=_PINNED,
                        period_end=_PERIOD_END,
                        cap_cents=500,
                        updated_at=_PINNED,
                    ),
                    BudgetLedger(
                        id="01HWA00000000000000000BLDB",
                        workspace_id=workspace.id,
                        period_start=_PERIOD_END,
                        period_end=next_period_end,
                        cap_cents=500,
                        updated_at=_PERIOD_END,
                    ),
                ]
            )
            # Both rows land — distinct periods are allowed.
            db_session.flush()
        finally:
            reset_current(ctx_token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums."""

    def test_bogus_approval_status_rejected(self, db_session: Session) -> None:
        """Acceptance: ``approval_request.status = 'bogus'`` rejected."""
        workspace, user = _bootstrap(
            db_session,
            email="bogus-ars@example.com",
            display="BogusArs",
            slug="bogus-ars-ws",
            name="BogusArsWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ApprovalRequest(
                    id="01HWA00000000000000000BARS",
                    workspace_id=workspace.id,
                    requester_actor_id=user.id,
                    status="escalated",  # not in the enum
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_bogus_llm_usage_status_rejected(self, db_session: Session) -> None:
        """Acceptance: ``llm_usage.status = 'bogus'`` rejected."""
        workspace, user = _bootstrap(
            db_session,
            email="bogus-lus@example.com",
            display="BogusLus",
            slug="bogus-lus-ws",
            name="BogusLusWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                LlmUsage(
                    id="01HWA00000000000000000BLUS",
                    workspace_id=workspace.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    status="paused",  # not in the enum
                    correlation_id="01HWA00000000000000000CRLB",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_inverted_budget_period_rejected(self, db_session: Session) -> None:
        """CHECK ``period_end > period_start`` rejects inverted windows."""
        workspace, user = _bootstrap(
            db_session,
            email="inv-blp@example.com",
            display="InvBlp",
            slug="inv-blp-ws",
            name="InvBlpWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000IBLP",
                    workspace_id=workspace.id,
                    # Inverted — end before start.
                    period_start=_PERIOD_END,
                    period_end=_PINNED,
                    cap_cents=500,
                    updated_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_equal_budget_period_rejected(self, db_session: Session) -> None:
        """CHECK ``period_end > period_start`` rejects zero-length window.

        Strict ``>`` (not ``>=``) means the boundary ``period_end =
        period_start`` is invalid too — a zero-length ledger window is
        a data bug. Guards the strictness of the CHECK body.
        """
        workspace, user = _bootstrap(
            db_session,
            email="eq-blp@example.com",
            display="EqBlp",
            slug="eq-blp-ws",
            name="EqBlpWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                BudgetLedger(
                    id="01HWA00000000000000000EBLP",
                    workspace_id=workspace.id,
                    period_start=_PINNED,
                    period_end=_PINNED,
                    cap_cents=500,
                    updated_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)


class TestApprovalRequestUserSetNull:
    """``ApprovalRequest`` user FKs use SET NULL — history survives.

    Both ``requester_actor_id`` and ``decided_by`` nullable on user
    delete so an approved action's audit trail does not vanish when
    the approving / requesting user is hard-deleted.
    """

    def test_deleting_user_nulls_both_fks(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, owner = _bootstrap(
            db_session,
            email="ar-snull-owner@example.com",
            display="ArSnullOwner",
            slug="ar-snull-ws",
            name="ArSnullWS",
        )
        clock = FrozenClock(_PINNED)
        requester = bootstrap_user(
            db_session,
            email="ar-requester@example.com",
            display_name="Requester",
            clock=clock,
        )
        approver = bootstrap_user(
            db_session,
            email="ar-approver@example.com",
            display_name="Approver",
            clock=clock,
        )

        ctx_token = set_current(_ctx_for(workspace, owner.id))
        try:
            row = ApprovalRequest(
                id="01HWA00000000000000000ARSN",
                workspace_id=workspace.id,
                requester_actor_id=requester.id,
                action_json={"method": "POST"},
                status="approved",
                decided_by=approver.id,
                decided_at=_LATER,
                rationale_md="ok",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()
        finally:
            reset_current(ctx_token)

        # Both user deletes are platform-level.
        with tenant_agnostic():
            db_session.delete(requester)
            db_session.delete(approver)
            db_session.flush()

        ctx_token = set_current(_ctx_for(workspace, owner.id))
        try:
            db_session.expire_all()
            reloaded = db_session.get(ApprovalRequest, row.id)
            assert reloaded is not None
            # SET NULL on both — row survives with nulled identity.
            assert reloaded.requester_actor_id is None
            assert reloaded.decided_by is None
            assert reloaded.status == "approved"
            assert reloaded.rationale_md == "ok"
        finally:
            reset_current(ctx_token)


class TestLlmCapabilityInheritanceCrud:
    """cd-u84y: insert + select + constraint coverage on the edge table."""

    def test_round_trip(self, db_session: Session) -> None:
        """Insert + reload a parent-child edge."""
        workspace, user = _bootstrap(
            db_session,
            email="lci-crud@example.com",
            display="LciCrud",
            slug="lci-crud-ws",
            name="LciCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = LlmCapabilityInheritance(
                id="01HWA00000000000000000LCIA",
                workspace_id=workspace.id,
                capability="chat.admin",
                inherits_from="chat.manager",
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            loaded = db_session.get(LlmCapabilityInheritance, row.id)
            assert loaded is not None
            assert loaded.capability == "chat.admin"
            assert loaded.inherits_from == "chat.manager"
        finally:
            reset_current(ctx_token)

    def test_unique_workspace_capability_rejects_duplicate(
        self, db_session: Session
    ) -> None:
        """cd-u84y acceptance: a second edge on the same child is rejected.

        Uniqueness on ``(workspace_id, capability)`` — a child has one
        parent or none. A duplicate would force the resolver to pick
        at random.
        """
        workspace, user = _bootstrap(
            db_session,
            email="lci-uq@example.com",
            display="LciUq",
            slug="lci-uq-ws",
            name="LciUqWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                LlmCapabilityInheritance(
                    id="01HWA00000000000000000LCUA",
                    workspace_id=workspace.id,
                    capability="chat.admin",
                    inherits_from="chat.manager",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                LlmCapabilityInheritance(
                    id="01HWA00000000000000000LCUB",
                    workspace_id=workspace.id,
                    capability="chat.admin",  # same child, same ws
                    inherits_from="chat.owner",  # different parent
                    created_at=_LATER,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_self_loop_rejected(self, db_session: Session) -> None:
        """cd-u84y acceptance: CHECK ``capability <> inherits_from`` rejects."""
        workspace, user = _bootstrap(
            db_session,
            email="lci-loop@example.com",
            display="LciLoop",
            slug="lci-loop-ws",
            name="LciLoopWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                LlmCapabilityInheritance(
                    id="01HWA00000000000000000LCLP",
                    workspace_id=workspace.id,
                    # Self-loop — CHECK must reject.
                    capability="chat.admin",
                    inherits_from="chat.admin",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_different_children_coexist(self, db_session: Session) -> None:
        """Different child capabilities inherit from the same parent."""
        workspace, user = _bootstrap(
            db_session,
            email="lci-diff@example.com",
            display="LciDiff",
            slug="lci-diff-ws",
            name="LciDiffWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    LlmCapabilityInheritance(
                        id="01HWA00000000000000000LCD1",
                        workspace_id=workspace.id,
                        capability="chat.admin",
                        inherits_from="chat.manager",
                        created_at=_PINNED,
                    ),
                    LlmCapabilityInheritance(
                        id="01HWA00000000000000000LCD2",
                        workspace_id=workspace.id,
                        capability="chat.owner",
                        inherits_from="chat.manager",
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()

            rows = db_session.scalars(
                select(LlmCapabilityInheritance)
                .where(LlmCapabilityInheritance.workspace_id == workspace.id)
                .order_by(LlmCapabilityInheritance.capability)
            ).all()
            assert [r.capability for r in rows] == ["chat.admin", "chat.owner"]
            assert all(r.inherits_from == "chat.manager" for r in rows)
        finally:
            reset_current(ctx_token)

    def test_cross_workspace_isolation(self, db_session: Session) -> None:
        """cd-u84y acceptance: a row in workspace A is invisible under
        a workspace-B context.

        Mirrors the sibling LLM-layer cross-workspace isolation tests —
        the tenant filter auto-injects ``workspace_id``, and the same
        ``(capability, inherits_from)`` pair can coexist across
        workspaces without colliding.
        """
        ws_a, user = _bootstrap(
            db_session,
            email="lci-xws@example.com",
            display="LciXws",
            slug="lci-xws-a",
            name="LciXwsA",
        )
        clock = FrozenClock(_PINNED)
        ws_b = bootstrap_workspace(
            db_session,
            slug="lci-xws-b",
            name="LciXwsB",
            owner_user_id=user.id,
            clock=clock,
        )

        token_a = set_current(_ctx_for(ws_a, user.id))
        try:
            db_session.add(
                LlmCapabilityInheritance(
                    id="01HWA00000000000000000LCXA",
                    workspace_id=ws_a.id,
                    capability="chat.admin",
                    inherits_from="chat.manager",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token_a)

        token_b = set_current(_ctx_for(ws_b, user.id))
        try:
            # Same child / parent pair lands cleanly in the sibling ws.
            db_session.add(
                LlmCapabilityInheritance(
                    id="01HWA00000000000000000LCXB",
                    workspace_id=ws_b.id,
                    capability="chat.admin",
                    inherits_from="chat.manager",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            b_rows = db_session.scalars(
                select(LlmCapabilityInheritance).where(
                    LlmCapabilityInheritance.workspace_id == ws_b.id
                )
            ).all()
            assert [r.id for r in b_rows] == ["01HWA00000000000000000LCXB"]
        finally:
            reset_current(token_b)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps every LLM-layer row belonging to it."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="llm-cascade@example.com",
            display="LlmCascade",
            slug="llm-cascade-ws",
            name="LlmCascadeWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    ModelAssignment(
                        id="01HWA00000000000000000MACW",
                        workspace_id=workspace.id,
                        capability="staff_chat",
                        model_id="01HWA00000000000000000MDLA",
                        provider="openrouter",
                        created_at=_PINNED,
                    ),
                    AgentToken(
                        id="01HWA00000000000000000ATCW",
                        workspace_id=workspace.id,
                        delegating_user_id=user.id,
                        label="w",
                        prefix="mip_w",
                        hash="5" * 64,
                        expires_at=_LATER,
                        created_at=_PINNED,
                    ),
                    ApprovalRequest(
                        id="01HWA00000000000000000ARCW",
                        workspace_id=workspace.id,
                        requester_actor_id=user.id,
                        status="pending",
                        created_at=_PINNED,
                    ),
                    LlmUsage(
                        id="01HWA00000000000000000LUCW",
                        workspace_id=workspace.id,
                        capability="staff_chat",
                        model_id="01HWA00000000000000000MDLA",
                        status="ok",
                        correlation_id="01HWA00000000000000000CRLC",
                        created_at=_PINNED,
                    ),
                    BudgetLedger(
                        id="01HWA00000000000000000BLCW",
                        workspace_id=workspace.id,
                        period_start=_PINNED,
                        period_end=_PERIOD_END,
                        cap_cents=500,
                        updated_at=_PINNED,
                    ),
                    LlmCapabilityInheritance(
                        id="01HWA00000000000000000LCCW",
                        workspace_id=workspace.id,
                        capability="chat.admin",
                        inherits_from="chat.manager",
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()
        finally:
            reset_current(ctx_token)

        # Workspace delete predates the ctx.
        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            for model in (
                ModelAssignment,
                AgentToken,
                ApprovalRequest,
                LlmUsage,
                BudgetLedger,
                LlmCapabilityInheritance,
            ):
                rows = db_session.scalars(
                    select(model).where(model.workspace_id == workspace.id)
                ).all()
                assert rows == [], f"{model.__tablename__} not swept"
        finally:
            reset_current(ctx_token)


class TestCrossWorkspaceIsolation:
    """LLM-layer rows do not leak across workspaces."""

    def test_model_assignment_same_capability_different_workspace(
        self, db_session: Session
    ) -> None:
        """Two workspaces can each bind the same capability."""
        ws_a, user = _bootstrap(
            db_session,
            email="ma-xws@example.com",
            display="MaXws",
            slug="ma-xws-a",
            name="MaXwsA",
        )
        clock = FrozenClock(_PINNED)
        ws_b = bootstrap_workspace(
            db_session,
            slug="ma-xws-b",
            name="MaXwsB",
            owner_user_id=user.id,
            clock=clock,
        )

        token_a = set_current(_ctx_for(ws_a, user.id))
        try:
            db_session.add(
                ModelAssignment(
                    id="01HWA00000000000000000MXA1",
                    workspace_id=ws_a.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    provider="openrouter",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token_a)

        token_b = set_current(_ctx_for(ws_b, user.id))
        try:
            db_session.add(
                ModelAssignment(
                    id="01HWA00000000000000000MXB1",
                    workspace_id=ws_b.id,
                    capability="staff_chat",  # same capability, different ws
                    model_id="01HWA00000000000000000MDLB",
                    provider="openrouter",
                    created_at=_PINNED,
                )
            )
            # Both rows land — the unique is per-workspace.
            db_session.flush()

            b_rows = db_session.scalars(
                select(ModelAssignment).where(ModelAssignment.workspace_id == ws_b.id)
            ).all()
            assert [r.model_id for r in b_rows] == ["01HWA00000000000000000MDLB"]
        finally:
            reset_current(token_b)

    def test_llm_usage_scoped_per_workspace(self, db_session: Session) -> None:
        """``LlmUsage`` rows do not leak across workspaces."""
        ws_a, user_a = _bootstrap(
            db_session,
            email="lu-xws-a@example.com",
            display="LuXwsA",
            slug="lu-xws-a-ws",
            name="LuXwsAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="lu-xws-b@example.com",
            display="LuXwsB",
            slug="lu-xws-b-ws",
            name="LuXwsBWS",
        )

        token_a = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                LlmUsage(
                    id="01HWA00000000000000000LXA1",
                    workspace_id=ws_a.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    status="ok",
                    correlation_id="01HWA00000000000000000CRXA",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token_a)

        token_b = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                LlmUsage(
                    id="01HWA00000000000000000LXB1",
                    workspace_id=ws_b.id,
                    capability="staff_chat",
                    model_id="01HWA00000000000000000MDLA",
                    status="ok",
                    correlation_id="01HWA00000000000000000CRXB",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            b_rows = db_session.scalars(
                select(LlmUsage).where(LlmUsage.workspace_id == ws_b.id)
            ).all()
            assert {r.correlation_id for r in b_rows} == {"01HWA00000000000000000CRXB"}

            a_rows = db_session.scalars(
                select(LlmUsage).where(LlmUsage.workspace_id == ws_a.id)
            ).all()
            assert {r.correlation_id for r in a_rows} == {"01HWA00000000000000000CRXA"}
        finally:
            reset_current(token_b)


class TestTenantFilter:
    """Every LLM table is workspace-scoped under the filter.

    Covers the cd-cm5 quintet plus the cd-u84y
    ``llm_capability_inheritance`` edge table — the tenancy filter
    auto-injects ``workspace_id`` on every SELECT, and a bare read
    without a :class:`WorkspaceContext` raises
    :class:`TenantFilterMissing`.
    """

    @pytest.mark.parametrize(
        "model",
        [
            ModelAssignment,
            AgentToken,
            ApprovalRequest,
            LlmUsage,
            BudgetLedger,
            LlmCapabilityInheritance,
        ],
    )
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[ModelAssignment]
        | type[AgentToken]
        | type[ApprovalRequest]
        | type[LlmUsage]
        | type[BudgetLedger]
        | type[LlmCapabilityInheritance],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__


class TestCdU84yMigrationRoundTrip:
    """cd-u84y migration lands cleanly, reverses, and re-applies.

    Mirrors the cd-i1qe migration smoke (see
    ``tests/integration/test_migration_cd_i1qe.py``): scratch SQLite
    file, ``alembic upgrade head``, introspect the added columns /
    table / indexes, ``downgrade -1`` to restore the cd-cm5 shape,
    then re-``upgrade head`` to prove the revision is reversible and
    idempotent.

    SQLite only — the cross-backend structural parity gate lives in
    ``tests/integration/test_schema_parity.py``. Narrowing this to
    the cd-u84y revision means a future breaking change points a
    failure straight at the migration rather than at a whole-schema
    fingerprint diff.
    """

    _REVISION_ID: str = "f6a7b8c9d0e1"
    _PREVIOUS_REVISION_ID: str = "e5f6a7b8c9d0"

    @staticmethod
    def _alembic_ini() -> Path:
        return Path(__file__).resolve().parents[2] / "alembic.ini"

    @staticmethod
    @contextmanager
    def _override_database_url(url: str) -> Iterator[None]:
        original = os.environ.get("CREWDAY_DATABASE_URL")
        os.environ["CREWDAY_DATABASE_URL"] = url
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            yield
        finally:
            if original is None:
                os.environ.pop("CREWDAY_DATABASE_URL", None)
            else:
                os.environ["CREWDAY_DATABASE_URL"] = original
            get_settings.cache_clear()

    def test_upgrade_adds_columns_and_table(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """cd-u84y acceptance: upgrade lands the columns + new table."""
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from app.adapters.db.session import make_engine

        db_path = tmp_path_factory.mktemp("cd-u84y-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            insp = inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("model_assignment")}
            for added in (
                "priority",
                "enabled",
                "max_tokens",
                "temperature",
                "extra_api_params",
                "required_capabilities",
            ):
                assert added in cols, f"{added} missing after upgrade"
            assert cols["priority"]["nullable"] is False
            assert cols["enabled"]["nullable"] is False
            # ``max_tokens`` / ``temperature`` nullable: NULL = inherit
            # the provider-model default.
            assert cols["max_tokens"]["nullable"] is True
            assert cols["temperature"]["nullable"] is True

            indexes = {ix["name"]: ix for ix in insp.get_indexes("model_assignment")}
            # Old unique gone, new composite non-unique in.
            assert "uq_model_assignment_workspace_capability" not in indexes
            assert "ix_model_assignment_workspace_capability_priority" in indexes

            # New table landed with its shape.
            assert "llm_capability_inheritance" in insp.get_table_names()
            lci_cols = {
                c["name"]: c for c in insp.get_columns("llm_capability_inheritance")
            }
            assert set(lci_cols) == {
                "id",
                "workspace_id",
                "capability",
                "inherits_from",
                "created_at",
            }
        finally:
            engine.dispose()

    def test_downgrade_reverses_columns_and_table(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """cd-u84y acceptance: downgrade -1 restores the cd-cm5 shape."""
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from app.adapters.db.session import make_engine

        db_path = tmp_path_factory.mktemp("cd-u84y-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, self._PREVIOUS_REVISION_ID)

            insp = inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("model_assignment")}
            for absent in (
                "priority",
                "enabled",
                "max_tokens",
                "temperature",
                "extra_api_params",
                "required_capabilities",
            ):
                assert absent not in cols, f"{absent} still present after downgrade"

            indexes = {ix["name"]: ix for ix in insp.get_indexes("model_assignment")}
            # cd-cm5 unique restored; cd-u84y composite gone.
            assert "uq_model_assignment_workspace_capability" in indexes
            assert "ix_model_assignment_workspace_capability_priority" not in indexes

            assert "llm_capability_inheritance" not in insp.get_table_names()
        finally:
            engine.dispose()

    def test_upgrade_after_downgrade_is_idempotent(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """cd-u84y acceptance: upgrade → downgrade → upgrade cycles clean."""
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from app.adapters.db.session import make_engine

        db_path = tmp_path_factory.mktemp("cd-u84y-mig-cycle") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, self._PREVIOUS_REVISION_ID)
                command.upgrade(cfg, self._REVISION_ID)

            insp = inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("model_assignment")}
            assert "priority" in cols
            assert "enabled" in cols
            assert "llm_capability_inheritance" in insp.get_table_names()
        finally:
            engine.dispose()

    def test_downgrade_preserves_primary_rows(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Happy path: ``priority=0`` rows survive the downgrade.

        A workspace carrying a single primary assignment per capability
        is the pre-cd-u84y shape — these rows are already compatible
        with the restored unique index and must round-trip untouched.
        """
        from alembic import command
        from alembic.config import Config as AlembicConfig
        from sqlalchemy import text

        from app.adapters.db.session import make_engine

        db_path = tmp_path_factory.mktemp("cd-u84y-mig-down-happy") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            # Seed: workspace + one primary assignment per capability.
            # Raw SQL (no ORM factories) so the test owns the row shape
            # at this exact revision.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workspace "
                        "(id, slug, name, plan, quota_json, created_at) "
                        "VALUES ('01HWA00000000000000000WDGH', 'dg-happy', "
                        "'DgHappy', 'free', '{}', '2026-04-24T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO model_assignment "
                        "(id, workspace_id, capability, model_id, provider, "
                        "priority, enabled, extra_api_params, "
                        "required_capabilities, created_at) VALUES "
                        "('01HWA00000000000000000MAHP', "
                        "'01HWA00000000000000000WDGH', 'staff_chat', "
                        "'01HWA00000000000000000MDLA', 'openrouter', "
                        "0, 1, '{}', '[]', '2026-04-24T12:00:00+00:00')"
                    )
                )

            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, self._PREVIOUS_REVISION_ID)

            # Primary survives under the restored pre-cd-u84y shape.
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id FROM model_assignment ORDER BY id")
                ).fetchall()
            assert [r[0] for r in rows] == ["01HWA00000000000000000MAHP"]
        finally:
            engine.dispose()

    def test_downgrade_sweeps_fallback_rows(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Sad path: ``priority > 0`` rungs are swept so the restored
        unique index lands cleanly.

        Mirrors the cd-i1qe PAT sweep (``DELETE FROM api_token WHERE
        kind = 'personal'``) — under cd-u84y a capability may carry
        several rungs, but the pre-cd-u84y schema allows only one per
        ``(workspace_id, capability)``. Leaving duplicates in place
        would collide with the restored unique and silently fail the
        rollback. The sweep deletes every ``priority > 0`` row; the
        primary (``priority=0``) survives.
        """
        from alembic import command
        from alembic.config import Config as AlembicConfig
        from sqlalchemy import text

        from app.adapters.db.session import make_engine

        db_path = tmp_path_factory.mktemp("cd-u84y-mig-down-sad") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            # Seed: one primary + two fallback rungs on the same
            # (workspace, capability). Raw SQL keeps the test
            # revision-agnostic.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workspace "
                        "(id, slug, name, plan, quota_json, created_at) "
                        "VALUES ('01HWA00000000000000000WDGS', 'dg-sad', "
                        "'DgSad', 'free', '{}', '2026-04-24T12:00:00+00:00')"
                    )
                )
                for index, priority in enumerate((0, 1, 2)):
                    conn.execute(
                        text(
                            "INSERT INTO model_assignment "
                            "(id, workspace_id, capability, model_id, provider, "
                            "priority, enabled, extra_api_params, "
                            "required_capabilities, created_at) VALUES "
                            f"('01HWA00000000000000000MS{index}P', "
                            "'01HWA00000000000000000WDGS', 'staff_chat', "
                            f"'01HWA0000000000000000MDL{index}', 'openrouter', "
                            f"{priority}, 1, '{{}}', '[]', "
                            "'2026-04-24T12:00:00+00:00')"
                        )
                    )

            # Downgrade must succeed — the sweep removes the two
            # fallback rungs before rebuilding the unique index.
            with self._override_database_url(url):
                cfg = AlembicConfig(str(self._alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, self._PREVIOUS_REVISION_ID)

            # Only the priority=0 primary survives; duplicates are gone.
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id FROM model_assignment ORDER BY id")
                ).fetchall()
            assert [r[0] for r in rows] == ["01HWA00000000000000000MS0P"]

            # And the restored unique landed — inserting a collision now
            # at the downgraded shape must fail.
            with (
                engine.begin() as conn,
                pytest.raises(IntegrityError),
            ):
                conn.execute(
                    text(
                        "INSERT INTO model_assignment "
                        "(id, workspace_id, capability, model_id, "
                        "provider, created_at) VALUES "
                        "('01HWA00000000000000000MSDUP', "
                        "'01HWA00000000000000000WDGS', 'staff_chat', "
                        "'01HWA00000000000000000MDLZ', 'openrouter', "
                        "'2026-04-24T12:00:00+00:00')"
                    )
                )
        finally:
            engine.dispose()
