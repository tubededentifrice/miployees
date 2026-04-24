"""Smoke tests for the ``crewday`` Click entry point.

Covers the scaffolding contract from Beads ``cd-j202``:

* ``crewday --help`` renders the root help with every global flag
  documented in §13 "Global flags" of ``docs/specs/13-cli.md``.
* ``crewday --version`` prints a version string (the packaged
  version, or the ``0.0.0+unknown`` fallback for virtual /
  editable-checkout installs).
* :class:`~crewday._globals.CrewdayContext` exposes the documented
  fields; importing the module by name works end-to-end.
* The Click group is present and invokable without any subcommand
  registered — the OpenAPI-driven command tree (``cd-1cfg``) wires
  the real verbs in later.

Tests drive the CLI through :class:`click.testing.CliRunner` — the
same harness the codegen tests (cd-1cfg) and override tests (tasks,
expenses, auth) will use, so assertion style stays uniform across
the suite.
"""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner
from crewday import _globals
from crewday._globals import (
    DEFAULT_OUTPUT,
    OUTPUT_CHOICES,
    CrewdayContext,
    default_idempotency_key_factory,
)
from crewday._main import (
    ApprovalPending,
    ConfigError,
    CrewdayError,
    ExitCode,
    RateLimited,
    ServerError,
    handle_errors,
    main,
    root,
)


@pytest.fixture
def runner() -> CliRunner:
    """Fresh :class:`CliRunner`.

    Click 8.2+ already keeps stdout and stderr separate on
    :class:`~click.testing.Result`; the older ``mix_stderr=False``
    knob was retired (see Click changelog 8.2 "Removed the
    mix_stderr argument"). Tests that care about stderr specifically
    read ``result.stderr``; tests that care about exit codes read
    ``result.exit_code``.
    """
    return CliRunner()


def test_help_lists_global_flags(runner: CliRunner) -> None:
    """``crewday --help`` must expose --profile, --workspace, --output."""
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output

    help_text = result.output
    for flag in ("--profile", "--workspace", "--output"):
        assert flag in help_text, f"missing global flag {flag!r} in --help"

    # Verbose flag is also a spec requirement (§13 "Global flags" row).
    assert "--verbose" in help_text
    # Version flag via -V/--version pair (short + long both shown).
    assert "--version" in help_text

    # The help header should mention the CLI's role so new users get
    # a single-sentence anchor on invocation.
    assert "crew.day" in help_text


def test_help_is_sorted_for_agent_discovery(runner: CliRunner) -> None:
    """Help is the agent's discovery surface (§13 "Discoverability for
    agents"). Verify it is non-empty and does not error — detail tests
    are deferred to the codegen task once commands register."""
    result = runner.invoke(root, ["-h"])
    assert result.exit_code == 0
    # The short alias (-h) must surface the same content as --help so
    # agents that default to -h don't see a different subset.
    assert "--profile" in result.output


def test_version_flag_prints_version(runner: CliRunner) -> None:
    """``crewday --version`` prints ``crewday <version>`` then exits 0."""
    result = runner.invoke(root, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("crewday "), result.output
    # Either the packaged version, or the editable-checkout fallback.
    # Both are valid strings; we only need to confirm a version is
    # present, not pin its exact value (that would couple the test to
    # the release cadence).
    assert len(result.output.strip()) > len("crewday ")


def test_version_short_flag(runner: CliRunner) -> None:
    """The ``-V`` short form matches ``--version``."""
    result = runner.invoke(root, ["-V"])
    assert result.exit_code == 0
    assert result.output.startswith("crewday ")


def test_main_is_importable_and_callable() -> None:
    """Entry-point contract from ``[project.scripts] crewday``."""
    # ``main`` must exist, be a callable, and wrap the root group
    # through ``@handle_errors`` (so :mod:`functools.wraps` has
    # copied the inner function's name onto the wrapper).
    assert callable(main)
    assert main.__name__ == "main"
    # ``functools.wraps`` sets ``__wrapped__`` on the decorator's
    # output; use getattr so mypy --strict doesn't need a type
    # ignore for an attribute it cannot see on ``Callable``.
    wrapped = getattr(main, "__wrapped__", None)
    assert wrapped is not None, "handle_errors did not wrap the inner fn"


def test_crewday_context_fields() -> None:
    """``CrewdayContext`` must carry profile / workspace / output plus
    the idempotency-key factory and logger required by §13."""
    ctx = CrewdayContext(
        profile="dev",
        workspace="smoke",
        output="json",
    )
    assert ctx.profile == "dev"
    assert ctx.workspace == "smoke"
    assert ctx.output == "json"

    # Idempotency factory: defaults to a ULID generator (§12
    # "Idempotency"); each call yields a fresh value and the values
    # are lexicographically sortable.
    key_1 = ctx.idempotency_key_factory()
    key_2 = ctx.idempotency_key_factory()
    assert key_1 != key_2
    assert len(key_1) == 26  # ULID canonical length
    assert key_1 < key_2  # ULIDs are strictly monotonic (app.util.ulid)

    # Logger scoped to 'crewday' so downstream children inherit.
    assert isinstance(ctx.logger, logging.Logger)
    assert ctx.logger.name == "crewday"


def test_crewday_context_is_frozen() -> None:
    """The context is immutable per-invocation — the dataclass is
    ``frozen=True`` so a command cannot accidentally swap the
    workspace slug mid-call."""
    from dataclasses import FrozenInstanceError

    ctx = CrewdayContext(profile=None, workspace=None, output="json")
    with pytest.raises(FrozenInstanceError):
        ctx.workspace = "other"  # type: ignore[misc]


def test_default_idempotency_key_factory_returns_ulid() -> None:
    """Sanity-check the factory matches :data:`CrewdayContext`'s default."""
    key = default_idempotency_key_factory()
    assert isinstance(key, str)
    assert len(key) == 26


def test_output_choices_cover_spec() -> None:
    """The four modes from §13 "Output" are present; default is json."""
    assert set(OUTPUT_CHOICES) == {"json", "yaml", "table", "ndjson"}
    assert DEFAULT_OUTPUT == "json"


def test_output_flag_accepts_each_mode(runner: CliRunner) -> None:
    """Every spec output mode is a valid ``-o`` argument. Since no
    subcommand is registered yet, Click prints the help and exits 0
    when invoked with no verb — we verify the option parses without
    raising ``UsageError``."""
    for mode in OUTPUT_CHOICES:
        result = runner.invoke(root, ["-o", mode, "--help"])
        assert result.exit_code == 0, f"{mode=} failed: {result.output}"


def test_output_flag_rejects_unknown(runner: CliRunner) -> None:
    """Unknown modes trip Click's own ``UsageError`` (exit 2).

    Note: ``--help`` short-circuits option validation, so the flag
    combination ``-o csv --help`` would succeed. We invoke without
    ``--help`` to exercise the choice check itself.
    """
    result = runner.invoke(root, ["-o", "csv"])
    assert result.exit_code == 2
    assert "csv" in (result.stderr or result.output)


def test_exit_code_constants_match_spec() -> None:
    """Spec numbers from §13 "Exit codes" (0..5) stay stable."""
    assert ExitCode.SUCCESS == 0
    assert ExitCode.CLIENT_ERROR == 1
    assert ExitCode.SERVER_ERROR == 2
    assert ExitCode.APPROVAL_PENDING == 3
    assert ExitCode.RATE_LIMITED == 4
    assert ExitCode.CONFIG_ERROR == 5


def test_crewday_error_subclasses_use_spec_exit_codes() -> None:
    """Each declared error maps to its §13 exit-code slot."""
    assert CrewdayError("x").exit_code == ExitCode.CLIENT_ERROR
    assert ConfigError("x").exit_code == ExitCode.CONFIG_ERROR
    assert ServerError("x").exit_code == ExitCode.SERVER_ERROR
    assert ApprovalPending("x").exit_code == ExitCode.APPROVAL_PENDING
    assert RateLimited("x").exit_code == ExitCode.RATE_LIMITED


def test_handle_errors_suppresses_traceback_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unhandled exceptions must not leak tracebacks to stderr at
    the default log level — the traceback frequently contains PII
    (exception messages built from user input, ORM row values).
    §15 "Security & privacy" requires the operator to opt in via
    ``--verbose`` before that data surfaces.

    Behaviour contract:
      * WARNING record names the exception class only (safe).
      * DEBUG record carries the traceback (only shown under
        ``--verbose`` which bumps the ``crewday`` logger to DEBUG).
      * stderr line is a fixed, PII-free string pointing at
        ``--verbose``.
    """

    @handle_errors
    def blows_up() -> None:
        raise RuntimeError("secret PII: user@example.com token=hunter2")

    caplog.set_level(logging.DEBUG, logger="crewday")
    with pytest.raises(SystemExit) as exc_info:
        blows_up()
    assert exc_info.value.code == ExitCode.SERVER_ERROR

    # WARNING: class-name only — no PII.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING record naming the exception class"
    warning_msg = warnings[-1].getMessage()
    assert "RuntimeError" in warning_msg
    assert "hunter2" not in warning_msg
    assert "example.com" not in warning_msg

    # DEBUG: traceback attached via exc_info (surfaces only under
    # --verbose, which bumps the logger level to DEBUG).
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug_records, "expected a DEBUG record carrying the traceback"
    assert any(r.exc_info is not None for r in debug_records)


def test_handle_errors_passes_click_exceptions_through() -> None:
    """``ClickException`` (and its :class:`CrewdayError` subclasses)
    must be re-raised so Click's own exit-code plumbing runs."""

    @handle_errors
    def raises_click() -> None:
        raise ConfigError("no such profile")

    with pytest.raises(ConfigError):
        raises_click()


def test_handle_errors_catches_keyboard_interrupt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C at a prompt exits 130 with a terse ``Aborted.`` line."""

    @handle_errors
    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(SystemExit) as exc_info:
        interrupted()
    assert exc_info.value.code == 130
    captured = capsys.readouterr()
    assert captured.err.strip() == "Aborted."


def test_python_dash_m_entry_point_module_exists() -> None:
    """``python -m crewday`` must resolve — the spec layout
    (``docs/specs/01-architecture.md`` §"Repo layout") names
    ``__main__.py`` as the package entry point."""
    from crewday import __main__ as dunder_main

    # The ``main`` symbol re-exported from _main must be what the
    # ``[project.scripts] crewday`` entry also calls, so the two
    # invocation paths share a single implementation.
    assert dunder_main.main is main


def test_placeholders_importable() -> None:
    """The three placeholder modules exist so later phases can plug
    in without churning import paths."""
    # Import-side-effect-only check: the mere ``from crewday import …``
    # statement at the top of this file would already fail the test
    # if a module was missing; adding explicit references keeps the
    # intent auditable.
    from crewday import _client, _config, _output

    assert _client.__all__ == []
    assert _config.__all__ == []
    assert _output.__all__ == []

    # Public surface of the package itself: empty list until codegen
    # wires groups in.
    assert hasattr(_globals, "CrewdayContext")
