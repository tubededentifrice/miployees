"""Synchronous SQLAlchemy engine, session factory, and Unit-of-Work.

The DB seam is **sync-only** for now. Async can come later under a
separate port if a use case forces it; most domain code is request-
scoped and fine with threadpool-backed sync SQLAlchemy.

Public surface:

* :func:`make_engine` — build a sync :class:`~sqlalchemy.engine.Engine`
  from a URL, normalising async driver prefixes.
* :class:`UnitOfWorkImpl` — concrete ``UnitOfWork`` adapter.
* :func:`make_uow` — convenience factory bound to the default engine.

See ``docs/specs/01-architecture.md`` §"Adapters".
"""

from __future__ import annotations

import logging
from types import TracebackType

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.ports import DbSession
from app.config import get_settings

__all__ = ["UnitOfWorkImpl", "make_engine", "make_uow", "normalise_sync_url"]

_log = logging.getLogger(__name__)

# Async-driver prefixes we strip when a sync engine is being built. The
# ``.env.example`` template uses ``sqlite+aiosqlite://`` / ``postgresql+
# asyncpg://`` for future-proofing; the sync factory silently rewrites
# them to the matching sync drivers so dev doesn't need to maintain two
# URLs in parallel.
_ASYNC_TO_SYNC_DRIVER: dict[str, str] = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgresql+aiopg": "postgresql+psycopg",
}


def normalise_sync_url(url: str) -> str:
    """Rewrite async driver prefixes to their sync equivalents.

    Leaves plain ``sqlite://``, ``postgresql://``,
    ``postgresql+psycopg://`` etc. untouched. The rewrite is logged at
    INFO once per call so the operator can tell what ended up on the
    wire.
    """
    parsed = make_url(url)
    sync_driver = _ASYNC_TO_SYNC_DRIVER.get(parsed.drivername)
    if sync_driver is None:
        return url
    rewritten = parsed.set(drivername=sync_driver)
    _log.info(
        "db: async driver %r rewritten to sync %r for UnitOfWork",
        parsed.drivername,
        sync_driver,
    )
    return rewritten.render_as_string(hide_password=False)


def make_engine(url: str | None = None) -> Engine:
    """Return a sync :class:`~sqlalchemy.engine.Engine` for ``url``.

    ``url`` defaults to :attr:`~app.config.Settings.database_url`. Async
    driver prefixes (``+aiosqlite``, ``+asyncpg``) are rewritten to sync
    equivalents — see :data:`_ASYNC_TO_SYNC_DRIVER` — with an INFO log
    so operators can tell.

    Dialect-specific tuning:

    * **SQLite in-memory** (``sqlite:///:memory:``): use
      :class:`~sqlalchemy.pool.StaticPool` and
      ``check_same_thread=False`` so every checkout sees the *same*
      in-memory database across threads. A fresh pool per checkout
      would hand out empty databases.
    * **SQLite file**: ``check_same_thread=False`` so requests on
      worker threads can reuse a connection; default pool otherwise.
    * **Postgres and everything else**: defaults.
    """
    resolved = url if url is not None else get_settings().database_url
    normalised = normalise_sync_url(resolved)
    parsed = make_url(normalised)

    if parsed.drivername.startswith("sqlite"):
        connect_args: dict[str, object] = {"check_same_thread": False}
        is_memory = parsed.database in (None, "", ":memory:")
        if is_memory:
            return create_engine(
                normalised,
                connect_args=connect_args,
                poolclass=StaticPool,
                future=True,
            )
        return create_engine(normalised, connect_args=connect_args, future=True)

    return create_engine(normalised, future=True)


# Lazy module-level defaults. We do NOT build the engine at import time:
# ``Settings`` requires ``CREWDAY_DATABASE_URL`` in env, so eager
# construction would break test collection on machines where that
# isn't set. The first caller of :func:`_default_sessionmaker` pays
# the one-off cost; subsequent callers reuse the cached factory.
_default_engine: Engine | None = None
_default_sessionmaker_: sessionmaker[Session] | None = None


def _default_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide default ``sessionmaker``, building on first use."""
    global _default_engine, _default_sessionmaker_
    if _default_sessionmaker_ is None:
        _default_engine = make_engine()
        _default_sessionmaker_ = sessionmaker(
            bind=_default_engine,
            expire_on_commit=False,
            class_=Session,
        )
    return _default_sessionmaker_


class UnitOfWorkImpl:
    """Concrete :class:`~app.adapters.db.ports.UnitOfWork`.

    Opens a fresh :class:`~sqlalchemy.orm.Session` on ``__enter__``,
    commits on a clean exit, rolls back on an exception, and always
    closes. Exceptions are never swallowed — ``__exit__`` returns
    ``None``.

    Construct one with :func:`make_uow` for production code; pass a
    custom ``session_factory`` for tests that need an isolated engine.
    """

    __slots__ = ("_factory", "_session")

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> DbSession:
        if self._session is not None:
            raise RuntimeError("UnitOfWorkImpl is not reentrant")
        factory = (
            self._factory if self._factory is not None else _default_sessionmaker()
        )
        self._session = factory()
        return self._session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        session = self._session
        if session is None:  # pragma: no cover - defensive
            return None
        try:
            if exc_type is None:
                session.commit()
            else:
                session.rollback()
        finally:
            session.close()
            self._session = None
        return None


def make_uow() -> UnitOfWorkImpl:
    """Return a :class:`UnitOfWorkImpl` bound to the default engine."""
    return UnitOfWorkImpl()
