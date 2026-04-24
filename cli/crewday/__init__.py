"""``crewday`` — thin Click-based CLI over the crew.day REST API.

See ``docs/specs/13-cli.md``. The CLI is a local client to the same
HTTP surface as ``api.v1.*`` (§01 "High-level picture"); it holds no
server-side logic and no state beyond config profiles
(``~/.config/crewday/config.toml``).

The public entry point is :func:`crewday._main.main`, registered as
the ``crewday`` script in ``pyproject.toml``. All non-leaf modules in
this package are leading-underscore internals so the OpenAPI-driven
command generator (cd-1cfg) can own the top-level command surface
without colliding with our scaffolding.
"""

from __future__ import annotations

__all__: list[str] = []
