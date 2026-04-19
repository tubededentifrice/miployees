"""SQLAlchemy 2.0 DeclarativeBase + shared metadata + naming convention.

Every per-context mapped class under ``app/adapters/db/<context>/models.py``
inherits from :class:`Base`. The shared :class:`sqlalchemy.MetaData`
carries a naming convention so Alembic autogenerate emits deterministic
constraint names — without it, anonymous FK / index / check names break
round-trips on SQLite's batch-alter path (the new table gets a fresh
random name each time, which shows up as a spurious diff).

See ``docs/specs/01-architecture.md`` §"Adapters" and §"Migrations stay
shared"; ``docs/specs/02-domain-model.md`` §"Conventions".
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["NAMING_CONVENTION", "Base", "metadata"]

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Shared declarative base for every crew.day mapped class.

    All per-context model modules (``app/adapters/db/<context>/models.py``)
    import and subclass this. Binding them to a single
    :class:`~sqlalchemy.MetaData` is what lets a single Alembic timeline
    cover the whole app (§01 "Migrations stay shared").
    """

    metadata = metadata
