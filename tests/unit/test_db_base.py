"""Tests for :mod:`app.adapters.db.base`.

Locks two contracts the rest of the DB layer depends on:

* The shared :class:`MetaData` carries the naming convention so
  Alembic autogenerate emits deterministic FK / index / check names
  (required for SQLite batch-alter round-trips).
* :class:`Base` and the module-level ``metadata`` point at the same
  object; mapped classes in per-context ``models.py`` files must all
  land in one timeline.
"""

from __future__ import annotations

from app.adapters.db.base import NAMING_CONVENTION, Base, metadata


def test_metadata_carries_naming_convention() -> None:
    assert dict(metadata.naming_convention) == NAMING_CONVENTION


def test_base_shares_module_metadata() -> None:
    assert Base.metadata is metadata


def test_naming_convention_covers_all_constraint_kinds() -> None:
    """Every constraint family must have a deterministic name template.

    Missing any of these keys — even ``ck`` — means Alembic falls back
    to anonymous names for that family and autogenerate round-trips
    drift on SQLite.
    """
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
