"""Unit tests for :mod:`app.adapters.db.workspace.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__``. Integration
coverage (migrations, FK cascade, uniqueness violations, tenant
filter enforcement) lives in
``tests/integration/test_db_workspace.py``.

See ``docs/specs/02-domain-model.md`` §"workspaces" and
§"user_workspace".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index

from app.adapters.db.workspace.models import UserWorkspace, Workspace

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class TestWorkspaceModel:
    """The ``Workspace`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        ws = Workspace(
            id="01HWA00000000000000000WSPA",
            slug="villa-sud",
            name="Villa Sud",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        assert ws.id == "01HWA00000000000000000WSPA"
        assert ws.slug == "villa-sud"
        assert ws.name == "Villa Sud"
        assert ws.plan == "free"
        assert ws.quota_json == {}
        assert ws.created_at == _PINNED
        # ``owner_onboarded_at`` is nullable; defaults to ``None`` until
        # the first-run wizard completes.
        assert ws.owner_onboarded_at is None

    def test_owner_onboarded_at_can_be_set(self) -> None:
        ws = Workspace(
            id="01HWA00000000000000000WSPA",
            slug="villa-sud",
            name="Villa Sud",
            plan="free",
            quota_json={},
            created_at=_PINNED,
            owner_onboarded_at=_PINNED,
        )
        assert ws.owner_onboarded_at == _PINNED

    def test_quota_json_accepts_mapping_payload(self) -> None:
        """Cap payloads land verbatim — shape is caller-owned."""
        payload = {"users_max": 5, "properties_max": 1, "storage_bytes": 10_000}
        ws = Workspace(
            id="01HWA00000000000000000WSPA",
            slug="villa-sud",
            name="Villa Sud",
            plan="free",
            quota_json=payload,
            created_at=_PINNED,
        )
        assert ws.quota_json == payload

    def test_tablename(self) -> None:
        assert Workspace.__tablename__ == "workspace"

    def test_plan_check_constraint_present(self) -> None:
        """``__table_args__`` carries the plan CHECK constraint."""
        checks = [c for c in Workspace.__table_args__ if isinstance(c, CheckConstraint)]
        # cd-n6p added a second CHECK on ``default_currency`` shape; the
        # ``plan`` CHECK is the named one we look up by constraint name
        # so the count guard does not need to track every future addition.
        # Naming convention prefixes with ``ck_<table>_`` — keep the
        # match string in sync with :mod:`app.adapters.db.base`.
        plan_checks = [c for c in checks if c.name == "ck_workspace_plan"]
        assert len(plan_checks) == 1
        sql = str(plan_checks[0].sqltext)
        for plan in ("free", "pro", "enterprise", "unlimited"):
            assert plan in sql

    def test_default_currency_shape_check_present(self) -> None:
        """cd-n6p adds a ``LENGTH(default_currency) = 3`` CHECK."""
        checks = [c for c in Workspace.__table_args__ if isinstance(c, CheckConstraint)]
        currency_checks = [
            c for c in checks if c.name == "ck_workspace_default_currency_shape"
        ]
        assert len(currency_checks) == 1
        assert "default_currency" in str(currency_checks[0].sqltext)


class TestUserWorkspaceModel:
    """The ``UserWorkspace`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        link = UserWorkspace(
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            source="workspace_grant",
            added_at=_PINNED,
        )
        assert link.user_id == "01HWA00000000000000000USRA"
        assert link.workspace_id == "01HWA00000000000000000WSPA"
        assert link.source == "workspace_grant"
        assert link.added_at == _PINNED

    def test_tablename(self) -> None:
        assert UserWorkspace.__tablename__ == "user_workspace"

    def test_source_check_constraint_present(self) -> None:
        """``__table_args__`` carries the source CHECK constraint."""
        checks = [
            c for c in UserWorkspace.__table_args__ if isinstance(c, CheckConstraint)
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for source in (
            "workspace_grant",
            "property_grant",
            "org_grant",
            "work_engagement",
        ):
            assert source in sql

    def test_workspace_index_present(self) -> None:
        """A composite index on ``workspace_id`` is declared."""
        indexes = [i for i in UserWorkspace.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_user_workspace_workspace" in names
        target = next(i for i in indexes if i.name == "ix_user_workspace_workspace")
        assert [c.name for c in target.columns] == ["workspace_id"]
