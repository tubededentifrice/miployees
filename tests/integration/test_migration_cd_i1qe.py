"""Migration smoke: cd-i1qe token-kinds columns round-trip cleanly.

Runs ``alembic upgrade head`` against a scratch SQLite file, confirms
the three added columns land on the ``api_token`` table with the
right nullability, then ``downgrade -1`` and re-``upgrade head`` to
prove the revision is reversible and idempotent.

A full-suite migration-parity test already lives in
:mod:`tests.integration.test_schema_parity` (SQLite vs Postgres
structural fingerprint). This module narrows the scope to cd-i1qe
so a future breaking change on the revision surface fails with a
message that points straight at the right migration instead of
requiring a diff on the parity snapshot.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from app.adapters.db.session import make_engine
from app.config import get_settings

pytestmark = pytest.mark.integration


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
    """Temporarily point ``app.config.get_settings`` at ``url``."""
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


_REVISION_ID: str = "f7c9e1a4b5d8"
_PREVIOUS_REVISION_ID: str = "e6f8c0b4a2d7"


class TestTokenKindsMigration:
    """cd-i1qe migration adds the three columns and is reversible."""

    def test_upgrade_adds_kind_and_fk_columns(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``alembic upgrade head`` lands the three cd-i1qe columns on
        ``api_token`` with the right nullability."""
        db_path = tmp_path_factory.mktemp("cd-i1qe-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            insp = inspect(engine)
            columns = {c["name"]: c for c in insp.get_columns("api_token")}
            # Three new columns landed.
            assert "kind" in columns
            assert "delegate_for_user_id" in columns
            assert "subject_user_id" in columns
            # Nullability:
            # * ``kind`` NOT NULL (has server default 'scoped')
            # * ``delegate_for_user_id`` NULL
            # * ``subject_user_id`` NULL
            # * ``workspace_id`` widened to NULL.
            assert columns["kind"]["nullable"] is False
            assert columns["delegate_for_user_id"]["nullable"] is True
            assert columns["subject_user_id"]["nullable"] is True
            assert columns["workspace_id"]["nullable"] is True
        finally:
            engine.dispose()

    def test_downgrade_removes_kind_and_fk_columns(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``alembic downgrade -1`` from the cd-i1qe head drops the
        three columns and narrows ``workspace_id`` back to NOT NULL."""
        db_path = tmp_path_factory.mktemp("cd-i1qe-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            insp = inspect(engine)
            columns = {c["name"]: c for c in insp.get_columns("api_token")}
            assert "kind" not in columns
            assert "delegate_for_user_id" not in columns
            assert "subject_user_id" not in columns
            assert columns["workspace_id"]["nullable"] is False
        finally:
            engine.dispose()

    def test_upgrade_after_downgrade_is_idempotent(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``upgrade head`` → ``downgrade -1`` → ``upgrade head`` cycle is clean."""
        db_path = tmp_path_factory.mktemp("cd-i1qe-mig-cycle") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)
                command.upgrade(cfg, _REVISION_ID)

            insp = inspect(engine)
            columns = {c["name"]: c for c in insp.get_columns("api_token")}
            assert "kind" in columns
            assert "delegate_for_user_id" in columns
            assert "subject_user_id" in columns
        finally:
            engine.dispose()

    def test_downgrade_purges_personal_rows_preserves_scoped(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Downgrade deletes PAT rows (NULL workspace_id) and keeps scoped rows.

        Without the pre-downgrade ``DELETE FROM api_token WHERE
        kind = 'personal'`` step the ``ALTER COLUMN workspace_id SET
        NOT NULL`` would fail (SQLite's batch table-copy refuses the
        NULL; Postgres rejects the constraint change). Scoped rows
        with a populated workspace_id must survive the rollback.
        """
        from sqlalchemy import text

        from app.adapters.db.base import Base

        db_path = tmp_path_factory.mktemp("cd-i1qe-mig-downgrade-data") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            # Seed: one user, one workspace, one scoped token
            # (workspace-pinned) + one PAT (workspace_id NULL). The
            # Base.metadata shape is what the cd-i1qe head ships, so
            # both inserts are legal at this point.
            with engine.begin() as conn:
                # We use raw SQL rather than the ORM so the test does
                # not need the workspace/permission factories; the
                # FK-free columns are all string / JSON.
                conn.execute(
                    text(
                        "INSERT INTO user "
                        "(id, email, email_lower, display_name, created_at) "
                        "VALUES ('01HWA00000000000000000USER', 'u@e.co', "
                        "'u@e.co', 'U', '2026-04-24T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO workspace "
                        "(id, slug, name, plan, quota_json, created_at) "
                        "VALUES ('01HWA00000000000000000WKSP', 'w', 'W', "
                        "'free', '{}', '2026-04-24T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO api_token "
                        "(id, user_id, workspace_id, kind, "
                        "delegate_for_user_id, subject_user_id, label, "
                        "scope_json, prefix, hash, created_at) VALUES "
                        "('01HWA00000000000000000SCTK', "
                        "'01HWA00000000000000000USER', "
                        "'01HWA00000000000000000WKSP', 'scoped', NULL, NULL, "
                        "'scoped-tok', '{}', 'pre_sc12', 'h_scoped', "
                        "'2026-04-24T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO api_token "
                        "(id, user_id, workspace_id, kind, "
                        "delegate_for_user_id, subject_user_id, label, "
                        "scope_json, prefix, hash, created_at) VALUES "
                        "('01HWA00000000000000000PATK', "
                        "'01HWA00000000000000000USER', NULL, 'personal', "
                        "NULL, '01HWA00000000000000000USER', "
                        "'pat-tok', '{\"me.tasks:read\": true}', 'pre_pa12', "
                        "'h_pat', '2026-04-24T12:00:00+00:00')"
                    )
                )

            # Downgrade must NOT fail on the PAT row's NULL workspace_id.
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            # Scoped row survives; PAT row is gone.
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id FROM api_token ORDER BY id")
                ).fetchall()
            assert [r[0] for r in rows] == ["01HWA00000000000000000SCTK"]

            # Re-upgrade so the test leaves the DB at head for Base
            # metadata cleanup semantics.
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            # Belt-and-braces: the Base metadata still knows the
            # post-upgrade shape (nothing unexpected leaked).
            assert "api_token" in Base.metadata.tables
        finally:
            engine.dispose()
