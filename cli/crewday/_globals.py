"""Global CLI state — the :class:`CrewdayContext` carried on ``click.Context.obj``.

Every Click command receives the same :class:`CrewdayContext` via
``click.pass_obj``; it packages the resolved profile, workspace slug,
output mode, a per-invocation idempotency-key generator, and a
scoped logger so commands never reach for process-wide state.

See ``docs/specs/13-cli.md`` §"Global flags" and §"Agent UX
conventions"; ``docs/specs/12-rest-api.md`` §"Idempotency".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_OUTPUT",
    "OUTPUT_CHOICES",
    "CrewdayContext",
    "OutputMode",
    "default_idempotency_key_factory",
]


OutputMode = Literal["json", "yaml", "table", "ndjson"]

#: The four output modes from §13 "Output". Ordered so the default
#: comes first, matching ``--output``'s help text.
OUTPUT_CHOICES: tuple[OutputMode, ...] = ("json", "yaml", "table", "ndjson")

DEFAULT_OUTPUT: OutputMode = "json"


def default_idempotency_key_factory() -> str:
    """Return a fresh ULID string suitable for ``Idempotency-Key``.

    The CLI does not need a separate format — §12 "Idempotency"
    accepts any opaque 1..255-char ASCII value, and ULIDs are
    lexicographically sortable, making them convenient for operator
    inspection of audit logs.
    """
    return new_ulid()


@dataclass(frozen=True, slots=True)
class CrewdayContext:
    """Immutable per-invocation CLI context.

    Populated by :func:`crewday._main.main` from global flags / env
    vars / config. Passed down to every command through Click's
    ``ctx.obj`` mechanism.

    Fields:
      * ``profile``: name of the active profile from
        ``~/.config/crewday/config.toml`` (or ``None`` when no
        profile is resolved — commands that require one must error
        with ``ClickException`` and exit 5, see §13 "Exit codes").
      * ``workspace``: workspace slug (``^[a-z][a-z0-9-]{1,38}[a-z0-9]$``
        per §01 "Workspace addressing"), or ``None`` when the verb is
        addressable at the bare host (``auth login``, ``admin ...``).
      * ``output``: resolved :data:`OutputMode`.
      * ``idempotency_key_factory``: zero-arg callable returning a
        fresh idempotency key per mutating call. Override via
        ``--idempotency-key`` wraps this with a constant factory.
      * ``logger``: scoped to ``crewday``; commands create children
        (``logger.getChild("tasks")``). Verbose mode bumps the level
        to DEBUG; default is WARNING so stdout stays clean for agent
        consumption.
    """

    profile: str | None
    workspace: str | None
    output: OutputMode
    idempotency_key_factory: Callable[[], str] = field(
        default=default_idempotency_key_factory,
    )
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("crewday"))
