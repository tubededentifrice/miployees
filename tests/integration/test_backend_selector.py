"""Exercises the ``CREWDAY_TEST_DB`` selector and ``pg_only`` marker.

These tests guard the cd-rhaj plumbing itself: if someone flips the
env var, the right backend must end up on the wire, and a
``pg_only``-marked test must be skipped on SQLite without the
developer having to remember to handle it at the test-body level.

See ``tests/integration/conftest.py`` and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def test_engine_dialect_matches_selector(engine: Engine) -> None:
    """The session-scoped engine must use the dialect the selector asked for.

    ``CREWDAY_TEST_DATABASE_URL`` overrides the selector (used in CI
    envs that own the container externally). The assertion only
    fires when the override is absent so the test is meaningful
    across all three spinup paths.
    """
    if os.environ.get("CREWDAY_TEST_DATABASE_URL"):
        pytest.skip("CREWDAY_TEST_DATABASE_URL override in effect")

    backend = os.environ.get("CREWDAY_TEST_DB", "sqlite").lower()
    name = engine.dialect.name
    if backend == "sqlite":
        assert name == "sqlite", f"expected sqlite dialect, got {name!r}"
    elif backend == "postgres":
        assert name == "postgresql", f"expected postgresql dialect, got {name!r}"
    else:  # pragma: no cover - conftest already raises on unknown values
        pytest.fail(f"unexpected backend {backend!r}")


@pytest.mark.pg_only
def test_pg_only_marker_runs_only_on_postgres(engine: Engine) -> None:
    """A ``pg_only``-marked test reaches its body only on the PG shard.

    On SQLite the ``pytest_collection_modifyitems`` hook in
    ``conftest.py`` adds a skip marker at collection time, so this
    body never executes. On PG the body runs and re-asserts the
    dialect as a belt-and-braces check.
    """
    assert engine.dialect.name == "postgresql"
