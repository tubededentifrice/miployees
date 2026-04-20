"""instructions — instruction / instruction_version.

Both tables in this package are workspace-scoped: each row carries a
``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A bare
read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

``InstructionVersion`` is not reached directly by tenant-scoped
business logic — versions are fetched by joining from
``Instruction`` — but the row is **denormalised** with its owning
``workspace_id`` so the ORM tenant filter injects the predicate
without a join. The denormalisation mirrors ``permission_group_member``
in :mod:`app.adapters.db.authz` and keeps the filter cheap.

``Instruction.current_version_id`` is a plain :class:`str` soft-ref
pointing at an :class:`InstructionVersion.id`. The two tables form a
circular dependency at the type level — a version FK-points back at
its instruction — and SQLAlchemy lets us resolve that with
``use_alter=True`` + ``post_update=True``. We intentionally do not —
an ``instruction`` row is created *before* its first version exists,
and the domain layer writes the ``current_version_id`` atomically
when bumping. A hard FK would force a two-phase write (insert the
instruction with ``NULL``, then UPDATE after the version exists) for
no correctness win; losing a dangling pointer is a data bug the
domain layer guards against, same as ``task.current_evidence_id``
(§02).

``scope_kind`` is an enum — ``template | property | area | asset |
stay | role | workspace`` — carrying the taxonomy in the spec's v1
slice (cd-bce). ``scope_id`` is a soft-ref :class:`str` pointing at
whichever entity the scope kind names; the domain layer resolves it.
When ``scope_kind = 'workspace'`` the scope_id is ``NULL`` — the
instruction applies to the whole workspace.

``created_by`` and ``InstructionVersion.author_id`` are plain
:class:`str` soft-refs: they point at a user id, but the author may
be a system actor (a future seed script, an agent authoring from a
capability). Audit-linkage semantics live in
:mod:`app.adapters.db.audit`, not here.

See ``docs/specs/02-domain-model.md`` §"instruction",
§"instruction_version" and ``docs/specs/07-instructions-kb.md``.
"""

from __future__ import annotations

from app.adapters.db.instructions.models import Instruction, InstructionVersion
from app.tenancy.registry import register

for _table in ("instruction", "instruction_version"):
    register(_table)

__all__ = ["Instruction", "InstructionVersion"]
