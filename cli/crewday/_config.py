"""Profile configuration placeholder.

Real implementation in Beads ``cd-cksj`` (``feat(cli/config):
~/.config/crewday/profiles.toml multi-environment + default
switching``). Until then, ``_main`` accepts ``--profile`` but does
not resolve it to a base URL / token; the subsequent task wires
this module up to a TOML loader that honours ``env:`` token
references per §13 "Config".

See ``docs/specs/13-cli.md`` §"Config" and §"Auth & profiles".
"""

from __future__ import annotations

__all__: list[str] = []
