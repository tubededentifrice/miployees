"""Schema fingerprint parity: SQLite vs Postgres.

Per cd-rhaj acceptance criterion "Migrations apply identically on
SQLite and PG (asserted by a schema-fingerprint test)".

The fingerprint is the *structural* shape of the schema — table
names, column names, nullability, PK/FK membership, index columns —
not dialect-specific types (``TEXT`` vs ``VARCHAR``, ``INTEGER`` vs
``INT4``, etc.). The two backends disagree on type keywords in
legitimate ways; a diff on *names* or *relationships* is the bug we
want the gate to catch (a missing migration on one side, an
accidentally-renamed column, a FK pointed at the wrong table).

The test spins up its **own** ``PostgresContainer`` rather than
piggy-backing on the session fixture — the session fixture only
targets the backend the current shard asked for, but we always want
to compare both sides here. Skipped cleanly when Docker isn't
available (typical on a dev laptop without Docker Desktop, or on a
CI runner that hasn't loaded the engine).

See ``docs/specs/17-testing-quality.md`` §"Integration" and
``docs/specs/01-architecture.md`` §"Migrations stay shared".
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, inspect

from app.adapters.db.session import make_engine, normalise_sync_url
from app.config import get_settings

pytestmark = pytest.mark.integration


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
    """Temporarily point ``app.config.get_settings`` at ``url``.

    ``migrations/env.py`` resolves the DB URL via
    :func:`app.config.get_settings`, so the context manager sets the
    env var, clears the lru_cache either side, and restores the
    original value on exit.
    """
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


def _migrate(url: str) -> Engine:
    """Apply ``alembic upgrade head`` against ``url`` and return an engine."""
    engine = make_engine(url)
    with _override_database_url(url):
        cfg = AlembicConfig(str(_alembic_ini()))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
    return engine


def _fingerprint(engine: Engine) -> dict[str, Any]:
    """Return a dialect-agnostic structural fingerprint of ``engine``'s schema.

    Skips ``alembic_version`` — it's a bookkeeping table not part of
    the domain schema, and a diff on *its* name or shape would mean
    Alembic itself changed, not the app.

    The fingerprint intentionally ignores reflection details that
    differ between backends for the same DDL:

    * ``get_columns()['primary_key']`` is only populated on SQLite.
      PK membership is compared via :meth:`Inspector.get_pk_constraint`
      which both backends implement.
    * A ``UNIQUE`` constraint declared in Alembic shows up under
      :meth:`Inspector.get_unique_constraints` on SQLite and as a
      ``unique=True`` entry in :meth:`Inspector.get_indexes` on
      PG (because PG implements ``UNIQUE`` via a unique index).
      We union the two into a single ``uniques`` set of column
      tuples, and a single ``indexes_non_unique`` set of column
      tuples, so the comparison is symmetric.
    * CHECK constraint *bodies* are compared by name only — SQLite
      stores the original expression text, PG canonicalises it
      (``'ok'::text`` vs ``'ok'``, redundant parens, operator
      normalisation), so body-level equality would fight the
      dialects. Constraint *names* are deterministic under the
      shared :mod:`app.adapters.db.base` naming convention, so a
      missing / extra CHECK on one side is the diff we want to
      catch; a bodies-equal / names-equal state is the invariant.
    """
    insp = inspect(engine)
    tables: dict[str, Any] = {}
    for name in sorted(insp.get_table_names()):
        if name == "alembic_version":
            continue

        columns = {
            c["name"]: {
                "nullable": bool(c["nullable"]),
            }
            for c in insp.get_columns(name)
        }

        pk = insp.get_pk_constraint(name)
        pk_cols = tuple(pk.get("constrained_columns", []) or [])

        fks = sorted(
            (
                tuple(fk.get("constrained_columns", []) or []),
                fk.get("referred_table"),
                tuple(fk.get("referred_columns", []) or []),
            )
            for fk in insp.get_foreign_keys(name)
        )

        # Union uniques coming from either surface. Tuple-of-columns
        # is the primitive both inspectors agree on.
        uniques_from_constraints: set[tuple[str, ...]] = {
            tuple(uc.get("column_names", []) or [])
            for uc in insp.get_unique_constraints(name)
        }
        uniques_from_indexes: set[tuple[str, ...]] = set()
        non_unique_indexes: set[tuple[str, ...]] = set()
        for ix in insp.get_indexes(name):
            cols = tuple(ix.get("column_names", []) or [])
            if ix.get("unique"):
                uniques_from_indexes.add(cols)
            else:
                non_unique_indexes.add(cols)
        uniques = sorted(uniques_from_constraints | uniques_from_indexes)
        indexes = sorted(non_unique_indexes)

        # CHECK constraints by name (see docstring for why names, not
        # bodies). Sorted so order-of-reflection differences between
        # the two inspectors don't fight us; ``name`` can be None on
        # anonymous constraints — fall back to the sqltext snippet so
        # two anonymous constraints on the same table still compare.
        checks = sorted(
            ck.get("name") or str(ck.get("sqltext", ""))
            for ck in insp.get_check_constraints(name)
        )

        tables[name] = {
            "columns": columns,
            "pk": pk_cols,
            "fks": fks,
            "indexes": indexes,
            "uniques": uniques,
            "checks": checks,
        }
    return tables


def _dict_diff(
    left: dict[str, Any], right: dict[str, Any], path: str = ""
) -> list[str]:
    """Return human-readable diff lines between two fingerprint dicts."""
    diffs: list[str] = []
    for key in sorted(set(left) | set(right)):
        here = f"{path}.{key}" if path else key
        if key not in left:
            diffs.append(f"+ only in pg: {here}")
            continue
        if key not in right:
            diffs.append(f"- only in sqlite: {here}")
            continue
        l_val, r_val = left[key], right[key]
        if isinstance(l_val, dict) and isinstance(r_val, dict):
            diffs.extend(_dict_diff(l_val, r_val, here))
        elif l_val != r_val:
            diffs.append(f"~ {here}: sqlite={l_val!r} pg={r_val!r}")
    return diffs


def test_schema_fingerprint_matches_on_sqlite_and_pg(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Migrations must produce the same structural schema on both backends.

    Runs ``alembic upgrade head`` against a fresh SQLite file and a
    freshly-spun-up ``postgres:15-alpine`` testcontainer, computes a
    dialect-agnostic fingerprint of each, and asserts they agree on
    tables, columns, PK/FK/unique membership, and index columns.
    Types are intentionally NOT compared — a migration is portable
    if the *relationships* and *names* match, even when the dialect
    renders e.g. ``VARCHAR`` as ``TEXT``.

    Skipped when Docker isn't reachable (no engine, no daemon, or
    insufficient perms) so dev machines without Docker Desktop can
    still run the rest of the integration suite.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - dep is in dev group
        pytest.skip(f"testcontainers not installed: {exc}")

    sqlite_path = tmp_path_factory.mktemp("parity-sqlite") / "parity.db"
    sqlite_url = f"sqlite:///{sqlite_path}"
    sqlite_engine = _migrate(sqlite_url)

    try:
        # The ``with`` statement here is what starts the container; a
        # Docker-less host raises at ``__enter__``. We wrap the
        # whole block so the container is stopped on any exception
        # path, and so we only skip on container-startup failures
        # (not on assertion failures inside the block).
        try:
            # ``driver="psycopg"`` pins the URL to psycopg 3 (the one
            # this repo installs) instead of the testcontainers
            # default of psycopg2 — no post-hoc string substitution
            # needed. See ``tests/integration/conftest.py::db_url``.
            pg_cm = PostgresContainer("postgres:15-alpine", driver="psycopg")
            pg_cm.__enter__()
        except Exception as exc:
            # Narrow: we only catch startup failures (Docker missing,
            # no perms, image pull failed). Once the container is up
            # the ``try`` block below does not catch — assertion
            # failures inside the fingerprint comparison propagate.
            pytest.skip(f"Docker/PostgresContainer unavailable: {exc}")

        try:
            pg_url = normalise_sync_url(pg_cm.get_connection_url())
            pg_engine = _migrate(pg_url)
            try:
                sqlite_fp = _fingerprint(sqlite_engine)
                pg_fp = _fingerprint(pg_engine)
            finally:
                pg_engine.dispose()
        finally:
            pg_cm.__exit__(None, None, None)
    finally:
        sqlite_engine.dispose()

    diffs = _dict_diff(sqlite_fp, pg_fp)
    assert not diffs, "schema fingerprint differs between SQLite and PG:\n" + "\n".join(
        diffs
    )
    # At least one table should have been migrated — empty parity is
    # meaningless and would falsely go green if migrations no-op'd.
    assert sqlite_fp, "migration produced no tables on SQLite"
    assert pg_fp, "migration produced no tables on PG"
