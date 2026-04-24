"""Module entry point — lets users run ``python -m crewday``.

Mirrors the shim registered in ``[project.scripts] crewday`` so the
two invocation paths are interchangeable. ``docs/specs/01-architecture.md``
§"Repo layout" marks ``crewday/__main__.py`` as the canonical entry
point; the ``crewday`` console script and ``python -m crewday`` both
resolve here via :func:`crewday._main.main`.

Keeping this module tiny (one import + one call) means the import
cost of ``python -m crewday`` is the same as the console script, and
there is a single implementation of the error-handling contract in
:mod:`crewday._main`.
"""

from __future__ import annotations

from crewday._main import main

__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover — exercised via ``python -m``.
    main()
