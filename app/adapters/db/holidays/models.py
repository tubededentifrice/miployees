"""Public holiday model (cd-l2r9).

A workspace-managed holiday date with a configurable scheduling effect
and an optional payroll multiplier. Sits between
:mod:`app.adapters.db.availability` (per-user state) and the §09
payroll pipeline (the multiplier feeds shift / booking rate
computation).

**Tenancy.** :class:`PublicHoliday` is workspace-scoped — every row
carries its own ``workspace_id``. The package's ``__init__`` registers
the table so the ORM tenant filter auto-injects a ``workspace_id``
predicate on every SELECT / UPDATE / DELETE.

**Soft delete.** ``deleted_at`` carries the retirement timestamp; live
rows have ``NULL``. The live-list path filters
``WHERE deleted_at IS NULL`` at the service layer.

**CHECK invariants.**

* ``scheduling_effect`` ∈ ``block | allow | reduced`` — §06 records
  three behaviours; the CHECK rejects unknown values before the ORM
  sees them.
* ``recurrence`` ∈ ``annual`` (or NULL for one-off) — §06 lists only
  annual recurrence in v1; the CHECK leaves room for future values
  while pinning the v1 alphabet.
* **Reduced hours pairing.** ``reduced_starts_local`` and
  ``reduced_ends_local`` are both required when ``scheduling_effect
  = 'reduced'`` and both forbidden otherwise — same biconditional
  shape as :class:`~app.adapters.db.workspace.models.WorkEngagement`'s
  ``supplier_org_pairing`` rule and the BOTH-OR-NEITHER hours rule
  on :class:`~app.adapters.db.availability.models.UserWeeklyAvailability`.

**UNIQUE.** ``(workspace_id, date, country)`` — one holiday per date
per country per workspace. ``country = NULL`` is a separate unique
slot (workspace-wide entry); per the §06 spec a NULL-country holiday
applies to every property regardless of country, while a populated
``country`` narrows the match to users whose primary property sits
in that country.

NULL columns participate in UNIQUE differently across backends —
SQLite treats two NULLs as distinct (so two workspace-wide entries
for the same date may coexist on SQLite), Postgres also treats NULLs
as distinct under default semantics. The domain layer (cd-rd68
holidays HTTP router) is responsible for "one workspace-wide entry
per (workspace, date)" — a partial UNIQUE on ``WHERE country IS
NULL`` is left for the follow-up that lands the router.

**Hot-path indexes.**

* ``(workspace_id, date)`` — "what holidays fall on this date in
  this workspace?" candidate-pool walk for the §06 availability
  stack.
* ``(workspace_id, deleted_at)`` — live-list filter for the manager
  configuration screen.

See ``docs/specs/06-tasks-and-scheduling.md`` §"public_holidays";
``docs/specs/02-domain-model.md`` §"Work" entity list;
``docs/specs/09-time-payroll-expenses.md`` §"Holiday multiplier"
(consumer of the payroll multiplier).
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
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

__all__ = ["PublicHoliday"]


# Allowed ``scheduling_effect`` values, enforced by a CHECK constraint.
# Matches §06 "public_holidays":
#   * ``block`` — employee unavailable for the whole day.
#   * ``allow`` — no scheduling impact (payroll/display only).
#   * ``reduced`` — employee available only during
#     ``reduced_starts_local..reduced_ends_local``.
_SCHEDULING_EFFECT_VALUES: tuple[str, ...] = ("block", "allow", "reduced")

# Allowed ``recurrence`` values, enforced by a CHECK constraint
# alongside NULL. NULL = one-off date; ``annual`` = (month, day) tuple
# match every year (§06 "Annual recurrence").
_RECURRENCE_VALUES: tuple[str, ...] = ("annual",)


class PublicHoliday(Base):
    """Workspace-managed holiday date (§06 "public_holidays").

    Each row declares one calendar date (or one annual anchor — see
    ``recurrence``) plus its scheduling effect and payroll multiplier.
    Managers configure each holiday's scheduling impact individually;
    the §06 availability precedence stack consults rows whose date /
    annual match the candidate occurrence.

    **Country matching.** ``country IS NULL`` makes the holiday
    workspace-wide; a populated ``country`` narrows to users whose
    primary property sits in that ISO-3166-1 alpha-2 country (§06
    "Availability precedence stack").

    **Reduced hours.** ``reduced_starts_local`` and
    ``reduced_ends_local`` are required iff ``scheduling_effect =
    'reduced'`` (CHECK enforced).

    **Payroll multiplier.** ``Numeric(5, 2)`` keeps Decimal precision
    cleanly across SQLite (TEXT) and PG (numeric); the §09 pay
    pipeline multiplies the worked-hours cost by this column when the
    holiday's date overlaps. NULL = no multiplier (the default —
    holidays only affect scheduling unless a multiplier is set).

    **Recurrence.** NULL = one-off; ``annual`` = (month, day) match
    every year. The §06 "Annual recurrence" section spells out the
    matching rule the resolver applies. The CHECK admits NULL or
    ``annual`` so a future spec extension (e.g. ``monthly``,
    ``weekly``) can land via a widened CHECK migration without a
    column rename.

    Registered as workspace-scoped in the package's ``__init__``;
    every SELECT auto-filters on ``workspace_id`` through the ORM
    tenant filter.

    See ``docs/specs/06-tasks-and-scheduling.md`` §"public_holidays".
    """

    __tablename__ = "public_holiday"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    # ISO-3166-1 alpha-2. NULL = workspace-wide entry.
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduling_effect: Mapped[str] = mapped_column(String, nullable=False)
    reduced_starts_local: Mapped[time | None] = mapped_column(Time, nullable=True)
    reduced_ends_local: Mapped[time | None] = mapped_column(Time, nullable=True)
    # ``Numeric(5, 2)`` — covers values like ``1.00``, ``1.50``,
    # ``2.00`` cleanly (the canonical "double pay" ceiling sits at
    # ``2.00``; ``999.99`` is the column ceiling, well above any
    # realistic multiplier).
    payroll_multiplier: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    recurrence: Mapped[str | None] = mapped_column(String, nullable=True)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
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
            "scheduling_effect IN ('" + "', '".join(_SCHEDULING_EFFECT_VALUES) + "')",
            name="scheduling_effect",
        ),
        # NULL-or-enum check. NULL = one-off; ``annual`` is the only
        # recurrence value in the v1 spec. Widening here is a CHECK
        # swap, not a column rename.
        CheckConstraint(
            "recurrence IS NULL OR recurrence IN ('"
            + "', '".join(_RECURRENCE_VALUES)
            + "')",
            name="recurrence",
        ),
        # Biconditional: reduced-hours columns are populated iff
        # ``scheduling_effect = 'reduced'``. Same shape as the
        # ``user_weekly_availability.hours_pairing`` /
        # ``work_engagement.supplier_org_pairing`` biconditionals.
        CheckConstraint(
            "(scheduling_effect = 'reduced' "
            "AND reduced_starts_local IS NOT NULL "
            "AND reduced_ends_local IS NOT NULL) "
            "OR (scheduling_effect != 'reduced' "
            "AND reduced_starts_local IS NULL "
            "AND reduced_ends_local IS NULL)",
            name="reduced_hours_pairing",
        ),
        # §06 "public_holidays" invariant: one row per
        # (workspace, date, country). NULL-country holidays are
        # handled by the domain layer (see module docstring).
        UniqueConstraint(
            "workspace_id",
            "date",
            "country",
            name="uq_public_holiday_workspace_date_country",
        ),
        # "What holidays fall on this date in this workspace?" —
        # candidate-pool walk for the §06 availability stack.
        Index(
            "ix_public_holiday_workspace_date",
            "workspace_id",
            "date",
        ),
        # "List live holidays for this workspace" hot path. Leading
        # ``workspace_id`` carries the tenant filter; trailing
        # ``deleted_at`` lets the planner skip tombstones without a
        # second pass — same idiom as ``ix_work_role_workspace_deleted``.
        Index(
            "ix_public_holiday_workspace_deleted",
            "workspace_id",
            "deleted_at",
        ),
    )
