"""Pure-probe unit tests for :mod:`app.capabilities`.

The DB-backed tests (``refresh_settings`` against real
``deployment_setting`` rows, migration round-trips) live in
``tests/integration/test_capabilities.py``; anything that only
exercises :func:`_probe_features` or dataclass invariants stays here
so it runs without the alembic harness.

See ``docs/specs/01-architecture.md`` §"Capability registry".
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Literal

import pytest

from app.capabilities import (
    Capabilities,
    DeploymentSettings,
    _probe_features,
    _sqlite_has_fts5,
    probe,
)
from app.config import Settings


def _sqlite_settings(
    *,
    database_url: str = "sqlite:///:memory:",
    storage_backend: Literal["localfs", "s3"] = "localfs",
) -> Settings:
    """Build a :class:`Settings` pinned to SQLite with selective overrides.

    ``model_construct`` bypasses env loading so host vars can't leak
    into the probe — tests own every knob that matters. Only the two
    fields the probe reads are parameterised; everything else gets
    the static defaults the real :class:`Settings` would emit.
    """
    return Settings.model_construct(
        database_url=database_url,
        bind_host="127.0.0.1",
        bind_port=8000,
        trusted_interfaces=["tailscale*"],
        allow_public_bind=False,
        data_dir=Path("."),
        public_url=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        openrouter_api_key=None,
        root_key=None,
        demo_mode=False,
        worker="internal",
        storage_backend=storage_backend,
    )


class TestProbeFeaturesSqlite:
    def test_sqlite_url_disables_rls_and_concurrent_writers(self) -> None:
        features = _probe_features(_sqlite_settings())
        assert features.rls is False
        assert features.concurrent_writers is False

    def test_sqlite_fulltext_matches_interpreter_build(self) -> None:
        """``fulltext_search`` must mirror the live sqlite3 FTS5 probe.

        We don't hard-code ``True``: the CPython build used in CI
        has FTS5, but an Alpine python without it must report False.
        Consistency between the public probe and the inner helper
        is what matters.
        """
        features = _probe_features(_sqlite_settings())
        assert features.fulltext_search is _sqlite_has_fts5()

    def test_sqlite_fulltext_false_when_fts5_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the live sqlite lacks FTS5, ``fulltext_search`` is False."""
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: False)
        features = _probe_features(_sqlite_settings())
        assert features.fulltext_search is False

    def test_sqlite_fulltext_true_when_fts5_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: True)
        features = _probe_features(_sqlite_settings())
        assert features.fulltext_search is True


class TestProbeFeaturesPostgres:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@localhost/db",
            "postgres://u:p@localhost/db",
            "postgresql+psycopg://u:p@localhost/db",
            "postgresql+asyncpg://u:p@localhost/db",
            "postgres+psycopg://u:p@localhost/db",
            # Mixed-case should still be matched — the probe lowercases.
            "PostgreSQL://u:p@localhost/db",
        ],
    )
    def test_postgres_urls_enable_rls_concurrent_writers_and_fts(
        self, url: str
    ) -> None:
        features = _probe_features(_sqlite_settings(database_url=url))
        assert features.rls is True
        assert features.concurrent_writers is True
        assert features.fulltext_search is True


class TestProbeFeaturesUnknownBackend:
    """Unknown DB URLs (mysql, oracle, …) must not borrow SQLite FTS5."""

    def test_mysql_url_leaves_fulltext_search_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FTS5 on the interpreter's SQLite says nothing about a mysql DB.

        Pin the inner probe to ``True`` so the test catches any code
        path that silently lets SQLite's FTS5 leak across to an
        unrelated backend.
        """
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: True)
        features = _probe_features(_sqlite_settings(database_url="mysql://u:p@h/db"))
        assert features.rls is False
        assert features.concurrent_writers is False
        assert features.fulltext_search is False

    def test_oracle_url_leaves_fulltext_search_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: True)
        features = _probe_features(_sqlite_settings(database_url="oracle://u:p@h/db"))
        assert features.fulltext_search is False


class TestProbeFeaturesStorage:
    def test_s3_storage_enables_object_storage(self) -> None:
        features = _probe_features(_sqlite_settings(storage_backend="s3"))
        assert features.object_storage is True

    def test_localfs_storage_disables_object_storage(self) -> None:
        features = _probe_features(_sqlite_settings(storage_backend="localfs"))
        assert features.object_storage is False


class TestProbeFeaturesStubs:
    """The three v1-stubbed fields stay False regardless of settings."""

    def test_stubs_are_false_on_sqlite(self) -> None:
        features = _probe_features(_sqlite_settings())
        assert features.wildcard_subdomains is False
        assert features.email_bounce_webhooks is False
        assert features.llm_voice_input is False

    def test_stubs_are_false_on_postgres(self) -> None:
        features = _probe_features(
            _sqlite_settings(database_url="postgresql://u:p@h/db")
        )
        assert features.wildcard_subdomains is False
        assert features.email_bounce_webhooks is False
        assert features.llm_voice_input is False


class TestProbeWithoutSession:
    def test_defaults_applied_when_session_none(self) -> None:
        caps = probe(_sqlite_settings(), session=None)
        assert isinstance(caps, Capabilities)
        assert caps.settings.signup_enabled is True
        assert caps.settings.signup_throttle_overrides == {}
        assert caps.settings.require_passkey_attestation is False
        assert caps.settings.llm_default_budget_cents_30d == 500

    def test_features_populated_when_session_none(self) -> None:
        caps = probe(_sqlite_settings(storage_backend="s3"), session=None)
        assert caps.features.object_storage is True
        assert caps.features.rls is False


class TestProbeLogsSnapshotOnce:
    def test_boot_log_is_emitted_exactly_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One INFO snapshot line per :func:`probe` call.

        ``caplog`` attaches its handler to the root logger, so we also
        force ``propagate=True`` on the capabilities logger: the
        integration fixture's ``alembic upgrade head`` path runs
        ``logging.config.fileConfig`` which can leave non-listed
        loggers with ``propagate`` cleared. Without the force, the
        INFO line reaches the capabilities logger but never the root,
        and the capture is empty.
        """
        caps_logger = logging.getLogger("app.capabilities")
        caps_logger.propagate = True
        # ``logging.config.fileConfig`` (used by alembic.ini during
        # integration test setup) defaults to
        # ``disable_existing_loggers=True``, which flips
        # :attr:`logging.Logger.disabled` on loggers that aren't in the
        # config file. Re-enable explicitly so this test is stable
        # regardless of whether the alembic fixture ran earlier.
        caps_logger.disabled = False
        caplog.set_level(logging.INFO, logger="app.capabilities")
        probe(_sqlite_settings(), session=None)
        snapshot_lines = [
            record
            for record in caplog.records
            if record.name == "app.capabilities"
            and "capabilities snapshot" in record.getMessage()
        ]
        assert len(snapshot_lines) == 1
        assert snapshot_lines[0].levelno == logging.INFO


class TestFrozenFeatures:
    def test_mutating_rls_raises_frozen_instance_error(self) -> None:
        """Frozen dataclass: direct assignment to ``rls`` must raise.

        The field name is chosen at runtime from
        :func:`dataclasses.fields` so mypy's strict read-only check
        doesn't fire on a known field name — ruff B010 is happy
        because the attribute name isn't a hard-coded constant, and
        the hard rules stay clean of ``# type: ignore``.
        """
        features = _probe_features(_sqlite_settings())
        field_name = dataclasses.fields(features)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(features, field_name, True)

    def test_mutating_any_field_raises_frozen_instance_error(self) -> None:
        """Every declared field must be immutable, not just ``rls``."""
        features = _probe_features(_sqlite_settings())
        for name in (f.name for f in dataclasses.fields(features)):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(features, name, True)


class TestDeploymentSettingsIsMutable:
    def test_settings_instance_accepts_direct_assignment(self) -> None:
        """``DeploymentSettings`` is deliberately NOT frozen.

        :meth:`Capabilities.refresh_settings` re-points fields in
        place so callers holding a reference to :class:`Capabilities`
        observe new values without a re-lookup.
        """
        settings = DeploymentSettings()
        settings.signup_enabled = False
        assert settings.signup_enabled is False


class TestSqliteFts5Probe:
    def test_direct_probe_returns_bool(self) -> None:
        """Sanity check: the probe always returns a bool, never raises."""
        result = _sqlite_has_fts5()
        assert isinstance(result, bool)
