"""Unit tests for :mod:`crewday._overrides.auth`.

Coverage maps to the cd-qnz3 acceptance criteria for ``auth login``:
happy-path write, ``--token`` vs ``--token-env`` storage, idempotent
re-write of the same profile, and the failure paths
(unreachable URL, invalid TOML in an existing config, mutually
exclusive flags). The tests stub the :func:`httpx.MockTransport`
layer so no real network call is made; the on-disk state is
redirected to a temp path through monkeypatching the module-level
config-file constants.
"""

from __future__ import annotations

import pathlib
import tomllib
from collections.abc import Callable

import httpx
import pytest
from click.testing import CliRunner
from crewday._main import ExitCode
from crewday._overrides import auth as auth_override


@pytest.fixture
def runner() -> CliRunner:
    """Fresh :class:`CliRunner`."""
    return CliRunner()


@pytest.fixture
def fake_config(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> pathlib.Path:
    """Redirect ``_CONFIG_FILE`` to a temp path for one test."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(auth_override, "_CONFIG_FILE", config_path)
    monkeypatch.setattr(auth_override, "_CONFIG_DIR", tmp_path)
    return config_path


def _patch_healthz(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Replace :func:`_ping_healthz` so its httpx.Client uses ``handler``."""
    original = auth_override._ping_healthz

    def patched(base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        return original(
            base_url,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(auth_override, "_ping_healthz", patched)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """Always 200; used by the happy-path tests."""
    assert request.url.path == "/healthz"
    return httpx.Response(200, json={"status": "ok"})


def _bad_handler(request: httpx.Request) -> httpx.Response:
    """Always 503; used by the failing-probe tests."""
    return httpx.Response(503, json={"status": "down"})


def test_login_happy_path_with_token_writes_profile(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auth login --token` writes the profile and reports success."""
    _patch_healthz(monkeypatch, _ok_handler)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:8100",
            "--token",
            "secret-abc",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Wrote profile 'dev'" in result.output
    assert fake_config.is_file()

    parsed = tomllib.loads(fake_config.read_text(encoding="utf-8"))
    assert parsed["default_profile"] == "dev"
    profile = parsed["profile"]["dev"]
    assert profile["base_url"] == "http://127.0.0.1:8100"
    assert profile["token"] == "secret-abc"


def test_login_token_env_stores_env_prefix(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--token-env VAR` writes ``token = "env:VAR"``, never the secret."""
    _patch_healthz(monkeypatch, _ok_handler)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "prod",
            "--base-url",
            "https://ops.example.com",
            "--token-env",
            "CREWDAY_TOKEN_PROD",
        ],
    )
    assert result.exit_code == 0, result.output

    parsed = tomllib.loads(fake_config.read_text(encoding="utf-8"))
    assert parsed["profile"]["prod"]["token"] == "env:CREWDAY_TOKEN_PROD"


def test_login_token_and_token_env_are_mutually_exclusive(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing both --token and --token-env is a UsageError."""
    _patch_healthz(monkeypatch, _ok_handler)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:8100",
            "--token",
            "secret",
            "--token-env",
            "FOO",
        ],
    )
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "mutually exclusive" in combined.lower()


def test_login_failed_healthz_exits_5_without_writing(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx /healthz aborts with ConfigError (exit 5)."""
    _patch_healthz(monkeypatch, _bad_handler)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:8100",
            "--token",
            "secret",
        ],
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR
    combined = (result.output or "") + (result.stderr or "")
    assert "/healthz" in combined.lower() or "503" in combined
    # On-disk state is untouched on failure.
    assert not fake_config.exists()


def test_login_unreachable_base_url_exits_5(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-level error aborts with ConfigError (exit 5)."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_healthz(monkeypatch, boom)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:9999",
            "--token",
            "secret",
        ],
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert not fake_config.exists()


def test_login_idempotent_rewrite_produces_same_bytes(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running `auth login` twice with the same args yields identical bytes."""
    _patch_healthz(monkeypatch, _ok_handler)

    args = [
        "--profile",
        "dev",
        "--base-url",
        "http://127.0.0.1:8100",
        "--token",
        "secret-abc",
    ]
    first = runner.invoke(auth_override.login, args)
    assert first.exit_code == 0, first.output
    bytes_after_first = fake_config.read_bytes()

    second = runner.invoke(auth_override.login, args)
    assert second.exit_code == 0, second.output
    bytes_after_second = fake_config.read_bytes()

    assert bytes_after_first == bytes_after_second


def test_login_preserves_other_profiles(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing 'dev' must not erase a pre-existing 'prod' profile."""
    fake_config.parent.mkdir(parents=True, exist_ok=True)
    fake_config.write_text(
        'default_profile = "prod"\n\n'
        "[profile.prod]\n"
        'base_url = "https://ops.example.com"\n'
        'token = "env:CREWDAY_TOKEN_PROD"\n',
        encoding="utf-8",
    )
    _patch_healthz(monkeypatch, _ok_handler)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:8100",
            "--token",
            "dev-secret",
        ],
    )
    assert result.exit_code == 0, result.output

    parsed = tomllib.loads(fake_config.read_text(encoding="utf-8"))
    # Both profiles present.
    assert set(parsed["profile"].keys()) == {"prod", "dev"}
    # default_profile unchanged on a second-profile add.
    assert parsed["default_profile"] == "prod"
    assert parsed["profile"]["prod"]["token"] == "env:CREWDAY_TOKEN_PROD"
    assert parsed["profile"]["dev"]["token"] == "dev-secret"


def test_login_corrupt_existing_config_aborts_cleanly(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable config.toml is a hard error, never silently overwritten."""
    fake_config.parent.mkdir(parents=True, exist_ok=True)
    fake_config.write_text("[unterminated table\n", encoding="utf-8")
    original_bytes = fake_config.read_bytes()

    _patch_healthz(monkeypatch, _ok_handler)

    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:8100",
            "--token",
            "secret",
        ],
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR
    # File untouched — the operator's other profiles must survive a
    # corrupt-config landmine.
    assert fake_config.read_bytes() == original_bytes


def test_login_metadata_attached_for_parity_gate() -> None:
    """The decorator stamps ``_cli_override`` so the parity gate sees the override."""
    metadata = getattr(auth_override.login, "_cli_override", None)
    assert metadata is not None
    group, verb, covers = metadata
    assert group == "auth"
    assert verb == "login"
    # ``auth login`` has no API analogue per the override contract.
    assert covers == ()


def test_login_help_renders(runner: CliRunner) -> None:
    """`auth login --help` should render without crashing."""
    result = runner.invoke(auth_override.login, ["--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--base-url" in result.output
    assert "--token" in result.output
    assert "--token-env" in result.output


def test_login_token_value_with_quotes_is_escaped_in_toml(
    runner: CliRunner,
    fake_config: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tokens carrying ``"`` round-trip through tomllib unchanged."""
    _patch_healthz(monkeypatch, _ok_handler)

    raw_token = 'odd"token\\with-escapes'
    result = runner.invoke(
        auth_override.login,
        [
            "--profile",
            "dev",
            "--base-url",
            "http://127.0.0.1:8100",
            "--token",
            raw_token,
        ],
    )
    assert result.exit_code == 0, result.output

    parsed = tomllib.loads(fake_config.read_text(encoding="utf-8"))
    assert parsed["profile"]["dev"]["token"] == raw_token
