"""llm_assignment_extras_cd_u84y

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-24 15:00:00.000000

Extends the cd-cm5 ``model_assignment`` table with the priority /
enabled / tuning columns the §11 router resolver depends on, and
creates the workspace-scoped ``llm_capability_inheritance`` edge
table. Lands ahead of cd-k0qf (the resolver) so the resolver has a
DB surface to read from.

Shape additions on ``model_assignment``:

* ``priority INT NOT NULL DEFAULT 0`` — sort key for the §11
  fallback chain. Lower = tried first; 0 = primary. CHECK
  ``priority >= 0`` is defensive; the reorder API keeps values
  dense (0, 1, 2, …).
* ``enabled BOOL NOT NULL DEFAULT true`` — paused-in-place flag.
  The §11 resolver skips ``enabled = false`` rows and, if every
  row for a capability is disabled, falls through to
  ``llm_capability_inheritance`` and then raises
  ``CapabilityUnassignedError``. ``sa.true()`` is the portable
  boolean literal (SQLite renders ``1``, PG renders ``TRUE``).
* ``max_tokens INT NULL`` / ``temperature FLOAT NULL`` — per-call
  tuning; NULL = inherit the provider-model / model default.
* ``extra_api_params JSON NOT NULL DEFAULT '{}'`` — extra provider-
  layer params, merged last over the provider-model defaults at
  call time. Bare-string ``server_default`` matches the sibling
  ``agent_token.scope_json`` / ``approval_request.action_json``
  pattern and round-trips on SQLite + PG.
* ``required_capabilities JSON NOT NULL DEFAULT '[]'`` — sub-
  capability tags (``vision``, ``json_mode``, …) the model must
  expose. Copied from the §11 capability catalogue on save.

Index changes on ``model_assignment``:

* Drops the cd-cm5 unique index
  ``uq_model_assignment_workspace_capability`` on
  ``(workspace_id, capability)``. A capability may now have many
  assignments — the unique was v1's "one row per capability" rule
  and has to go.
* Creates a non-unique composite index
  ``ix_model_assignment_workspace_capability_priority`` on
  ``(workspace_id, capability, priority)`` to back the §11
  resolver's sorted scan. The leading ``workspace_id`` carries the
  tenant filter; per-capability lookup still rides the
  ``(workspace_id, capability)`` prefix of the same index.

New table ``llm_capability_inheritance`` (workspace-scoped):

* ``id`` ULID PK (plain ``String``, matches the cd-yesa
  harmonisation).
* ``workspace_id`` FK ``workspace.id`` CASCADE NOT NULL — sweeping
  a workspace sweeps its override edges.
* ``capability`` / ``inherits_from`` — child / parent keys from the
  §11 catalogue. Both NOT NULL.
* ``created_at`` TZ-aware.
* CHECK ``ck_llm_capability_inheritance_no_self_loop``:
  ``capability <> inherits_from``. A self-loop is an obvious data
  bug; multi-hop cycle detection is a write-path concern (the
  admin / API layer that writes this table rejects
  ``422 capability_inheritance_cycle`` before insert).
* Unique
  ``uq_llm_capability_inheritance_workspace_capability`` on
  ``(workspace_id, capability)`` — one parent per child per
  workspace.

**Reversibility.** ``downgrade()`` sweeps ``model_assignment`` rows
whose ``priority > 0`` first — under cd-u84y a capability can carry
several rungs, but the pre-cd-u84y schema allows only one per
``(workspace_id, capability)``, so rolling back with duplicates in
place would collide with the restored unique index. The sweep
mirrors cd-i1qe's ``DELETE FROM api_token WHERE kind = 'personal'``
pattern (same class of rollback hazard). After the sweep the new
table is dropped and every column / index / CHECK change on
``model_assignment`` is reversed. The CHECK + new columns live
inside a single ``batch_alter_table`` so SQLite round-trips cleanly
(the schema-fingerprint gate keeps the upgrade → downgrade → upgrade
cycle honest on both backends).

See ``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md`` §"Model assignment" /
§"Capability inheritance" / §"Capability defaults (seeds)".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``model_assignment`` — drop the old unique, add columns, swap
    # to the composite priority index. Everything lives inside a
    # single ``batch_alter_table`` so SQLite materialises the add-
    # columns + CHECK through one table-copy rather than one per op.
    with op.batch_alter_table("model_assignment", schema=None) as batch_op:
        # Drop the v1 "one row per (workspace, capability)" rule.
        batch_op.drop_index("uq_model_assignment_workspace_capability")

        # Priority — sort key for the §11 fallback chain. 0 = primary.
        batch_op.add_column(
            sa.Column(
                "priority",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        # Paused-in-place flag. ``sa.true()`` renders ``1`` on SQLite
        # and ``TRUE`` on PG — the only portable way to spell a
        # boolean literal across both backends.
        batch_op.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        # Per-call caps. Nullable = inherit the provider-model default.
        batch_op.add_column(sa.Column("max_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("temperature", sa.Float(), nullable=True))
        # JSON blobs. Bare-string defaults match the sibling
        # ``scope_json`` / ``action_json`` pattern (cd-cm5, cd-pjm).
        batch_op.add_column(
            sa.Column(
                "extra_api_params",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "required_capabilities",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )

        # Defensive CHECK — a negative priority would silently sort
        # ahead of the primary and break every downstream reorder
        # invariant.
        batch_op.create_check_constraint(
            "priority_non_negative",
            "priority >= 0",
        )

        # Sorted-scan index for the §11 resolver's per-capability walk.
        # The leading ``workspace_id`` carries the tenant filter;
        # per-capability lookup still rides the ``(workspace_id,
        # capability)`` prefix of this same index.
        batch_op.create_index(
            "ix_model_assignment_workspace_capability_priority",
            ["workspace_id", "capability", "priority"],
            unique=False,
        )

    # ``llm_capability_inheritance`` — new table. Workspace-scoped
    # parent / child fallback edges. CASCADE on ``workspace_id`` so
    # sweeping a workspace sweeps its overrides.
    op.create_table(
        "llm_capability_inheritance",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("inherits_from", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "capability <> inherits_from",
            name=op.f("ck_llm_capability_inheritance_no_self_loop"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_llm_capability_inheritance_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_capability_inheritance")),
    )
    with op.batch_alter_table("llm_capability_inheritance", schema=None) as batch_op:
        # One parent per child per workspace — a second edge would
        # force the resolver to pick at random.
        batch_op.create_index(
            "uq_llm_capability_inheritance_workspace_capability",
            ["workspace_id", "capability"],
            unique=True,
        )


def downgrade() -> None:
    """Downgrade schema.

    FK-safe order: sweep rows that violate the pre-cd-u84y unique
    first, drop the new table, then reverse every change on
    ``model_assignment``. The ``batch_alter_table`` on the reverse
    path rebuilds the table without the cd-u84y columns on SQLite;
    on PG the individual ``ALTER TABLE`` steps run in sequence.

    **Data loss note.** cd-u84y allows many assignments per
    ``(workspace_id, capability)`` ordered by ``priority``; the
    pre-cd-u84y schema allowed one. Rolling back without sweeping
    would leave duplicates that collide with the restored unique
    index (silent failure on PG, CHECK-rebuild abort on SQLite).
    We mirror the cd-i1qe PAT sweep (``DELETE FROM api_token WHERE
    kind = 'personal'``): delete every row whose priority would
    violate the restored rule — i.e. anything past the ``priority=0``
    primary for each ``(workspace_id, capability)`` pair. The
    ``priority=0`` primary survives and round-trips cleanly.

    Operators planning a *real* rollback (rare — rollbacks on a dev
    DB are the common path) should dump ``model_assignment`` first;
    the fallback rungs are irrecoverable under the pre-cd-u84y
    schema by definition.
    """
    # Sweep fallback rungs (``priority > 0``) so the restored unique
    # index on ``(workspace_id, capability)`` lands cleanly. Raw
    # DELETE so the step works at the schema level without importing
    # an ORM that already knows the pre-cd-u84y shape. No-op when
    # the table is empty or every row is already a primary — this is
    # the happy path on a fresh dev DB.
    op.execute("DELETE FROM model_assignment WHERE priority > 0")

    with op.batch_alter_table("llm_capability_inheritance", schema=None) as batch_op:
        batch_op.drop_index("uq_llm_capability_inheritance_workspace_capability")
    op.drop_table("llm_capability_inheritance")

    with op.batch_alter_table("model_assignment", schema=None) as batch_op:
        # Reverse of the upgrade — indexes first, then CHECK, then
        # columns, then the restored v1 unique index.
        batch_op.drop_index("ix_model_assignment_workspace_capability_priority")
        # Short body matches the create-side convention (the naming
        # template ``ck_%(table_name)s_%(constraint_name)s`` is applied
        # at drop time too; passing the fully-qualified name would
        # double-prefix it).
        batch_op.drop_constraint(
            "priority_non_negative",
            type_="check",
        )
        batch_op.drop_column("required_capabilities")
        batch_op.drop_column("extra_api_params")
        batch_op.drop_column("temperature")
        batch_op.drop_column("max_tokens")
        batch_op.drop_column("enabled")
        batch_op.drop_column("priority")
        # Restore the v1 "one row per (workspace, capability)" rule.
        # The ``DELETE`` above swept every row that would violate it,
        # so the unique lands cleanly on every worktree.
        batch_op.create_index(
            "uq_model_assignment_workspace_capability",
            ["workspace_id", "capability"],
            unique=True,
        )
