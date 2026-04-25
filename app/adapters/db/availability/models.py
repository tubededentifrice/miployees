"""User leave / weekly availability / availability override models (cd-l2r9).

Three sibling rows describing **when a user is willing to work**, all
keyed by ``(workspace_id, user_id)``. Together they feed the
availability precedence stack in §06 "Availability precedence stack":

1. Approved :class:`UserLeave` (one-off date range) blocks the user
   for every covered date.
2. Approved :class:`UserAvailabilityOverride` (single date) substitutes
   for the weekly pattern on that date — adding work, removing work,
   or shifting hours.
3. :class:`UserWeeklyAvailability` (one row per (user, weekday)) is
   the standing pattern. ``starts_local`` and ``ends_local`` are
   either both set or both ``NULL``; a null pair means "off that
   weekday".

The :class:`PublicHoliday` row (the fourth sibling in this slice)
lives in :mod:`app.adapters.db.holidays.models` because its FK target
and write authority are workspace-managed config rather than per-user
state.

**Tenancy.** All three rows are workspace-scoped — their
``workspace_id`` carries the tenant filter (registered in this
package's ``__init__``). ``user_id`` is a **soft reference** to
``users.id`` until the broader tenancy-join refactor lands, matching
the sibling :class:`~app.adapters.db.workspace.models.WorkEngagement`
/ :class:`~app.adapters.db.workspace.models.UserWorkRole` pattern.

**Soft delete.** :class:`UserLeave` and :class:`UserAvailabilityOverride`
carry a ``deleted_at`` tombstone — a worker may withdraw a leave or
override request, and the audit trail preserves the row.
:class:`UserWeeklyAvailability` does **not** carry ``deleted_at`` —
there is exactly one live row per (workspace, user, weekday); editing
the pattern overwrites the existing row in place.

**CHECK invariants.**

* :class:`UserWeeklyAvailability` and
  :class:`UserAvailabilityOverride` both enforce
  "``starts_local`` and ``ends_local`` are either both set or both
  null" — the BOTH-OR-NEITHER invariant from §06 "Weekly availability"
  / §06 "user_availability_overrides".
* :class:`UserWeeklyAvailability` enforces ``weekday`` ∈ ``0..6``
  (ISO Mon..Sun) at the column level so a malformed write is a DB
  error, not a domain error.
* :class:`UserLeave` enforces ``ends_on >= starts_on`` — a leave with
  a backwards range is a data bug. ``category`` is enum-checked
  (``vacation | sick | personal | bereavement | other``).

**UNIQUE constraints.**

* :class:`UserAvailabilityOverride`: ``UNIQUE(workspace_id, user_id,
  date)`` — one override per user per date (§06).
* :class:`UserWeeklyAvailability`: ``UNIQUE(workspace_id, user_id,
  weekday)`` — one row per user per weekday.

**Hot-path indexes.** Each table indexes
``(workspace_id, user_id)`` so the candidate-pool path (§06) walks a
local index when the assignment algorithm checks "is this user
available?".

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"Weekly availability", §"user_availability_overrides";
``docs/specs/02-domain-model.md`` §"Work" entity list;
``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment" (consumer of the rota composition).
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``workspace.id`` FKs below
# resolve against ``Base.metadata`` only if ``workspace.models`` has
# been imported, so we register it here as a side effect.
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "UserAvailabilityOverride",
    "UserLeave",
    "UserWeeklyAvailability",
]


# Allowed ``user_leave.category`` values, enforced by a CHECK
# constraint. Matches §06 "user_leave" — five buckets the
# self-service form exposes; ``other`` keeps the column open without
# inviting free-text drift.
_LEAVE_CATEGORY_VALUES: tuple[str, ...] = (
    "vacation",
    "sick",
    "personal",
    "bereavement",
    "other",
)


class UserLeave(Base):
    """Per-user one-off leave row (§06 "user_leave").

    Self-submitted by a worker (``approved_at IS NULL``) or
    owner/manager-created (``approved_at`` populated at insert time).
    Pending leaves do **not** affect assignment — only approved ones
    enter the availability precedence stack.

    **Soft delete.** A withdrawn leave is tombstoned via
    ``deleted_at`` rather than hard-deleted so the audit trail keeps
    the request visible. The live-list path filters
    ``WHERE deleted_at IS NULL`` at the service layer.

    **CHECK: range validity.** ``ends_on >= starts_on`` — same-day
    leaves are valid (``starts_on = ends_on``), backwards ranges are
    a data bug.

    **CHECK: category enum.** Five buckets matching the self-service
    form. ``other`` keeps the column open without inviting free-text
    drift.

    **Soft references.** ``user_id`` and ``approved_by`` are plain
    :class:`str` columns rather than :class:`~sqlalchemy.ForeignKey`
    relations because the broader tenancy-join refactor has not
    promoted ``users.id`` into a real FK target across the schema yet
    (sibling pattern with ``user_workspace.user_id`` /
    ``work_engagement.user_id``). The column names and nullability
    stay stable so domain callers are undisturbed.

    **Hot-path index.** ``(workspace_id, user_id)`` backs the
    "what leaves does this user hold here?" candidate-pool walk in
    §06's assignment algorithm.

    Registered as workspace-scoped in the package's ``__init__``;
    every SELECT auto-filters on ``workspace_id`` through the ORM
    tenant filter.

    See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave".
    """

    __tablename__ = "user_leave"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft reference — see class docstring.
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    # Approval state — null until an owner/manager approves. The
    # write boundary in the (forthcoming) leaves domain service
    # (cd-oydd) sets ``approved_at`` + ``approved_by`` together.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # Worker-provided context. Markdown body; the agent inbox / digest
    # surfaces honour the rendering convention.
    note_md: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "category IN ('" + "', '".join(_LEAVE_CATEGORY_VALUES) + "')",
            name="category",
        ),
        # Same-day leaves are valid; backwards ranges are a data bug.
        CheckConstraint("ends_on >= starts_on", name="range"),
        # "What leaves does this user hold in this workspace?" — the
        # candidate-pool path walks this index when checking
        # availability. Leading ``workspace_id`` carries the tenant
        # filter.
        Index(
            "ix_user_leave_workspace_user",
            "workspace_id",
            "user_id",
        ),
    )


class UserWeeklyAvailability(Base):
    """Standing weekly pattern — one row per (workspace, user, weekday).

    Authoritative for the assignment algorithm: a user is a candidate
    for a task only if the occurrence's local start time falls inside
    their weekly window for that weekday (§06 "Weekly availability").

    **No soft delete.** Editing the pattern overwrites the existing
    row in place — there is exactly one live row per (workspace, user,
    weekday). The ``UNIQUE(workspace_id, user_id, weekday)``
    constraint pins this invariant.

    **CHECK: weekday range.** ``weekday`` ∈ ``0..6`` (ISO Mon..Sun).
    A bad value is a data bug; the CHECK rejects it before the ORM
    sees it.

    **CHECK: BOTH-OR-NEITHER hours.** ``starts_local`` and
    ``ends_local`` are either both set or both ``NULL``. A null pair
    means "off that weekday"; a half-set pair is a half-wired
    pattern — same biconditional shape as
    :class:`~app.adapters.db.workspace.models.WorkEngagement.supplier_org_id`'s
    pairing rule.

    **Soft references.** ``user_id`` is a plain :class:`str` column;
    see :class:`UserLeave` for the rationale.

    **Hot-path indexes.** The ``UNIQUE(workspace_id, user_id, weekday)``
    constraint already supports the "what's this user's pattern for
    weekday N?" lookup; an additional ``(workspace_id, user_id)``
    index backs "what's this user's full week?" without scanning the
    composite UNIQUE for every weekday (the leading ``workspace_id``
    + ``user_id`` is enough to drive a per-user range scan).

    Registered as workspace-scoped in the package's ``__init__``.

    See ``docs/specs/06-tasks-and-scheduling.md`` §"Weekly availability".
    """

    __tablename__ = "user_weekly_availability"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft reference — see :class:`UserLeave` rationale.
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    # ISO weekday, ``0..6`` (Mon..Sun). The CHECK enforces the range.
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_local: Mapped[time | None] = mapped_column(Time, nullable=True)
    ends_local: Mapped[time | None] = mapped_column(Time, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # ISO weekday guard — Mon..Sun.
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday_range"),
        # BOTH-OR-NEITHER: ``starts_local`` and ``ends_local`` either
        # both set or both NULL. A half-set pair is a half-wired
        # pattern; the biconditional rejects both half-shapes.
        CheckConstraint(
            "(starts_local IS NULL AND ends_local IS NULL) "
            "OR (starts_local IS NOT NULL AND ends_local IS NOT NULL)",
            name="hours_pairing",
        ),
        # One live row per (workspace, user, weekday). No soft delete
        # — edits overwrite the existing row.
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "weekday",
            name="uq_user_weekly_availability_user_weekday",
        ),
        # "What's this user's full week?" — the worker /schedule view
        # scans the seven rows under one tenant-local prefix.
        Index(
            "ix_user_weekly_availability_workspace_user",
            "workspace_id",
            "user_id",
        ),
    )


class UserAvailabilityOverride(Base):
    """Date-specific override of a user's weekly availability pattern (§06).

    Adding availability is self-service (auto-approved on create);
    reducing availability requires owner/manager approval. The
    ``approval_required`` boolean is computed at the write boundary
    by the (forthcoming) overrides domain service (cd-uqw1) per §06's
    "Approval logic (hybrid model)" table.

    **Soft delete.** A withdrawn override is tombstoned via
    ``deleted_at`` rather than hard-deleted so the audit trail keeps
    the request visible. The live-list path filters
    ``WHERE deleted_at IS NULL`` at the service layer.

    **CHECK: BOTH-OR-NEITHER hours.** Same biconditional as
    :class:`UserWeeklyAvailability`: ``starts_local`` and
    ``ends_local`` either both set or both ``NULL``. When both are
    null on an ``available = true`` row, the assignment algorithm
    falls back to the weekly pattern's hours (§06).

    **UNIQUE.** ``(workspace_id, user_id, date)`` — one override per
    user per date. Withdrawing an override and re-submitting on the
    same date overwrites the same row (the service layer flips
    ``deleted_at`` back to ``NULL`` rather than minting a new row).

    **Soft references.** ``user_id`` and ``approved_by`` are plain
    :class:`str`; see :class:`UserLeave` for the rationale.

    **Hot-path index.** ``(workspace_id, user_id)`` backs the
    "what overrides does this user hold here?" candidate-pool walk;
    the ``UNIQUE(workspace_id, user_id, date)`` index also satisfies
    "is this user overridden on this date?" via its prefix. The
    explicit non-unique sibling index keeps parity with
    :class:`UserLeave` / :class:`UserWeeklyAvailability` so every
    availability table walks the same shape.

    Registered as workspace-scoped in the package's ``__init__``.

    See ``docs/specs/06-tasks-and-scheduling.md`` §"user_availability_overrides".
    """

    __tablename__ = "user_availability_override"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft reference — see :class:`UserLeave` rationale.
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    starts_local: Mapped[time | None] = mapped_column(Time, nullable=True)
    ends_local: Mapped[time | None] = mapped_column(Time, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # Computed at write time — the cd-uqw1 service walks the §06
    # approval-logic table to fill this. The DB stores it explicitly
    # so the audit log can replay "did this override need approval?"
    # without re-running the resolver.
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # BOTH-OR-NEITHER — same biconditional as the weekly pattern.
        CheckConstraint(
            "(starts_local IS NULL AND ends_local IS NULL) "
            "OR (starts_local IS NOT NULL AND ends_local IS NOT NULL)",
            name="hours_pairing",
        ),
        # One override per (workspace, user, date) — §06 invariant.
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "date",
            name="uq_user_availability_override_user_date",
        ),
        # "What overrides does this user hold here?" candidate-pool
        # walk. Leading ``workspace_id`` carries the tenant filter.
        Index(
            "ix_user_availability_override_workspace_user",
            "workspace_id",
            "user_id",
        ),
    )
