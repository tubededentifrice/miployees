"""HTTP client placeholder.

The real implementation lands in Beads ``cd-2ms7``
(``feat(cli/client): httpx async client — token auth, retries,
streaming, idempotency, pagination``). Keeping this module on the tree
now so import paths are stable: every codegen command (cd-1cfg) will
call ``_client.request()`` and format through ``_output.format()``.

See ``docs/specs/13-cli.md`` §"Runtime command construction" and
§"Global flags".
"""

from __future__ import annotations

__all__: list[str] = []
