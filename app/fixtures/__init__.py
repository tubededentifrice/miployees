"""Deployment-bootstrap seeds (cd-4btd).

Houses helpers that seed deployment-scope data a fresh deployment
needs in order for the §11 LLM resolver to find at least one usable
provider / model / provider_model trio. The package is the home for
**non-demo** seeds — :mod:`app.fixtures.demo` (a future slice, see
:mod:`docs/specs/24-demo-mode`) hosts the scenario fixtures the
demo workspace ships with.

Today the only seed lives in :mod:`app.fixtures.llm`; future seeds
(role catalogue, capability defaults, …) land alongside it.
"""

from __future__ import annotations

__all__: list[str] = []
