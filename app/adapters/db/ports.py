"""Database ports.

Defines the narrow seam the domain layer uses to reach a SQL backend:

* :class:`DbSession` — a structural subset of SQLAlchemy 2.0's
  :class:`sqlalchemy.orm.Session` that covers the operations domain
  code is allowed to perform.
* :class:`UnitOfWork` — a context manager that yields a
  :class:`DbSession`; concrete adapters wire transaction scoping here.

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
checks against these protocols would mask typos and invite
duck-typing shortcuts.

See ``docs/specs/01-architecture.md`` §"Adapters" and
§"Shared kernel".
"""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Protocol, TypeVar

from sqlalchemy import Executable, Result, ScalarResult

__all__ = ["DbSession", "UnitOfWork"]

_T = TypeVar("_T")


class DbSession(Protocol):
    """Structural subset of :class:`sqlalchemy.orm.Session`.

    Domain services depend on this protocol, not on the concrete
    SQLAlchemy ``Session`` class. ``Any`` leaks from ``execute``'s
    ``params`` and ``get``'s ``ident`` because SQLAlchemy itself
    types them that way — they are bind-parameter bags and primary
    keys whose shape depends on the mapped entity.
    """

    def execute(
        self,
        statement: Executable,
        params: Mapping[str, Any] | None = None,
    ) -> Result[Any]:
        """Execute an ORM or Core statement and return its ``Result``."""
        ...

    def scalar(self, statement: Executable) -> Any:
        """Execute ``statement`` and return the first column of the first row."""
        ...

    def scalars(self, statement: Executable) -> ScalarResult[Any]:
        """Execute ``statement`` and return a scalar result iterator."""
        ...

    def get(self, entity: type[_T], ident: Any) -> _T | None:
        """Return the mapped instance for ``ident`` or ``None`` if absent."""
        ...

    def add(self, instance: object) -> None:
        """Mark ``instance`` as pending for insert on the next flush."""
        ...

    def flush(self) -> None:
        """Push pending changes to the DB without committing the transaction."""
        ...

    def commit(self) -> None:
        """Commit the current transaction."""
        ...

    def rollback(self) -> None:
        """Roll back the current transaction."""
        ...


class UnitOfWork(Protocol):
    """Context manager producing a :class:`DbSession`.

    Usage::

        with uow() as session:
            session.add(entity)
            session.commit()

    Concrete adapters decide whether entering the context opens a
    fresh transaction or reuses an outer one; the protocol only
    commits to the ``with`` shape.
    """

    def __enter__(self) -> DbSession:
        """Enter the transactional scope and return a session."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Exit the scope. Returning ``True`` suppresses the exception."""
        ...
