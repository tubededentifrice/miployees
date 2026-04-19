"""Integration tests for :mod:`app.capabilities` against a real DB.

These hang off the session-scoped ``db_session`` fixture from
``tests/integration/conftest.py`` so migrations have already produced
the ``deployment_setting`` table. Pure-probe tests — FTS5 detection,
URL-based branches, frozen-dataclass invariants — live in
``tests/unit/test_capabilities.py``.

See ``docs/specs/01-architecture.md`` §"Capability registry" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.adapters.db.capabilities.models import DeploymentSetting
from app.capabilities import Capabilities, DeploymentSettings, Features

pytestmark = pytest.mark.integration


def _empty_capabilities() -> Capabilities:
    """Build a :class:`Capabilities` with default settings and dummy features."""
    features = Features(
        rls=False,
        fulltext_search=False,
        concurrent_writers=False,
        object_storage=False,
        wildcard_subdomains=False,
        email_bounce_webhooks=False,
        llm_voice_input=False,
    )
    return Capabilities(features=features, settings=DeploymentSettings())


def _add(
    session: Session,
    key: str,
    value: object,
    *,
    updated_by: str | None = None,
) -> None:
    session.add(
        DeploymentSetting(
            key=key,
            value=value,
            updated_at=datetime.now(UTC),
            updated_by=updated_by,
        )
    )
    session.flush()


class TestMigrationCreatesTable:
    def test_deployment_setting_table_exists(self, engine: Engine) -> None:
        """Migration 0001 (``deployment_setting``) landed at head."""
        assert "deployment_setting" in inspect(engine).get_table_names()

    def test_deployment_setting_columns_match_spec(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("deployment_setting")}
        assert set(cols) == {"key", "value", "updated_at", "updated_by"}
        assert cols["updated_by"]["nullable"] is True
        assert cols["value"]["nullable"] is False
        assert cols["updated_at"]["nullable"] is False
        pk = inspect(engine).get_pk_constraint("deployment_setting")
        assert pk["constrained_columns"] == ["key"]


class TestRefreshSettings:
    def test_signup_enabled_false_is_read(self, db_session: Session) -> None:
        _add(db_session, "signup_enabled", False)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is False

    def test_signup_enabled_true_roundtrip(self, db_session: Session) -> None:
        _add(db_session, "signup_enabled", True)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is True

    def test_llm_default_budget_updates(self, db_session: Session) -> None:
        _add(db_session, "llm_default_budget_cents_30d", 1500)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.llm_default_budget_cents_30d == 1500

    def test_require_passkey_attestation_updates(self, db_session: Session) -> None:
        _add(db_session, "require_passkey_attestation", True)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.require_passkey_attestation is True

    def test_signup_throttle_overrides_updates(self, db_session: Session) -> None:
        _add(db_session, "signup_throttle_overrides", {"per_email_per_day": 5})
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.signup_throttle_overrides == {"per_email_per_day": 5}

    def test_unknown_key_silently_ignored(self, db_session: Session) -> None:
        """A DB row with a key the app doesn't know about must not crash."""
        _add(db_session, "invented_future_toggle", "anything")
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        # Defaults untouched.
        assert caps.settings.signup_enabled is True
        assert caps.settings.llm_default_budget_cents_30d == 500

    def test_insert_then_update_then_refresh_sees_latest(
        self, db_session: Session
    ) -> None:
        """Refresh picks up the latest row value, not a cached one."""
        _add(db_session, "signup_enabled", True)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is True

        # Update in place through the ORM (admin settings path).
        row = db_session.get(DeploymentSetting, "signup_enabled")
        assert row is not None
        row.value = False
        row.updated_at = datetime.now(UTC)
        db_session.flush()

        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is False

    def test_refresh_with_no_rows_keeps_defaults(self, db_session: Session) -> None:
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is True
        assert caps.settings.signup_throttle_overrides == {}
        assert caps.settings.require_passkey_attestation is False
        assert caps.settings.llm_default_budget_cents_30d == 500

    def test_multiple_rows_all_applied(self, db_session: Session) -> None:
        _add(db_session, "signup_enabled", False)
        _add(db_session, "llm_default_budget_cents_30d", 2500)
        _add(db_session, "require_passkey_attestation", True)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is False
        assert caps.settings.llm_default_budget_cents_30d == 2500
        assert caps.settings.require_passkey_attestation is True

    def test_refresh_is_idempotent(self, db_session: Session) -> None:
        _add(db_session, "signup_enabled", False)
        caps = _empty_capabilities()
        caps.refresh_settings(db_session)
        caps.refresh_settings(db_session)
        assert caps.settings.signup_enabled is False

    def test_bad_payload_leaves_settings_unchanged(self, db_session: Session) -> None:
        """A single malformed row must not leave ``settings`` half-updated.

        ``signup_enabled`` gets a good payload and ``llm_default_budget_cents_30d``
        gets a non-numeric string — the second coercion raises, and we
        expect the earlier good payload NOT to have landed either, so
        every observer keeps seeing the pre-refresh values.
        """
        _add(db_session, "signup_enabled", False)
        _add(db_session, "llm_default_budget_cents_30d", "not-a-number")
        caps = _empty_capabilities()
        with pytest.raises((TypeError, ValueError)):
            caps.refresh_settings(db_session)
        # signup_enabled stayed at the default True — atomicity holds.
        assert caps.settings.signup_enabled is True
        assert caps.settings.llm_default_budget_cents_30d == 500


class TestProbeWithSession:
    """End-to-end: ``probe(settings, session)`` reads DB rows through."""

    def test_probe_with_session_applies_db_values(self, db_session: Session) -> None:
        from app.capabilities import probe
        from app.config import Settings

        _add(db_session, "signup_enabled", False)
        settings = Settings.model_construct(
            database_url="sqlite:///:memory:",
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
            storage_backend="localfs",
        )
        caps = probe(settings, session=db_session)
        assert caps.settings.signup_enabled is False
