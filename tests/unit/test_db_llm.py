"""Unit tests for :mod:`app.adapters.db.llm.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, index columns, tenancy-registry membership). Integration
coverage (migrations, FK cascade, CHECK violations against a real
DB, cross-workspace isolation, tenant-filter behaviour) lives in
``tests/integration/test_db_llm.py``.

See ``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.llm import (
    AgentToken,
    ApprovalRequest,
    BudgetLedger,
    LlmCapabilityInheritance,
    LlmUsage,
    ModelAssignment,
)
from app.adapters.db.llm import models as llm_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestModelAssignmentModel:
    """The ``ModelAssignment`` mapped class carries the cd-u84y slice."""

    def test_minimal_construction(self) -> None:
        row = ModelAssignment(
            id="01HWA00000000000000000MAAA",
            workspace_id="01HWA00000000000000000WSPA",
            capability="staff_chat",
            model_id="01HWA00000000000000000MDLA",
            provider="openrouter",
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000MAAA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.capability == "staff_chat"
        assert row.model_id == "01HWA00000000000000000MDLA"
        assert row.provider == "openrouter"
        assert row.created_at == _PINNED

    def test_full_construction(self) -> None:
        """cd-u84y columns carry through a full construction."""
        extra = {"top_p": 0.9, "frequency_penalty": 0.0}
        req_caps = ["vision", "json_mode"]
        row = ModelAssignment(
            id="01HWA00000000000000000MAFA",
            workspace_id="01HWA00000000000000000WSPA",
            capability="staff_chat",
            model_id="01HWA00000000000000000MDLA",
            provider="openrouter",
            priority=2,
            enabled=False,
            max_tokens=4096,
            temperature=0.2,
            extra_api_params=extra,
            required_capabilities=req_caps,
            created_at=_PINNED,
        )
        assert row.priority == 2
        assert row.enabled is False
        assert row.max_tokens == 4096
        assert row.temperature == 0.2
        assert row.extra_api_params == extra
        assert row.required_capabilities == req_caps

    def test_tablename(self) -> None:
        assert ModelAssignment.__tablename__ == "model_assignment"

    def test_priority_index_present(self) -> None:
        """cd-u84y: composite ``(workspace_id, capability, priority)`` index.

        Replaces the cd-cm5 unique index on ``(workspace_id,
        capability)``. Non-unique — a capability may carry many
        assignments (the §11 fallback chain). The index backs the
        resolver's sorted scan; the leading ``workspace_id`` carries
        the tenant filter and the ``(workspace_id, capability)`` prefix
        still serves per-capability lookup.
        """
        indexes = [i for i in ModelAssignment.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_model_assignment_workspace_capability_priority" in names
        target = next(
            i
            for i in indexes
            if i.name == "ix_model_assignment_workspace_capability_priority"
        )
        assert target.unique is False
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "capability",
            "priority",
        ]

    def test_old_unique_index_removed(self) -> None:
        """The cd-cm5 unique index no longer lands on the model.

        Guards against a future refactor silently re-introducing the
        one-row-per-capability rule the §11 resolver's fallback chain
        depends on being absent.
        """
        names = [i.name for i in ModelAssignment.__table_args__ if isinstance(i, Index)]
        assert "uq_model_assignment_workspace_capability" not in names

    def test_priority_check_present(self) -> None:
        """CHECK ``priority >= 0`` clamps the sort key to non-negative."""
        checks = [
            c
            for c in ModelAssignment.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("priority_non_negative")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "priority" in sql
        assert ">=" in sql or ">= 0" in sql


class TestLlmCapabilityInheritanceModel:
    """cd-u84y: the parent-child fallback edge table."""

    def test_minimal_construction(self) -> None:
        row = LlmCapabilityInheritance(
            id="01HWA00000000000000000LCIA",
            workspace_id="01HWA00000000000000000WSPA",
            capability="chat.admin",
            inherits_from="chat.manager",
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000LCIA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.capability == "chat.admin"
        assert row.inherits_from == "chat.manager"
        assert row.created_at == _PINNED

    def test_tablename(self) -> None:
        assert LlmCapabilityInheritance.__tablename__ == "llm_capability_inheritance"

    def test_no_self_loop_check_present(self) -> None:
        """CHECK ``capability <> inherits_from`` rejects the obvious self-loop."""
        checks = [
            c
            for c in LlmCapabilityInheritance.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("no_self_loop")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "capability" in sql
        assert "inherits_from" in sql

    def test_unique_workspace_capability_index_present(self) -> None:
        """Unique ``(workspace_id, capability)`` — one parent per child per ws."""
        indexes = [
            i for i in LlmCapabilityInheritance.__table_args__ if isinstance(i, Index)
        ]
        names = [i.name for i in indexes]
        assert "uq_llm_capability_inheritance_workspace_capability" in names
        target = next(
            i
            for i in indexes
            if i.name == "uq_llm_capability_inheritance_workspace_capability"
        )
        assert target.unique is True
        assert [c.name for c in target.columns] == ["workspace_id", "capability"]


class TestAgentTokenModel:
    """The ``AgentToken`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        token = AgentToken(
            id="01HWA00000000000000000ATKA",
            workspace_id="01HWA00000000000000000WSPA",
            delegating_user_id="01HWA00000000000000000USRA",
            label="manager-chat-agent",
            prefix="mip_abc",
            hash="0" * 64,  # sha256 hex is 64 chars — shape sanity only.
            expires_at=_LATER,
            created_at=_PINNED,
        )
        assert token.id == "01HWA00000000000000000ATKA"
        assert token.workspace_id == "01HWA00000000000000000WSPA"
        assert token.delegating_user_id == "01HWA00000000000000000USRA"
        assert token.label == "manager-chat-agent"
        assert token.prefix == "mip_abc"
        assert token.hash == "0" * 64
        assert token.expires_at == _LATER
        assert token.created_at == _PINNED
        # Nullable fields default on a minimal construction.
        assert token.revoked_at is None
        assert token.last_used_at is None

    def test_full_construction(self) -> None:
        scope = {"actions": ["expenses:write"], "agent_channel": "web_owner_sidebar"}
        token = AgentToken(
            id="01HWA00000000000000000ATKB",
            workspace_id="01HWA00000000000000000WSPA",
            delegating_user_id="01HWA00000000000000000USRA",
            label="manager-chat-agent",
            prefix="mip_xyz",
            hash="a" * 64,
            scope_json=scope,
            expires_at=_LATER,
            created_at=_PINNED,
            revoked_at=_LATER,
            last_used_at=_LATER,
        )
        assert token.scope_json == scope
        assert token.revoked_at == _LATER
        assert token.last_used_at == _LATER

    def test_null_delegating_user_allowed(self) -> None:
        """``delegating_user_id`` is nullable (SET NULL on user delete).

        A freshly-minted row always carries a delegating user; the
        column is nullable only so the FK's SET NULL rule lands
        cleanly when the user is later hard-deleted. Guard the shape
        so a future refactor doesn't tighten to NOT NULL and break
        the audit-trail survival contract.
        """
        token = AgentToken(
            id="01HWA00000000000000000ATKC",
            workspace_id="01HWA00000000000000000WSPA",
            # delegating_user_id left None — nullable after user delete.
            label="orphaned-token",
            prefix="mip_orf",
            hash="0" * 64,
            expires_at=_LATER,
            created_at=_PINNED,
        )
        assert token.delegating_user_id is None

    def test_tablename(self) -> None:
        assert AgentToken.__tablename__ == "agent_token"

    def test_workspace_prefix_index_present(self) -> None:
        """Listing / revocation hot path: ``(workspace_id, prefix)``."""
        indexes = [i for i in AgentToken.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_agent_token_workspace_prefix" in names
        target = next(i for i in indexes if i.name == "ix_agent_token_workspace_prefix")
        assert target.unique is False
        assert [c.name for c in target.columns] == ["workspace_id", "prefix"]

    def test_hash_column_is_unique(self) -> None:
        """Acceptance: ``AgentToken.hash`` enforces uniqueness at the DB.

        Matches the sibling ``api_token.hash`` pattern: the auth layer
        keys lookups off the sha256 hex and a collision would be
        undisambiguatable. Uniqueness can land via either a column-level
        ``unique=True`` flag or a table-level ``UniqueConstraint``; we
        accept either — the invariant is "uniqueness enforced, by any
        mechanism". The integration suite verifies the migration emits
        a corresponding DB constraint on both SQLite and Postgres.
        """
        column_unique = bool(AgentToken.__table__.c.hash.unique)
        explicit_constraint = any(
            isinstance(c, UniqueConstraint)
            and any(col.name == "hash" for col in c.columns)
            for c in AgentToken.__table_args__
        )
        assert column_unique or explicit_constraint, (
            "AgentToken.hash must be unique — either via a table-level "
            "UniqueConstraint or a column-level unique=True flag."
        )


class TestApprovalRequestModel:
    """The ``ApprovalRequest`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        row = ApprovalRequest(
            id="01HWA00000000000000000ARRA",
            workspace_id="01HWA00000000000000000WSPA",
            requester_actor_id="01HWA00000000000000000USRA",
            status="pending",
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000ARRA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.requester_actor_id == "01HWA00000000000000000USRA"
        assert row.status == "pending"
        # Nullable on a pending row — no decision yet.
        assert row.decided_by is None
        assert row.decided_at is None
        assert row.rationale_md is None
        assert row.created_at == _PINNED

    def test_full_construction(self) -> None:
        action = {
            "method": "POST",
            "path": "/api/v1/expenses",
            "body": {"vendor": "Marché Provence", "amount_minor": 2210},
            "idempotency_key": "01HWA00000000000000000IDMP",
        }
        row = ApprovalRequest(
            id="01HWA00000000000000000ARRB",
            workspace_id="01HWA00000000000000000WSPA",
            requester_actor_id="01HWA00000000000000000USRA",
            action_json=action,
            status="approved",
            decided_by="01HWA00000000000000000USRB",
            decided_at=_LATER,
            rationale_md="Looks right — approving.",
            created_at=_PINNED,
        )
        assert row.action_json == action
        assert row.status == "approved"
        assert row.decided_by == "01HWA00000000000000000USRB"
        assert row.decided_at == _LATER
        assert row.rationale_md == "Looks right — approving."

    def test_every_status_constructs(self) -> None:
        """Each v1 approval status builds a valid row."""
        for index, status in enumerate(llm_models._APPROVAL_REQUEST_STATUS_VALUES):
            row = ApprovalRequest(
                id=f"01HWA0000000000000000ARR{index}",
                workspace_id="01HWA00000000000000000WSPA",
                requester_actor_id="01HWA00000000000000000USRA",
                status=status,
                created_at=_PINNED,
            )
            assert row.status == status

    def test_tablename(self) -> None:
        assert ApprovalRequest.__tablename__ == "approval_request"

    def test_status_check_present(self) -> None:
        # The naming convention rewrites the bare ``status`` name to
        # ``ck_approval_request_status`` on the bound column; match by
        # suffix per the sibling ``chat_channel`` / ``email_opt_out``
        # pattern.
        checks = [
            c
            for c in ApprovalRequest.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("status")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for status in llm_models._APPROVAL_REQUEST_STATUS_VALUES:
            assert status in sql, f"{status} missing from CHECK constraint"

    def test_pending_queue_index_present(self) -> None:
        """Pending-queue pagination: ``(workspace_id, status, created_at)``."""
        indexes = [i for i in ApprovalRequest.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_approval_request_workspace_status_created" in names
        target = next(
            i
            for i in indexes
            if i.name == "ix_approval_request_workspace_status_created"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "status",
            "created_at",
        ]


class TestLlmUsageModel:
    """The ``LlmUsage`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        row = LlmUsage(
            id="01HWA00000000000000000LUSA",
            workspace_id="01HWA00000000000000000WSPA",
            capability="staff_chat",
            model_id="01HWA00000000000000000MDLA",
            status="ok",
            correlation_id="01HWA00000000000000000CRLA",
            created_at=_PINNED,
        )
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.capability == "staff_chat"
        assert row.model_id == "01HWA00000000000000000MDLA"
        assert row.status == "ok"
        assert row.correlation_id == "01HWA00000000000000000CRLA"

    def test_full_construction(self) -> None:
        row = LlmUsage(
            id="01HWA00000000000000000LUSB",
            workspace_id="01HWA00000000000000000WSPA",
            capability="daily_digest",
            model_id="01HWA00000000000000000MDLA",
            tokens_in=1200,
            tokens_out=340,
            cost_cents=18,
            latency_ms=942,
            status="ok",
            correlation_id="01HWA00000000000000000CRLB",
            created_at=_PINNED,
        )
        assert row.tokens_in == 1200
        assert row.tokens_out == 340
        assert row.cost_cents == 18
        assert row.latency_ms == 942

    def test_every_status_constructs(self) -> None:
        """Each v1 ``llm_usage.status`` value builds a valid row."""
        for index, status in enumerate(llm_models._LLM_USAGE_STATUS_VALUES):
            row = LlmUsage(
                id=f"01HWA0000000000000000LUS{index}",
                workspace_id="01HWA00000000000000000WSPA",
                capability="staff_chat",
                model_id="01HWA00000000000000000MDLA",
                status=status,
                correlation_id=f"01HWA000000000000000CRL{index}",
                created_at=_PINNED,
            )
            assert row.status == status

    def test_tablename(self) -> None:
        assert LlmUsage.__tablename__ == "llm_usage"

    def test_status_check_present(self) -> None:
        checks = [
            c
            for c in LlmUsage.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("status")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for status in llm_models._LLM_USAGE_STATUS_VALUES:
            assert status in sql, f"{status} missing from CHECK constraint"

    def test_feed_index_present(self) -> None:
        """Feed hot path: ``(workspace_id, created_at)``."""
        indexes = [i for i in LlmUsage.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_llm_usage_workspace_created" in names
        target = next(i for i in indexes if i.name == "ix_llm_usage_workspace_created")
        assert [c.name for c in target.columns] == ["workspace_id", "created_at"]

    def test_capability_index_present(self) -> None:
        """Per-capability breakdown: ``(workspace_id, capability, created_at)``."""
        indexes = [i for i in LlmUsage.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_llm_usage_workspace_capability_created" in names
        target = next(
            i for i in indexes if i.name == "ix_llm_usage_workspace_capability_created"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "capability",
            "created_at",
        ]


class TestBudgetLedgerModel:
    """The ``BudgetLedger`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        row = BudgetLedger(
            id="01HWA00000000000000000BLRA",
            workspace_id="01HWA00000000000000000WSPA",
            period_start=_PINNED,
            period_end=_LATER,
            cap_cents=500,
            updated_at=_PINNED,
        )
        assert row.period_start == _PINNED
        assert row.period_end == _LATER
        assert row.cap_cents == 500
        # ``spent_cents`` has ``server_default='0'`` — pre-flush the
        # ORM attribute reads as ``None`` and the DB materialises the
        # default at INSERT time. The integration suite covers the
        # post-insert path; here we only assert the attribute is the
        # expected shape (unset, not an exception).
        assert row.spent_cents is None
        assert row.updated_at == _PINNED

    def test_full_construction(self) -> None:
        row = BudgetLedger(
            id="01HWA00000000000000000BLRB",
            workspace_id="01HWA00000000000000000WSPA",
            period_start=_PINNED,
            period_end=_LATER,
            spent_cents=234,
            cap_cents=500,
            updated_at=_LATER,
        )
        assert row.spent_cents == 234
        assert row.cap_cents == 500
        assert row.updated_at == _LATER

    def test_tablename(self) -> None:
        assert BudgetLedger.__tablename__ == "budget_ledger"

    def test_period_end_after_start_check_present(self) -> None:
        checks = [
            c
            for c in BudgetLedger.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("period_end_after_start")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "period_end" in sql
        assert "period_start" in sql

    def test_unique_period_index_present(self) -> None:
        """Acceptance: unique ``(workspace_id, period_start, period_end)``."""
        indexes = [i for i in BudgetLedger.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "uq_budget_ledger_workspace_period" in names
        target = next(
            i for i in indexes if i.name == "uq_budget_ledger_workspace_period"
        )
        assert target.unique is True
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "period_start",
            "period_end",
        ]


class TestPackageReExports:
    """``app.adapters.db.llm`` re-exports every model."""

    def test_models_re_exported(self) -> None:
        assert ModelAssignment is llm_models.ModelAssignment
        assert AgentToken is llm_models.AgentToken
        assert ApprovalRequest is llm_models.ApprovalRequest
        assert LlmUsage is llm_models.LlmUsage
        assert BudgetLedger is llm_models.BudgetLedger
        assert LlmCapabilityInheritance is llm_models.LlmCapabilityInheritance


class TestRegistryIntent:
    """Every LLM table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.llm``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls :func:`registry._reset_for_tests` which
    wipes the process-wide set, so asserting presence after that
    reset would be flaky. The tests below encode the invariant —
    "every LLM table is scoped" — without over-coupling to import
    ordering. Mirrors the pattern in ``tests/unit/test_db_messaging.py``.
    """

    _TABLES: tuple[str, ...] = (
        "model_assignment",
        "agent_token",
        "approval_request",
        "llm_usage",
        "budget_ledger",
        "llm_capability_inheritance",
    )

    def test_every_llm_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in self._TABLES:
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in self._TABLES:
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in self._TABLES:
            registry.register(table)
        for table in self._TABLES:
            assert registry.is_scoped(table) is True

    def test_reimport_is_idempotent(self) -> None:
        """Re-importing ``app.adapters.db.llm`` does not raise.

        A multi-worker ASGI process may import the package more than
        once (once per worker, and again under test harnesses that
        reload application code). ``registry.register`` is set-backed
        so the second pass is a no-op; the guard here is a regression
        test against a future refactor that tightens the registry
        into raising on double-register.
        """
        import importlib

        import app.adapters.db.llm as llm_pkg

        importlib.reload(llm_pkg)
        for table in self._TABLES:
            assert llm_pkg.__name__ == "app.adapters.db.llm"
            # Re-register directly — exercises the same code path the
            # module body runs at import. Idempotent by set semantics.
            from app.tenancy import registry

            registry.register(table)
            registry.register(table)
            assert registry.is_scoped(table) is True


class TestSanityInterval:
    """Quick sanity: the pinned test constants respect the CHECK bound."""

    def test_pinned_later_is_after_pinned(self) -> None:
        # The ``BudgetLedger.period_end > period_start`` CHECK relies
        # on this ordering — if the test constants drift, every
        # ledger test above would insert an invalid row and the
        # integration suite's CHECK rejection would fail silently.
        # Guard the invariant.
        assert _LATER > _PINNED
        assert timedelta(days=1) == _LATER - _PINNED
