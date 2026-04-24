"""Output formatter placeholder.

Real implementation in Beads ``cd-oe5j`` (``feat(cli/output): json |
yaml | table | ndjson formatters``). Until then, commands route
directly through :mod:`json` — the module is present so later verbs
can migrate without rippling import changes.

See ``docs/specs/13-cli.md`` §"Output".
"""

from __future__ import annotations

__all__: list[str] = []
