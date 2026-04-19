"""Tests for :class:`app.adapters.db.session.UnitOfWorkImpl`.

Every test wires its own in-memory SQLite engine and ``sessionmaker``
and injects that into :class:`UnitOfWorkImpl`. No test touches the
module-level default engine — those globals require
``CREWDAY_DATABASE_URL`` at runtime and would leak state between
tests.

Covers:

* commit on clean exit,
* rollback on exception (and the exception still propagates),
* session close on every exit path (so the next UoW gets a fresh one),
* two concurrent UoWs hold independent sessions,
* async driver prefixes in the URL are rewritten to their sync
  equivalents by :func:`make_engine`,
* ``UnitOfWorkImpl`` is not reentrant inside the same object.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.session import UnitOfWorkImpl, make_engine


class _TestBase(DeclarativeBase):
    """Test-local declarative base — deliberately isolated from app metadata.

    Using the production :class:`app.adapters.db.base.Base` would let a
    stray model leak into autogenerate once contexts start landing. This
    base lives only in the test module.
    """


class _User(_TestBase):
    __tablename__ = "uow_test_users"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


@pytest.fixture
def engine() -> Engine:
    """A fresh shared-cache in-memory engine per test.

    :class:`StaticPool` means every checkout sees the same underlying
    SQLite database — required for ``:memory:`` to survive across
    connections opened by the UoW.
    """
    eng = make_engine("sqlite:///:memory:")
    _TestBase.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def test_commits_on_clean_exit(session_factory: sessionmaker[Session]) -> None:
    with UnitOfWorkImpl(session_factory) as session:
        session.add(_User(id="01HXZ30000000000000000COMM", name="Alice"))
        # No explicit commit: the UoW commits on exit.

    with session_factory() as verify:
        stored = verify.scalars(select(_User)).all()
        assert [u.name for u in stored] == ["Alice"]


def test_rolls_back_on_exception(session_factory: sessionmaker[Session]) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        UnitOfWorkImpl(session_factory) as session,
    ):
        session.add(_User(id="01HXZ30000000000000000ROLL", name="Bob"))
        session.flush()  # hit the DB so we can see the rollback work
        raise RuntimeError("boom")

    with session_factory() as verify:
        assert verify.scalars(select(_User)).all() == []


def test_exception_is_not_swallowed(session_factory: sessionmaker[Session]) -> None:
    """``__exit__`` must return ``None`` / falsy — exceptions propagate."""
    with (
        pytest.raises(ValueError, match="propagate"),
        UnitOfWorkImpl(session_factory),
    ):
        raise ValueError("propagate")


def test_session_is_closed_after_exit(session_factory: sessionmaker[Session]) -> None:
    """After exit the UoW's session is released and a new enter works."""
    uow = UnitOfWorkImpl(session_factory)
    with uow as session_one:
        session_one.add(_User(id="01HXZ30000000000000000CLS1", name="Carol"))
    # Reusing the same UoW re-enters cleanly.
    with uow as session_two:
        assert session_two is not session_one
        session_two.add(_User(id="01HXZ30000000000000000CLS2", name="Dan"))

    with session_factory() as verify:
        names = {u.name for u in verify.scalars(select(_User)).all()}
        assert names == {"Carol", "Dan"}


def test_session_closes_after_exception(
    session_factory: sessionmaker[Session],
) -> None:
    uow = UnitOfWorkImpl(session_factory)
    with pytest.raises(RuntimeError), uow:
        raise RuntimeError("oops")
    # Next enter must succeed — the previous session was closed, not
    # leaked.
    with uow as session:
        session.add(_User(id="01HXZ30000000000000000RECO", name="Eve"))

    with session_factory() as verify:
        names = {u.name for u in verify.scalars(select(_User)).all()}
        assert names == {"Eve"}


def test_nested_uows_hold_independent_sessions(
    session_factory: sessionmaker[Session],
) -> None:
    outer = UnitOfWorkImpl(session_factory)
    inner = UnitOfWorkImpl(session_factory)
    with outer as outer_session, inner as inner_session:
        assert outer_session is not inner_session


def test_single_uow_is_not_reentrant(
    session_factory: sessionmaker[Session],
) -> None:
    uow = UnitOfWorkImpl(session_factory)
    with uow, pytest.raises(RuntimeError, match="not reentrant"):
        uow.__enter__()


def test_make_engine_rewrites_aiosqlite_prefix() -> None:
    """``.env.example`` ships ``sqlite+aiosqlite://`` — sync engine must accept it."""
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    assert eng.dialect.name == "sqlite"
    assert eng.dialect.driver == "pysqlite"


def test_make_engine_memory_uses_static_pool() -> None:
    eng = make_engine("sqlite:///:memory:")
    assert isinstance(eng.pool, StaticPool)


def test_make_engine_file_sqlite_keeps_default_pool(tmp_path: object) -> None:
    """File-backed SQLite must not be forced onto StaticPool.

    StaticPool on a file DB would serialise every request through a
    single connection — correct but needlessly slow. The factory only
    applies StaticPool for ``:memory:``.
    """
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db_path = tmp_path / "unit.db"
    eng = make_engine(f"sqlite:///{db_path}")
    assert not isinstance(eng.pool, StaticPool)
