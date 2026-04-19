"""Alembic environment for crew.day.

Reads the database URL from :func:`app.config.get_settings` rather than
``alembic.ini`` so one env var — ``CREWDAY_DATABASE_URL`` — governs
runtime and migrations alike. Async driver prefixes
(``sqlite+aiosqlite``, ``postgresql+asyncpg``) are rewritten to their
sync equivalents to match the Unit-of-Work factory.

Per-context model modules live under ``app/adapters/db/<context>/models.py``
and register their tables against the shared
:attr:`app.adapters.db.base.Base.metadata`. This env imports every
discovered ``models`` module so autogenerate sees the full schema,
regardless of which contexts have landed yet. On a bare checkout the
loop finds zero modules — that is expected and fine; autogenerate
then reports an empty diff.

See ``docs/specs/01-architecture.md`` §"Migrations stay shared".
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.adapters.db as adapters_db_pkg
from app.adapters.db.base import Base
from app.adapters.db.session import normalise_sync_url
from app.config import get_settings

_log = logging.getLogger("alembic.env")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Pull the URL from settings and normalise async driver prefixes. We set
# the value on the Alembic config so the standard
# ``engine_from_config`` path below picks it up without further
# plumbing.
_database_url = normalise_sync_url(get_settings().database_url)
config.set_main_option("sqlalchemy.url", _database_url)


def _load_context_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` that exists.

    Walks the top-level packages under :mod:`app.adapters.db` and imports
    any ``<context>.models`` submodule. Missing modules are ignored —
    contexts that have not yet defined tables simply contribute no
    metadata. We do NOT recurse deeper than one level: models live at
    exactly ``app.adapters.db.<context>.models``.
    """
    for modinfo in pkgutil.iter_modules(
        adapters_db_pkg.__path__, prefix=f"{adapters_db_pkg.__name__}."
    ):
        if not modinfo.ispkg:
            continue
        models_name = f"{modinfo.name}.models"
        try:
            importlib.import_module(models_name)
        except ModuleNotFoundError as exc:
            # Only swallow the specific "this context has no models
            # module yet" case; any other ModuleNotFoundError (e.g. a
            # broken import inside models.py) must surface.
            if exc.name == models_name:
                continue
            raise
        else:
            _log.info("alembic: loaded %s", models_name)


_load_context_models()

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    url = config.get_main_option("sqlalchemy.url") or ""
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations without an active DB connection (emit SQL)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_is_sqlite(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live engine."""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=_is_sqlite(),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
