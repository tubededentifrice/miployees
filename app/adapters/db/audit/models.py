"""``audit_log`` — append-only immutable mutation log.

See ``docs/specs/02-domain-model.md`` §"audit_log",
``docs/specs/15-security-privacy.md`` §"Audit log", and
``docs/specs/01-architecture.md`` §"Key runtime invariants" #3.
Every domain mutation writes one row here in the same transaction as
the mutation — commit the Unit-of-Work and the audit row lands;
rollback and it's gone.

The spec (§02) calls for a richer schema than what is materialised
here (``before_json`` / ``after_json`` split, hash-chain columns,
``via`` / ``token_id`` provenance, …). This cd-ehf slice ships the
minimum append-only surface consumed by cd-bfc's self-review and
the blocked DB-context tasks (cd-1b2, cd-chd, …); the remaining
fields are added by follow-up migrations owned by
``audit_integrity_check`` / ``audit_verify`` (§15 "Tamper
detection") without widening this table's public write contract.

``scope_kind`` partitions audit rows into two universes:

* ``'workspace'`` (legacy default) — the row is workspace-scoped,
  ``workspace_id`` is NOT NULL, and the ORM tenant filter pins
  reads to the active :class:`~app.tenancy.WorkspaceContext`.
  Every domain mutation under ``/api/v1/...`` and ``/w/<slug>/...``
  emits a row in this partition.
* ``'deployment'`` — the row records an admin mutation against the
  deployment itself (token mint/revoke against an operator
  identity, ``deployment_setting`` edit, signup-policy change, …).
  ``workspace_id`` is NULL; the ``GET /admin/api/v1/audit`` feed
  (§12) reads this partition under
  :func:`~app.tenancy.tenant_agnostic` because there is no tenant
  to pin to. The biconditional CHECK below enforces the
  "deployment ⇒ NULL" / "workspace ⇒ NOT NULL" pairing at the DB
  level (cd-kgcc).

No foreign keys: the spec calls for "soft refs only, for speed"
(§02 entity preamble). The columns are ULID strings and carry their
own semantics; enforcing FKs would force every audit emission to
resolve a real ``users`` / ``workspace`` row even when the referent
has been archived.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["AuditLog"]


# Allowed ``audit_log.scope_kind`` values, enforced by a CHECK
# constraint installed by the cd-kgcc migration. Mirrors the
# ``role_grant`` analogue (cd-wchi) so the two scope-tagged tables
# share the same enum.
_SCOPE_KIND_VALUES: tuple[str, ...] = ("workspace", "deployment")


class AuditLog(Base):
    """Append-only audit row: one per domain mutation.

    The writer (:mod:`app.audit`) is the only allowed producer; the
    table is never updated or deleted by application code. Retention
    rotation (§02 "Operational-log retention defaults",
    ``rotate_audit_log`` worker) archives rows to JSONL.gz and
    deletes the originals; that is the sole supported delete path.

    ``scope_kind`` is ``'workspace'`` for the legacy per-tenant
    timeline and ``'deployment'`` for admin actions whose subject is
    the deployment itself (§12 "Admin surface"). The biconditional
    CHECK below enforces ``workspace_id IS NULL ⇔ scope_kind =
    'deployment'`` at the DB level — the application's writer keeps
    the same invariant, this is defence-in-depth.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # ``workspace_id`` is NULL for deployment-scoped rows and
    # non-NULL for workspace-scoped rows — the pairing CHECK below
    # enforces the biconditional invariant. The cd-kgcc migration
    # widened the column from NOT NULL; the new biconditional CHECK
    # closes the hole that widening would otherwise open.
    workspace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    actor_grant_role: Mapped[str] = mapped_column(String, nullable=False)
    actor_was_owner_member: Mapped[bool] = mapped_column(Boolean, nullable=False)
    entity_kind: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    # ``diff`` carries arbitrary JSON-serialisable payloads the caller
    # supplies — dict for structured changes, list for bulk events,
    # empty dict when the mutation is shape-free (a ``deleted``). The
    # outer ``Any`` is scoped to SQLAlchemy's ``JSON`` column type;
    # the writer's public signature constrains callers to concrete
    # mapping/sequence/``None`` inputs.
    diff: Mapped[Any] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    # ``scope_kind`` partitions rows into ``workspace`` (legacy
    # default) and ``deployment`` (admin surface). Defaulted to
    # ``'workspace'`` on the Python side so existing call sites that
    # only set ``workspace_id`` keep working — every legacy
    # ``AuditLog(workspace_id=..., ...)`` is implicitly a
    # workspace-scoped row.
    scope_kind: Mapped[str] = mapped_column(String, nullable=False, default="workspace")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Two composite indexes the spec calls for (§02 "audit_log"): a
    # per-workspace timeline feed (list newest-first) and a
    # per-entity lookup (one entity's full history). The naming
    # convention in ``base.py`` only emits ``ix_<col_label>`` for
    # single-column indexes; these composite shapes need an explicit
    # name so alembic autogenerate sees a stable identifier.
    #
    # ``ix_audit_log_scope_kind_created`` (cd-kgcc) backs the
    # ``GET /admin/api/v1/audit`` feed — a per-deployment newest-first
    # walk over ``scope_kind = 'deployment'``. The workspace-keyed
    # composite above does not cover that partition because
    # deployment rows carry ``workspace_id IS NULL``.
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('" + "', '".join(_SCOPE_KIND_VALUES) + "')",
            name="scope_kind",
        ),
        # Biconditional: a deployment row carries no workspace_id;
        # a workspace row must carry one. The DB-level CHECK is
        # defence-in-depth — :mod:`app.audit` is the first line of
        # defence and the writer never lets the two halves disagree.
        CheckConstraint(
            "(scope_kind = 'deployment' AND workspace_id IS NULL) "
            "OR (scope_kind = 'workspace' AND workspace_id IS NOT NULL)",
            name="scope_kind_workspace_pairing",
        ),
        Index("ix_audit_log_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_audit_log_workspace_entity",
            "workspace_id",
            "entity_kind",
            "entity_id",
        ),
        Index("ix_audit_log_scope_kind_created", "scope_kind", "created_at"),
    )
