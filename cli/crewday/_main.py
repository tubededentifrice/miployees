"""Click root group and ``crewday`` entry point.

The root group exposes the v1 global flags from §13 "Global flags"
and hands a fully-populated :class:`~crewday._globals.CrewdayContext`
down to every subcommand via ``click.pass_obj``. Subcommands are not
registered yet — the codegen pipeline (Beads ``cd-1cfg``) loads
``_surface.json`` at import time and builds them dynamically. Until
that lands, ``crewday --help`` lists only the globals and
``crewday --version``, which is sufficient to verify the entry-point
wiring end-to-end.

Error handling: Click's own :class:`click.ClickException` already
maps to a non-zero exit; :func:`main` additionally traps
:class:`KeyboardInterrupt` (the user aborting at a prompt) and any
unhandled exception to print a short human message on stderr and
return a non-zero exit instead of a stack trace. The exit codes from
§13 "Exit codes" (``1`` client, ``2`` server/network, ``3`` approval
pending, ``4`` rate-limited, ``5`` config) are modelled by
:class:`CrewdayError` and its subclasses so later phases have one
consistent raise site.

See ``docs/specs/13-cli.md`` §"Global flags", §"Output",
§"Exit codes"; ``docs/specs/01-architecture.md`` §"High-level
picture".
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from functools import wraps
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import click

from crewday._globals import (
    DEFAULT_OUTPUT,
    OUTPUT_CHOICES,
    CrewdayContext,
    OutputMode,
    default_idempotency_key_factory,
)

__all__ = [
    "ApprovalPending",
    "ConfigError",
    "CrewdayError",
    "ExitCode",
    "RateLimited",
    "ServerError",
    "handle_errors",
    "main",
    "root",
]


class ExitCode:
    """Numeric exit codes from §13 "Exit codes".

    Declared as a namespace class (not an ``IntEnum``) so callers can
    write ``sys.exit(ExitCode.CLIENT_ERROR)`` without pulling in the
    ``enum`` machinery. The values match the spec table verbatim.
    """

    SUCCESS = 0
    CLIENT_ERROR = 1
    SERVER_ERROR = 2
    APPROVAL_PENDING = 3
    RATE_LIMITED = 4
    CONFIG_ERROR = 5


class CrewdayError(click.ClickException):
    """Base class for CLI errors that carry a spec exit code.

    :class:`click.ClickException` already prints its message on
    stderr and exits with ``self.exit_code``; we just pin the exit
    code per §13 so subclasses stay declarative. Commands raising
    one of these never need to touch ``sys.exit`` themselves.
    """

    exit_code: int = ExitCode.CLIENT_ERROR


class ConfigError(CrewdayError):
    """Profile / base URL / token resolution failure."""

    exit_code = ExitCode.CONFIG_ERROR


class ServerError(CrewdayError):
    """5xx or transport-level failure."""

    exit_code = ExitCode.SERVER_ERROR


class ApprovalPending(CrewdayError):
    """Request accepted but blocked behind human approval (§11)."""

    exit_code = ExitCode.APPROVAL_PENDING


class RateLimited(CrewdayError):
    """Exceeded retry budget against a 429 / token-bucket response."""

    exit_code = ExitCode.RATE_LIMITED


def _resolve_version() -> str:
    """Return the installed package version, or a dev fallback.

    Mirrors :func:`app.api.factory._resolve_version`'s pattern: ask
    ``importlib.metadata``, fall back to a sentinel when the package
    is running from an editable checkout that hasn't been installed
    (``uv run`` with a virtual project). The fallback keeps
    ``crewday --version`` working in every dev environment without
    hard-coding a literal.
    """
    try:
        return _pkg_version("crewday")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def handle_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Decorator that funnels unhandled exceptions to a clean exit.

    Click already handles :class:`click.ClickException` (and therefore
    :class:`CrewdayError`) itself — the subclass exit codes propagate
    through :meth:`click.BaseCommand.main`. This decorator adds two
    things: a :class:`KeyboardInterrupt` branch (user pressed Ctrl-C
    at a prompt or mid-stream) that prints ``Aborted`` on stderr and
    exits ``130`` as Unix convention dictates, and a catch-all for
    :class:`Exception` that logs via the ``crewday`` logger and exits
    :attr:`ExitCode.SERVER_ERROR`. The second branch is the final
    safety net; business logic should raise a :class:`CrewdayError`
    subclass so the exit code is explicit.

    The traceback of an unhandled :class:`Exception` is emitted at
    ``DEBUG`` level only — it is therefore visible with ``--verbose``
    but suppressed in the default/quiet mode. This is deliberate:
    tracebacks frequently contain PII (exception messages built from
    user input, row values printed by ORM errors) and spec §15
    "Security & privacy" forbids leaking PII into operator-visible
    output unless the operator has opted in. The WARNING line names
    the exception class only, which is safe by construction.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except click.ClickException:
            # Let Click's own machinery print + exit; do not swallow.
            raise
        except KeyboardInterrupt:
            click.echo("Aborted.", err=True)
            sys.exit(130)
        except Exception as exc:
            logger = logging.getLogger("crewday")
            # Class name is safe (no PII); the full traceback goes to
            # DEBUG so it only surfaces under ``--verbose``. ``exc_info``
            # on the debug call attaches the traceback to that record
            # rather than the warning one.
            logger.warning("unhandled CLI error: %s", type(exc).__name__)
            logger.debug("unhandled CLI error traceback", exc_info=exc)
            click.echo(
                "crewday: internal error (re-run with --verbose for details)",
                err=True,
            )
            sys.exit(ExitCode.SERVER_ERROR)

    return wrapper


@click.group(
    name="crewday",
    context_settings={
        "help_option_names": ["-h", "--help"],
        # Let users pass ``--`` and we do not rewrite option order.
        "auto_envvar_prefix": "CREWDAY",
    },
)
@click.version_option(
    _resolve_version(),
    "-V",
    "--version",
    prog_name="crewday",
    message="%(prog)s %(version)s",
)
@click.option(
    "--profile",
    type=str,
    default=None,
    envvar="CREWDAY_PROFILE",
    help=(
        "Name of the profile in ~/.config/crewday/config.toml to use "
        "for base URL + token. Overrides CREWDAY_PROFILE."
    ),
)
@click.option(
    "--workspace",
    type=str,
    default=None,
    envvar="CREWDAY_WORKSPACE",
    help=(
        "Workspace slug to target (the '<slug>' in /w/<slug>/api/v1/...). "
        "Required for every workspace-scoped verb; optional for auth/admin."
    ),
)
@click.option(
    "-o",
    "--output",
    type=click.Choice(OUTPUT_CHOICES, case_sensitive=False),
    default=DEFAULT_OUTPUT,
    show_default=True,
    envvar="CREWDAY_OUTPUT",
    help="Output format. 'json' (default) and 'ndjson' stream cleanly through jq.",
)
@click.option(
    "--verbose/--quiet",
    default=False,
    help="Bump the 'crewday' logger to DEBUG on stderr (default: WARNING).",
)
@click.pass_context
def root(
    ctx: click.Context,
    *,
    profile: str | None,
    workspace: str | None,
    output: str,
    verbose: bool,
) -> None:
    """crew.day command-line interface.

    A thin client over the crew.day REST API. Every workspace verb is
    addressed as ``crewday <group> <verb>``; deployment admin verbs
    live under ``crewday deploy``, and host-only operator verbs under
    ``crewday admin``. See ``docs/specs/13-cli.md`` for the full
    command tree and the code behind ``_surface.json`` for the
    canonical source.
    """
    # Click's ``click.Choice`` returns the chosen value as a plain
    # ``str``; narrow it to the Literal so the dataclass field stays
    # strictly typed without a cast.
    normalised: OutputMode = _narrow_output(output)

    ctx.obj = CrewdayContext(
        profile=profile,
        workspace=workspace,
        output=normalised,
        idempotency_key_factory=default_idempotency_key_factory,
        logger=logging.getLogger("crewday"),
    )

    # Configure the 'crewday' logger lazily — the root logger is owned
    # by whoever invoked the CLI (tests, shells, uvicorn in the agent
    # embedded runtime). We only touch our own branch.
    cli_logger = logging.getLogger("crewday")
    cli_logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
    if not cli_logger.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        cli_logger.addHandler(handler)
        # Do not propagate — the host application's root logger (if
        # any) should stay clean. Scoped children inherit this.
        cli_logger.propagate = False


def _narrow_output(value: str) -> OutputMode:
    """Narrow a ``--output`` string to :data:`OutputMode`.

    ``click.Choice(case_sensitive=False)`` accepts ``JSON`` /
    ``Json`` / ``json`` but always returns the canonical form (the
    entry from the choice tuple). We walk the choice tuple
    explicitly so mypy can see each branch returns a concrete
    literal — cheaper than ``cast`` and the real invariant stays
    auditable.
    """
    match value:
        case "json":
            return "json"
        case "yaml":
            return "yaml"
        case "table":
            return "table"
        case "ndjson":
            return "ndjson"
        case _:
            raise click.BadParameter(
                f"unexpected --output value: {value!r}",
                param_hint="--output",
            )


@handle_errors
def main() -> None:
    """Console-script entry point (``[project.scripts] crewday``).

    Delegates to :func:`root` with ``standalone_mode=True`` so Click
    handles ``--help`` / exit-code plumbing; the :func:`handle_errors`
    wrapper catches the two cases Click does not own: Ctrl-C and an
    unhandled :class:`Exception` (which should not happen — every
    CLI error path raises a :class:`CrewdayError`).
    """
    root(prog_name="crewday")


if __name__ == "__main__":  # pragma: no cover — ``python -m crewday._main`` path.
    # ``python -m crewday`` routes through :mod:`crewday.__main__` instead,
    # which imports :func:`main` from here. This block is only reached
    # when someone targets the submodule explicitly.
    main()
