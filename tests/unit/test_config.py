"""Tests for :mod:`app.config`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings, get_settings

if TYPE_CHECKING:
    from pytest import MonkeyPatch

# Env vars this module touches. Derived from ``Settings.model_fields`` so
# new fields automatically join the cleanup set — stops a future author
# from forgetting to strip a host-shell value before a test runs.
_CREWDAY_VARS: tuple[str, ...] = tuple(
    f"CREWDAY_{name.upper()}" for name in Settings.model_fields
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate each test from the host env and any repo-root ``.env``.

    - Strips every ``CREWDAY_*`` var so the baseline is "unset".
    - ``chdir`` to a temp directory so pydantic-settings doesn't pick
      up a stray ``.env`` from the repo root mid-suite.
    - Clears ``get_settings``' cache so every test builds fresh.
    """
    for name in _CREWDAY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


class TestDefaults:
    def test_defaults_when_only_required_set(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        s = Settings()
        assert s.database_url == "sqlite:///:memory:"
        assert s.bind_host == "127.0.0.1"
        assert s.bind_port == 8000
        assert s.trusted_interfaces == ["tailscale*"]
        assert s.allow_public_bind is False
        assert s.data_dir == Path("./data")
        assert s.public_url is None
        assert s.smtp_host is None
        assert s.smtp_port == 587
        assert s.smtp_user is None
        assert s.smtp_password is None
        assert s.smtp_from is None
        assert s.smtp_use_tls is True
        assert s.smtp_timeout == 10
        assert s.smtp_bounce_domain is None
        assert s.openrouter_api_key is None
        assert s.root_key is None
        assert s.demo_mode is False
        assert s.worker == "internal"
        assert s.storage_backend == "localfs"


class TestEnvOverride:
    def test_env_overrides_propagate(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_BIND_HOST", "100.1.2.3")
        monkeypatch.setenv("CREWDAY_BIND_PORT", "9000")
        monkeypatch.setenv("CREWDAY_DEMO_MODE", "1")
        monkeypatch.setenv("CREWDAY_WORKER", "external")
        monkeypatch.setenv("CREWDAY_STORAGE_BACKEND", "s3")
        s = Settings()
        assert s.bind_host == "100.1.2.3"
        assert s.bind_port == 9000
        assert s.demo_mode is True
        assert s.worker == "external"
        assert s.storage_backend == "s3"

    def test_invalid_worker_literal_raises(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_WORKER", "bogus")
        with pytest.raises(ValidationError):
            Settings()

    def test_invalid_storage_backend_raises(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_STORAGE_BACKEND", "gcs")
        with pytest.raises(ValidationError):
            Settings()


class TestMissingRequired:
    def test_missing_database_url_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Settings()
        assert "database_url" in str(excinfo.value).lower()


class TestTrustedInterfacesParsing:
    def test_comma_separated_env_splits(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_TRUSTED_INTERFACES", "tailscale*,wg*")
        s = Settings()
        assert s.trusted_interfaces == ["tailscale*", "wg*"]

    def test_single_value_still_becomes_list(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_TRUSTED_INTERFACES", "wg0")
        s = Settings()
        assert s.trusted_interfaces == ["wg0"]

    def test_trailing_comma_drops_empty(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_TRUSTED_INTERFACES", "tailscale*, wg*,")
        s = Settings()
        assert s.trusted_interfaces == ["tailscale*", "wg*"]

    def test_unset_falls_back_to_default(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        s = Settings()
        assert s.trusted_interfaces == ["tailscale*"]


class TestSecretRedaction:
    def test_safe_dump_masks_populated_secrets(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_SMTP_PASSWORD", "hunter2")
        monkeypatch.setenv("CREWDAY_OPENROUTER_API_KEY", "sk-test-abc")
        monkeypatch.setenv("CREWDAY_ROOT_KEY", "root-secret")
        dump = Settings().safe_dump()
        assert dump["smtp_password"] == "***"
        assert dump["openrouter_api_key"] == "***"
        assert dump["root_key"] == "***"

    def test_safe_dump_passes_through_non_secret(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("CREWDAY_TRUSTED_INTERFACES", "tailscale*,wg*")
        dump = Settings().safe_dump()
        assert dump["database_url"] == "sqlite:///:memory:"
        assert dump["bind_host"] == "127.0.0.1"
        assert dump["trusted_interfaces"] == ["tailscale*", "wg*"]
        assert dump["worker"] == "internal"

    def test_safe_dump_none_for_unset_secrets(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        dump = Settings().safe_dump()
        assert dump["smtp_password"] is None
        assert dump["openrouter_api_key"] is None
        assert dump["root_key"] is None

    def test_repr_never_leaks_secrets(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_ROOT_KEY", "super-sensitive-value")
        monkeypatch.setenv("CREWDAY_OPENROUTER_API_KEY", "sk-secret")
        monkeypatch.setenv("CREWDAY_SMTP_PASSWORD", "p@ssw0rd")
        s = Settings()
        text = repr(s)
        assert "super-sensitive-value" not in text
        assert "sk-secret" not in text
        assert "p@ssw0rd" not in text

    def test_secret_values_still_accessible(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_ROOT_KEY", "abc123")
        s = Settings()
        assert isinstance(s.root_key, SecretStr)
        assert s.root_key.get_secret_value() == "abc123"


class TestGetSettingsCache:
    def test_returns_same_instance_on_repeated_calls(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_cache_clear_reloads_env(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_BIND_PORT", "8000")
        assert get_settings().bind_port == 8000
        monkeypatch.setenv("CREWDAY_BIND_PORT", "9999")
        # Without clearing, the cached value wins.
        assert get_settings().bind_port == 8000
        get_settings.cache_clear()
        assert get_settings().bind_port == 9999


class TestModuleLevelSettings:
    def test_module_attr_settings_returns_get_settings(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """``from app.config import settings`` must resolve lazily."""
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        import app.config as config_module

        # Accessing the proxy invokes ``__getattr__`` → ``get_settings()``.
        assert config_module.settings is get_settings()

    def test_unknown_module_attr_raises(self) -> None:
        import app.config as config_module

        with pytest.raises(AttributeError):
            config_module.does_not_exist  # noqa: B018
