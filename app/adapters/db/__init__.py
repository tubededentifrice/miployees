"""Database adapters — DbSession port and per-context SQLAlchemy repos.

See docs/specs/01-architecture.md §"Adapters".

Cross-package FK contract
-------------------------

Every ``<pkg>/models.py`` declares its tables on the shared
:data:`Base.metadata`. SQLAlchemy resolves ``ForeignKey("<table>.id",
…)`` strings lazily — the target table only needs to be in
``Base.metadata`` by the time ``create_all`` (or the equivalent DDL
walk) runs. That's a load-order trap: if package ``A`` references
``B``'s table and only ``A.models`` has been imported, ``create_all``
crashes with :class:`~sqlalchemy.exc.NoReferencedTableError`.

The convention to keep every package self-contained is a side-effect
import at the top of ``<pkg>/models.py`` for each cross-package FK
target:

    from app.adapters.db.<other_pkg> import models as _<other_pkg>_models  # noqa: F401

The ``noqa: F401`` is intentional — the import is the registration,
not a name. The dependency graph is acyclic with ``workspace`` as the
single leaf (no cross-package FKs of its own); ``identity`` depends
only on ``workspace``; everything else depends on
``identity`` / ``workspace`` (and a small subset on ``places`` /
``payroll``). Adding a new cross-package FK adds the matching side-
effect import in the same change.

The test suite enforces this indirectly via fixtures that walk every
``app.adapters.db.*`` subpackage before ``create_all``; the side-
effect imports make the contract hold even for callers that load a
single ``<pkg>.models`` directly (a smaller test, an offline script,
a future Alembic env that pins one package).
"""
