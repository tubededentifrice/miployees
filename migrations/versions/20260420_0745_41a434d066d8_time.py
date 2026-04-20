"""time

Revision ID: 41a434d066d8
Revises: eee32ef09c5d
Create Date: 2026-04-20 07:45:22.231949

Creates the three time-context tables that back the clock-in /
clock-out + leave-request + per-property geofence flows (see
``docs/specs/02-domain-model.md`` §"shift", §"leave",
§"geofence_setting" and ``docs/specs/09-time-payroll-expenses.md``):

* ``shift`` — a worked interval, open (``ends_at IS NULL``) or
  closed. ``source`` CHECK enforces the v1 capture enum (``manual |
  geofence | occurrence``). Registered as workspace-scoped via
  ``app/adapters/db/time/__init__.py`` so the ORM tenant filter
  auto-injects a ``workspace_id`` predicate. ``workspace_id`` FK
  cascades — sweeping a workspace sweeps its time history (§15
  export snapshots first). ``user_id`` FK uses ``RESTRICT`` to
  preserve labour-law records (§09 §"Labour-law compliance"); the
  normal erasure path is ``crewday admin purge --person`` (§15)
  which anonymises the user in place, keeping references valid.
  ``property_id``, ``approved_by`` are plain :class:`str` soft-refs;
  FK promotion waits on the §05 / ``property_workspace``
  intersection (see the model docstring). The
  ``(user_id, ends_at)`` index makes the open-shift scan cheap; the
  ``(workspace_id, starts_at)`` index powers the rota / payroll
  sweep.

* ``leave`` — a manager-approved absence. ``kind`` CHECK
  (``vacation | sick | comp | other``) + ``status`` CHECK
  (``pending | approved | rejected | cancelled``) land the v1
  state machine; cd-31c widens the ``kind`` set and the transition
  rules without a rewrite. ``ends_at > starts_at`` guards against
  zero-or-negative windows. ``user_id`` FK uses ``RESTRICT`` for
  the same §09 / §15 reason as ``shift``. ``decided_by`` is a
  soft-ref :class:`str` — same rationale as ``shift.approved_by``.
  The ``(workspace_id, status)`` index powers the "pending leave
  requests" inbox.

* ``geofence_setting`` — per-property centre + radius + kill
  switch. UNIQUE ``(workspace_id, property_id)`` enforces one
  configuration per (workspace, property) pair — the acceptance
  criterion from cd-8yn. CHECK constraints clamp the coordinate
  fields (``-90 ≤ lat ≤ 90``, ``-180 ≤ lon ≤ 180``) and the
  positivity of ``radius_m`` so even a poorly-validated write
  path (a future import script, a raw ``UPDATE``) can't corrupt
  the geometry.

All three tables are workspace-scoped. Tables are created in a
stable deterministic order matching the declaration order in
:mod:`app.adapters.db.time.models`; ``downgrade()`` drops in
reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "41a434d066d8"
down_revision: str | Sequence[str] | None = "eee32ef09c5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "shift",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("property_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('manual', 'geofence', 'occurrence')",
            name=op.f("ck_shift_source"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_shift_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_shift_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shift")),
    )
    with op.batch_alter_table("shift", schema=None) as batch_op:
        batch_op.create_index(
            "ix_shift_user_ends_at", ["user_id", "ends_at"], unique=False
        )
        batch_op.create_index(
            "ix_shift_workspace_starts",
            ["workspace_id", "starts_at"],
            unique=False,
        )

    op.create_table(
        "leave",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason_md", sa.String(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ends_at > starts_at",
            name=op.f("ck_leave_ends_after_starts"),
        ),
        sa.CheckConstraint(
            "kind IN ('vacation', 'sick', 'comp', 'other')",
            name=op.f("ck_leave_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_leave_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_leave_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_leave_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave")),
    )
    with op.batch_alter_table("leave", schema=None) as batch_op:
        batch_op.create_index(
            "ix_leave_workspace_status",
            ["workspace_id", "status"],
            unique=False,
        )

    op.create_table(
        "geofence_setting",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("radius_m", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "lat BETWEEN -90 AND 90",
            name=op.f("ck_geofence_setting_lat_bounds"),
        ),
        sa.CheckConstraint(
            "lon BETWEEN -180 AND 180",
            name=op.f("ck_geofence_setting_lon_bounds"),
        ),
        sa.CheckConstraint(
            "radius_m > 0",
            name=op.f("ck_geofence_setting_radius_m_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_geofence_setting_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geofence_setting")),
        sa.UniqueConstraint(
            "workspace_id",
            "property_id",
            name="uq_geofence_setting_workspace_property",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("geofence_setting")

    with op.batch_alter_table("leave", schema=None) as batch_op:
        batch_op.drop_index("ix_leave_workspace_status")
    op.drop_table("leave")

    with op.batch_alter_table("shift", schema=None) as batch_op:
        batch_op.drop_index("ix_shift_workspace_starts")
        batch_op.drop_index("ix_shift_user_ends_at")
    op.drop_table("shift")
