"""Integration-layer pytest fixtures.

Per ``docs/specs/17-testing-quality.md`` §"Integration":

* Each test gets a session bound to a per-test transaction that is
  rolled back at teardown — no ``TRUNCATE`` dance needed because the
  rollback naturally reverts every insert/update/delete.
* Migrations run once per session via ``alembic upgrade head`` against
  the test engine. A worker that never reaches a real DB (for
  example, the smoke test below) pays only the fixture's setup cost;
  subsequent tests share it.
* Two backend-selection knobs (cd-rhaj):
    - ``CREWDAY_TEST_DB={sqlite,postgres}`` picks the backend.
      ``sqlite`` (default) returns a per-session temp-file SQLite
      URL. ``postgres`` spins up a session-scoped
      :class:`testcontainers.postgres.PostgresContainer` (image
      ``postgres:15-alpine``) and returns its connection URL, with
      the ``psycopg2`` driver prefix rewritten to ``psycopg`` (v3,
      which is what this project pins — see
      ``app/adapters/db/session.py::normalise_sync_url``).
    - ``CREWDAY_TEST_DATABASE_URL`` is an explicit URL override and
      wins over the selector. Use it when CI has already started a
      PG service outside the test process.
  The backend defaults to a fresh file-based SQLite under pytest's
  ``tmp_path_factory`` because alembic's ``env.py`` creates its own
  engine from the URL — an ``sqlite:///:memory:`` URL would hand
  alembic a different in-memory DB than the one the test holds. A
  temp file lets both sides see the same bytes without any special
  plumbing.

See ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.session import make_engine, normalise_sync_url
from app.config import get_settings


def _alembic_ini() -> Path:
    """Return the repo-root ``alembic.ini`` path."""
    return Path(__file__).resolve().parents[2] / "alembic.ini"


def _backend() -> str:
    """Return the current backend name (``sqlite`` or ``postgres``).

    Resolves ``CREWDAY_TEST_DB`` (case-insensitive) with a ``sqlite``
    default. Raised into a function so collection-time and
    fixture-time paths agree on the value.
    """
    return os.environ.get("CREWDAY_TEST_DB", "sqlite").lower()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``pg_only`` tests when the backend is SQLite.

    Tests that exercise Postgres-only behaviour (RLS predicates,
    PG-specific SQL, etc.) carry the ``pg_only`` marker. On the
    SQLite shard we skip them at collection time so they don't try
    to run against a backend that can't satisfy the prerequisite.
    """
    if _backend() != "sqlite":
        return
    skip_pg = pytest.mark.skip(reason="PG-only test; CREWDAY_TEST_DB=sqlite")
    for item in items:
        if "pg_only" in item.keywords:
            item.add_marker(skip_pg)


@pytest.fixture(scope="session")
def db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Session-scoped test DB URL.

    Honours ``CREWDAY_TEST_DATABASE_URL`` (explicit URL override, used
    when CI already owns the container) first, then falls back to
    the ``CREWDAY_TEST_DB`` backend selector:

    * ``sqlite`` (default): a fresh file-based SQLite under pytest's
      ``tmp_path_factory`` root.
    * ``postgres``: a session-scoped :class:`PostgresContainer`
      (``postgres:15-alpine``). The container is torn down at
      session end. We pass ``driver="psycopg"`` so testcontainers
      emits a ``postgresql+psycopg://`` URL directly — this repo
      pins psycopg 3, not psycopg2, and pinning the driver via
      the constructor avoids a brittle string substitution on the
      returned URL.
    """
    override = os.environ.get("CREWDAY_TEST_DATABASE_URL")
    if override:
        yield override
        return

    backend = _backend()
    if backend == "sqlite":
        root = tmp_path_factory.mktemp("crewday-db")
        yield f"sqlite:///{root / 'test.db'}"
        return

    if backend == "postgres":
        # Imported lazily so the sqlite shard never pulls the docker
        # client (and fails ImportError-style on machines without it).
        from testcontainers.postgres import PostgresContainer

        # ``driver="psycopg"`` makes the container emit a
        # ``postgresql+psycopg://`` URL (psycopg 3, the driver this
        # repo actually pins). Without it testcontainers defaults to
        # ``+psycopg2`` which we don't install. Running the URL
        # through ``normalise_sync_url`` is belt-and-braces — it's a
        # no-op on a ``+psycopg`` URL but keeps parity with the
        # production URL-normalisation path.
        with PostgresContainer("postgres:15-alpine", driver="psycopg") as container:
            yield normalise_sync_url(container.get_connection_url())
        return

    raise ValueError(
        f"Unknown CREWDAY_TEST_DB={backend!r}; expected 'sqlite' or 'postgres'"
    )


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    """Session-scoped SQLAlchemy engine bound to :func:`db_url`.

    Shared across every integration test; disposal happens at session
    teardown.
    """
    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrate_once(engine: Engine, db_url: str) -> Iterator[None]:
    """Run ``alembic upgrade head`` exactly once per worker.

    ``migrations/env.py`` reads the DB URL from
    :func:`app.config.get_settings` rather than from the Alembic
    config file (§01 "Migrations stay shared"), so we set
    ``CREWDAY_DATABASE_URL`` in the process environment for the
    duration of the upgrade and clear ``get_settings``'s lru_cache
    either side so the test URL actually reaches ``env.py``. The
    original value (if any) is restored on teardown.

    Current state has no migration revisions, so this is a no-op on a
    fresh checkout — the fixture still exercises the alembic wiring
    so the first real migration (cd-w7h successors) lights up tests
    immediately.
    """
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = db_url
    get_settings.cache_clear()
    try:
        cfg = AlembicConfig(str(_alembic_ini()))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Function-scoped DB session wrapped in a rollback-on-exit transaction.

    Opens a top-level connection + transaction, binds a
    :class:`Session` to that connection with ``join_transaction_mode=
    "create_savepoint"``, yields it to the test, then rolls back
    everything — including savepoints from nested ``commit()`` calls
    — on teardown.

    This is the "SAVEPOINT per test" pattern the SQLAlchemy docs call
    out as the canonical way to isolate tests without per-test
    schema reset. Much faster than ``TRUNCATE`` on Postgres and
    completely free on SQLite.
    """
    with engine.connect() as raw_connection:
        outer = raw_connection.begin()
        factory = sessionmaker(
            bind=raw_connection,
            expire_on_commit=False,
            class_=Session,
            join_transaction_mode="create_savepoint",
        )
        session = factory()
        try:
            yield session
        finally:
            session.close()
            if outer.is_active:
                outer.rollback()
