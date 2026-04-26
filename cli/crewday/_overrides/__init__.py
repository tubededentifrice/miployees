"""Hand-written Click overrides for composite CLI flows.

The codegen pipeline (Beads ``cd-1cfg``) builds one Click command per
``operation_id`` from the OpenAPI surface — perfect for plain CRUD,
but composite verbs (interactive ``auth login``, ``tasks complete``
that uploads evidence then transitions state, ``expenses submit`` that
scans + creates + submits) need bespoke wiring. Per spec
``docs/specs/13-cli.md`` §"Overrides", every override module exposes
``register(root: click.Group)`` and stamps each command with
:func:`cli_override` so the parity gate (cd-1cfg) sees the wrapped
``operation_id``\\ s as covered.

Override modules are auto-discovered: :func:`register_overrides` walks
the package's submodules (skipping ``_*`` and ``__init__``) and
delegates to each module's ``register(root)``. Adding a new override is
therefore a matter of dropping a new file in this directory; the
runtime picks it up at startup.

See ``docs/specs/13-cli.md`` §"Overrides", §"Auth & profiles",
§"crewday tasks", §"crewday expenses".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable

import click

__all__ = [
    "cli_override",
    "register_overrides",
]


def cli_override(
    group: str,
    verb: str,
    *,
    covers: list[str],
) -> Callable[[click.Command], click.Command]:
    """Stamp a Click command with the override metadata.

    The parity gate (cd-1cfg) reads ``_cli_override`` to confirm that
    every entry on the ``operation_id`` allow-list either has a
    generated command or a hand-written override claiming coverage.
    The decorator is therefore declarative: it does not change runtime
    behaviour, it just attaches a tuple the gate can introspect.

    ``covers`` is a list (not a tuple) at the call site for ergonomic
    Python literals, but stored as a tuple on the command so the
    metadata is immutable once stamped — a future refactor that
    mutates it in place would have to convert through ``list()`` first,
    which is a clear signal in code review.
    """

    def decorator(command: click.Command) -> click.Command:
        # Attach the metadata as a private attribute. Click's
        # ``Command`` class is not typed against arbitrary attributes
        # but the CLI parity gate inspects via ``getattr`` so the
        # untyped attribute is safe within our own code paths.
        command._cli_override = (group, verb, tuple(covers))  # type: ignore[attr-defined]
        return command

    return decorator


def register_overrides(root: click.Group) -> None:
    """Mount every override module's commands onto ``root``.

    Walks the package via :mod:`pkgutil` so a new override file lands
    automatically — no central registration list to keep in sync. The
    iteration order is filesystem order (alphabetical on POSIX); each
    module's ``register(root)`` is responsible for adding itself
    *under* the correct group (e.g. ``tasks complete`` lands under the
    existing ``tasks`` group from codegen).

    Idempotent at the import level (Python caches modules) but the
    individual ``register()`` calls would re-register a command on a
    second invocation, silently shadowing the first. Callers
    (``crewday._main``) gate this with a one-shot module-level flag
    just like the codegen registration path.
    """
    package_name = __name__
    package = importlib.import_module(package_name)
    package_path = package.__path__

    for module_info in pkgutil.iter_modules(package_path):
        # Skip leading-underscore helpers (e.g. ``_helpers.py``) so
        # override authors can colocate private utilities with their
        # commands without those getting a ``register()`` look-up.
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package_name}.{module_info.name}")
        register = getattr(module, "register", None)
        if register is None:
            # An override file that ships without ``register`` is a
            # programming error — fail loudly rather than silently
            # ignoring it (the operationIds that module claimed to
            # cover would then go uncovered).
            raise RuntimeError(
                f"override module {module.__name__!r} is missing the "
                "required register(root: click.Group) entry point"
            )
        register(root)
