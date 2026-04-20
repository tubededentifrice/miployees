"""Shift / Leave / GeofenceSetting SQLAlchemy models.

v1 slice per cd-8yn — sufficient for the clock-in / clock-out (cd-whl)
and leave-request (cd-31c) follow-ups to layer business rules on top.
The richer §02 / §09 surface (``work_engagement_id`` on shift, the
``booking`` supersession story, a proper state-machine on leave,
``property_workspace`` join keys on geofence) lands with those
follow-ups without breaking this migration's public write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
the rest of the app:

* ``workspace_id`` cascades on delete — sweeping a workspace sweeps
  its time history (the §15 tombstone / export worker snapshots
  first).
* ``user_id`` uses ``RESTRICT`` on delete — a shift or leave row
  carries labour-law-compliance weight (§09 §"Labour-law
  compliance"), so a raw ``DELETE FROM user`` must not silently
  take the evidence with it. The normal erasure path is
  ``crewday admin purge --person`` (§15 "Right to erasure") which
  anonymises the user row in place and keeps historical
  ``user_id`` references valid; hard-deleting a user is an
  identity-layer op that must first purge or reassign these rows.
  ``SET NULL`` would match the sibling :mod:`app.adapters.db.tasks`
  pattern but the column is NOT NULL here — a shift or leave row
  without a worker is not a meaningful artefact — and nulling
  would need an owning reason beyond "the user went away".
* ``property_id`` stays a plain :class:`str` — soft-ref only. The §02
  places / §05 work-engagement intersection owns when that becomes
  a hard FK; tying it here now would either pin us to the current
  ``property_workspace`` shape or force a backfill when it moves.
* ``approved_by`` / ``decided_by`` are plain :class:`str` too — they
  point at a user id, but the approver is not always a user (could
  be a system actor per the §01 ``actor_kind`` taxonomy), and the
  audit-trail semantics live in :mod:`app.adapters.db.audit`, not
  here.

See ``docs/specs/02-domain-model.md`` §"shift", §"leave",
§"geofence_setting", and ``docs/specs/09-time-payroll-expenses.md``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["GeofenceSetting", "Leave", "Shift"]


# Allowed ``shift.source`` values — how the row was captured.
# ``manual`` is a manager-entered edit, ``geofence`` is automated
# from the geofence enter / exit events, ``occurrence`` is derived
# from completed §06 occurrences when the clock-state bridge fires.
_SHIFT_SOURCE_VALUES: tuple[str, ...] = ("manual", "geofence", "occurrence")

# Allowed ``leave.kind`` values — the v1 slice. §02 §"Enums" lists a
# richer ``leave_category`` (``vacation | sick | personal |
# bereavement | other``); the narrower set here matches cd-8yn's
# explicit scope and lets cd-31c widen it without rewriting history.
_LEAVE_KIND_VALUES: tuple[str, ...] = ("vacation", "sick", "comp", "other")

# Allowed ``leave.status`` values — the v1 state machine. The
# richer transition table (transitions + guards) lands with cd-31c.
_LEAVE_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "cancelled",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``tasks`` / ``stays`` / ``places``
    modules so the enum CHECK constraints below stay readable.
    """
    return "'" + "', '".join(values) + "'"


class Shift(Base):
    """A worked interval — either closed (``ends_at`` set) or open.

    The v1 slice carries the minimum the clock-in / clock-out
    follow-up (cd-whl) needs: the user + workspace the shift belongs
    to, the open / close timestamps, the capture source enum, a
    nullable property pointer, optional notes, and the manager
    approval marker pair. An open shift is the single row where
    ``ends_at IS NULL``; the ``(user_id, ends_at)`` index answers
    "does this user have an open shift?" with an index-only scan on
    both Postgres and SQLite.

    The ``(workspace_id, starts_at)`` index powers the manager's
    rota view ("every shift in the last 7 days for this workspace")
    and the payroll worker's per-period sweep.
    """

    __tablename__ = "shift"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``RESTRICT`` preserves labour-law-compliance records (§09) —
    # a hard ``DELETE FROM user`` is blocked while shifts remain,
    # forcing the caller through ``crewday admin purge --person``
    # (§15) or an explicit reassignment first.
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ``NULL`` means the shift is still open — the worker clocked in
    # but hasn't clocked out yet. The ``(user_id, ends_at)`` index
    # makes the "is there an open shift for this user?" check cheap.
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft-ref :class:`str` — the domain layer resolves it against
    # :mod:`app.adapters.db.places.models` once §05's
    # ``property_workspace`` intersection settles. Nullable because
    # a manager-entered manual shift (e.g. "driver ran airport
    # pickups") may not pin to a single property.
    property_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft-ref :class:`str` — see the module docstring. ``NULL``
    # until a manager approves the shift; once set, the ``approved_at``
    # column is the authoritative wall-clock for payroll.
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"source IN ({_in_clause(_SHIFT_SOURCE_VALUES)})",
            name="source",
        ),
        # Per-acceptance: "is this user's shift still open?" Index is
        # ``(user_id, ends_at)`` so the equality on ``user_id`` plus
        # the ``IS NULL`` on ``ends_at`` both ride the same B-tree.
        Index("ix_shift_user_ends_at", "user_id", "ends_at"),
        # Manager rota sweep: "every shift in this workspace in a
        # starts_at range". Leading ``workspace_id`` lets the tenant
        # filter's equality predicate ride the same B-tree.
        Index("ix_shift_workspace_starts", "workspace_id", "starts_at"),
    )


class Leave(Base):
    """A manager-approved absence request.

    The v1 slice carries the minimum the leave-request follow-up
    (cd-31c) needs: the user requesting the leave, the
    ``kind`` / ``status`` enums, the requested window, an optional
    markdown reason, and the decided-by / decided-at pair for the
    manager's approval audit. The CHECK on ``ends_at > starts_at``
    guards against zero-or-negative windows (a half-day leave is
    still positive on the wall-clock).

    The ``(workspace_id, status)`` index powers the manager's
    "pending leave requests" inbox.
    """

    __tablename__ = "leave"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``RESTRICT`` — see :class:`Shift`. Approved leave carries
    # payroll + labour-law weight; losing the requesting user via
    # an unqualified ``DELETE FROM user`` would silently corrupt
    # the record.
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    reason_md: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft-ref :class:`str` — see the module docstring. Null until
    # a manager decides the request; ``decided_at`` wall-clocks the
    # transition for audit.
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_LEAVE_KIND_VALUES)})",
            name="kind",
        ),
        CheckConstraint(
            f"status IN ({_in_clause(_LEAVE_STATUS_VALUES)})",
            name="status",
        ),
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        # Per-acceptance: manager inbox — "pending leave requests for
        # this workspace" rides the workspace-scoped index's equality
        # on both columns.
        Index("ix_leave_workspace_status", "workspace_id", "status"),
    )


class GeofenceSetting(Base):
    """Per-property geofence configuration.

    One row per ``(workspace_id, property_id)`` pair — the
    ``UNIQUE`` composite enforces the invariant. The v1 slice
    captures the centre (``lat`` / ``lon``) + ``radius_m`` radius
    plus the ``enabled`` kill switch; the geofence poller (cd-whl
    and successors) reads this row to decide whether a tracked user
    is on-site.

    CHECK constraints enforce the coordinate bounds (``-90 ≤ lat ≤
    90``, ``-180 ≤ lon ≤ 180``) and the positivity of ``radius_m``.
    Keeping them at the DB lets even a poorly-validated write path
    (a future import script, a raw ``UPDATE``) not corrupt the
    field; the domain layer re-validates before write anyway.

    ``property_id`` stays a plain :class:`str` (soft-ref) for the
    same reason as :class:`Shift` — the §05 /
    ``property_workspace`` intersection owns FK promotion.
    """

    __tablename__ = "geofence_setting"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft-ref :class:`str` — see the module docstring. NOT NULL
    # because a geofence without a target property is meaningless;
    # the UNIQUE composite then pins one row per (workspace,
    # property) pair.
    property_id: Mapped[str] = mapped_column(String, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("radius_m > 0", name="radius_m_positive"),
        CheckConstraint("lat BETWEEN -90 AND 90", name="lat_bounds"),
        CheckConstraint("lon BETWEEN -180 AND 180", name="lon_bounds"),
        UniqueConstraint(
            "workspace_id",
            "property_id",
            name="uq_geofence_setting_workspace_property",
        ),
    )
