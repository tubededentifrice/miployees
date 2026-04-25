"""Instruction / InstructionVersion SQLAlchemy models.

v1 slice per cd-bce — sufficient for the ``instruction`` + revision
CRUD follow-up (cd-oyq) to layer the auto-version-bump logic on top.
The richer §07 surface (``tags`` array, ``summary_md``,
``attachment_file_ids``, ``status`` / ``deleted_at`` retirement
lifecycle, ``change_note``, the ``instruction_link`` cross-reference
table) lands with those follow-ups without breaking this
migration's public write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. The
``InstructionVersion.workspace_id`` is denormalised from its owning
``Instruction.workspace_id`` so the ORM tenant filter's injected
predicate rides the same B-tree as the row lookup — without this
column the filter would need a join through ``instruction``. FK
hygiene:

* ``Instruction.workspace_id`` cascades on delete — sweeping a
  workspace sweeps its instructions library (the §15 tombstone /
  export worker snapshots first).
* ``InstructionVersion.workspace_id`` likewise cascades — the
  denormalised column participates in the same sweep.
* ``InstructionVersion.instruction_id`` cascades — deleting an
  ``Instruction`` drops every version row in one go. Version rows
  are not independently useful once the parent is gone.
* ``Instruction.current_version_id`` is persisted as a plain
  :class:`str` soft-ref (no SQL foreign key) to sidestep the
  circular dependency between the two tables. The domain layer
  writes the pointer atomically when bumping a version and guards
  against dangling refs; the same pattern is used for
  ``task.current_evidence_id`` (§02) for the same reason.
* ``created_by`` on ``Instruction`` and ``author_id`` on
  ``InstructionVersion`` are plain :class:`str` soft-refs — the
  author may be a user or a system actor (a future seed script,
  an agent authoring from a capability). Audit-trail semantics
  live in :mod:`app.adapters.db.audit`, not here.

Allowed ``scope_kind`` values — the v1 enum matches cd-bce's
explicit taxonomy: ``template`` (a :class:`TaskTemplate`),
``property``, ``area``, ``asset``, ``stay``, ``role`` (a
``work_role``), ``workspace`` (the whole workspace —
``scope_id`` is then ``NULL``). The richer §07 surface uses a
narrower ``global | property | area`` enum plus an
``instruction_link`` cross-reference table; the wider enum here
captures the taxonomy cd-bce pins for the v1 schema so the
follow-up work can either project onto the spec's narrower enum
or widen the spec — both roads stay open.

See ``docs/specs/02-domain-model.md`` §"instruction",
§"instruction_version" and ``docs/specs/07-instructions-kb.md``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``workspace.id`` FKs below
# resolve against ``Base.metadata`` only if ``workspace.models`` has
# been imported, so we register it here as a side effect.
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["Instruction", "InstructionVersion"]


# Allowed ``instruction.scope_kind`` values — the v1 taxonomy matching
# cd-bce's explicit scope enum. Widening / narrowing against §07's
# ``global | property | area`` spec happens in the cd-oyq follow-up
# without rewriting history.
_SCOPE_KIND_VALUES: tuple[str, ...] = (
    "template",
    "property",
    "area",
    "asset",
    "stay",
    "role",
    "workspace",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``time`` / ``payroll`` / ``tasks`` /
    ``stays`` / ``places`` modules so the enum CHECK constraint below
    stays readable.
    """
    return "'" + "', '".join(values) + "'"


class Instruction(Base):
    """A standing piece of content attached to a scope.

    An instruction is the long-lived anchor row: a title, a slug
    unique within the workspace, a scope (``scope_kind`` + optional
    ``scope_id``), and a pointer at its *current* version. The body
    itself lives on the linked :class:`InstructionVersion` rows — a
    new version is minted on every edit (cd-oyq will own the bump
    logic).

    UNIQUE ``(workspace_id, slug)`` enforces cd-bce's acceptance
    criterion: a workspace cannot have two instructions sharing a
    slug. The ``(workspace_id, scope_kind, scope_id)`` index powers
    the "instructions that apply to this <scope>" lookup the worker
    task screen runs on every open (§07 §"Rendered in context").

    ``current_version_id`` is a plain :class:`str` soft-ref: the two
    tables are mutually dependent (a version FK-points at its
    instruction), and a hard FK would force a two-phase write (insert
    the instruction with ``NULL``, then UPDATE after the version
    lands). The domain layer writes the pointer atomically on version
    bump and guards against dangling refs — same pattern as
    ``task.current_evidence_id`` (§02).
    """

    __tablename__ = "instruction"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # URL-safe handle unique per workspace. The composite UNIQUE
    # ``(workspace_id, slug)`` enforces the invariant at the DB.
    slug: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    # The CHECK enum clamps the v1 scope taxonomy at the DB layer;
    # the domain layer validates the companion invariant (when
    # ``scope_kind = 'workspace'`` the scope_id must be NULL; for
    # every other kind a scope_id must be set) because SQLite's
    # CHECK dialect cannot portably express a column-dependent
    # condition against a nullable sibling.
    scope_kind: Mapped[str] = mapped_column(String, nullable=False)
    # Soft-ref :class:`str` — points at a template / property / area /
    # asset / stay / role id depending on ``scope_kind``. ``NULL``
    # when ``scope_kind = 'workspace'``. A hard FK would need to be
    # polymorphic, which SQLAlchemy does not express portably.
    scope_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft-ref :class:`str` — see the module docstring. Points at an
    # :class:`InstructionVersion.id`; written atomically on version
    # bump by the domain layer. No hard FK because the pair is
    # mutually dependent — hard-wiring would force a two-phase write.
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"scope_kind IN ({_in_clause(_SCOPE_KIND_VALUES)})",
            name="scope_kind",
        ),
        # Per-acceptance: a workspace cannot mint two instructions
        # with the same slug.
        UniqueConstraint(
            "workspace_id",
            "slug",
            name="uq_instruction_workspace_slug",
        ),
        # "Instructions that apply to this <scope>": the worker task
        # screen's hot path. Leading ``workspace_id`` lets the tenant
        # filter ride the same B-tree; ``scope_kind`` + ``scope_id``
        # carry the equality filter for the scope lookup.
        Index(
            "ix_instruction_workspace_scope",
            "workspace_id",
            "scope_kind",
            "scope_id",
        ),
    )


class InstructionVersion(Base):
    """An immutable snapshot of an instruction's body.

    Every edit mints a new ``InstructionVersion`` and the domain
    layer flips ``Instruction.current_version_id`` to point at the
    freshly-minted row. Versions are append-only: a version is never
    rewritten, only superseded.

    UNIQUE ``(instruction_id, version_num)`` enforces the monotonic
    version-number invariant: the same instruction cannot mint two
    v3 rows. CHECK ``version_num >= 1`` guards against the
    off-by-one bug of writing v0 on the first bump.

    ``body_md`` is a markdown string — empty strings are allowed
    (the first bump of a draft instruction that has no body yet) but
    unusual in practice.

    ``workspace_id`` is denormalised from the owning
    ``Instruction.workspace_id`` so the ORM tenant filter's injected
    predicate matches a local column rather than joining through
    the parent — matches the ``permission_group_member`` pattern in
    :mod:`app.adapters.db.authz`. The domain layer keeps the two
    columns consistent on insert; a divergence is a data bug the
    integration tests guard against.
    """

    __tablename__ = "instruction_version"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Denormalised workspace_id — see the class docstring. CASCADE
    # mirrors the parent Instruction's CASCADE so sweeping a
    # workspace drops every version row in lock-step.
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE — deleting an instruction drops every version with it.
    # Version rows are not independently useful once the parent is
    # gone.
    instruction_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("instruction.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Monotonic per instruction — first version is 1, next is 2, etc.
    # The CHECK clamps the off-by-one bug of writing v0.
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    # Markdown body. Empty allowed (a draft with no body yet); the
    # domain layer's render path treats empty as "no body" without
    # crashing.
    body_md: Mapped[str] = mapped_column(String, nullable=False)
    # Soft-ref :class:`str` — see the module docstring. Nullable
    # because a system-actor seed (a migration backfill, a capability
    # agent authoring from a template) has no user id to pin.
    author_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("version_num >= 1", name="version_num_positive"),
        # Per-acceptance: the same instruction cannot mint two v3
        # rows. The composite ``(instruction_id, version_num)`` is
        # also the natural key for "fetch the v3 row for this
        # instruction" reads.
        UniqueConstraint(
            "instruction_id",
            "version_num",
            name="uq_instruction_version_instruction_version_num",
        ),
    )
