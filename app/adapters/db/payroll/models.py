"""PayRule / PayPeriod / Payslip SQLAlchemy models.

v1 slice per cd-a3w — sufficient for the period-close + payslip-issue
follow-ups to layer the business rules on top. The richer §02 / §09
surface (``pay_rule.kind`` enum with per-kind rate columns,
``pay_period_entry`` per-day line rows, ``payslip.status`` state
machine, ``payout_snapshot_json``, ``components_json``, PDF
rendering, ``email_delivery_id``, jurisdiction / locale, etc.) lands
with those follow-ups without breaking this migration's public
write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
:mod:`app.adapters.db.time` — same shape, same rationale:

* ``workspace_id`` cascades on delete — sweeping a workspace sweeps
  its payroll artefacts (the §15 tombstone / export worker
  snapshots first).
* ``user_id`` uses ``RESTRICT`` on delete — pay_rule rows set the
  hourly rate a payslip is computed from and payslip rows are the
  payroll-law evidence (§09 §"Labour-law compliance", §15 §"Right
  to erasure"). A raw ``DELETE FROM user`` must not silently take
  that evidence with it. The normal erasure path is
  ``crewday admin purge --person`` (§15) which anonymises the user
  row in place and keeps historical ``user_id`` references valid.
* ``pay_period_id`` on ``payslip`` cascades — deleting a period
  (only legal while ``state = 'open'``, enforced at the domain
  layer) sweeps every draft payslip for that period in one go.
* ``created_by`` / ``locked_by`` are plain :class:`str` soft-refs —
  same rationale as ``shift.approved_by`` / ``leave.decided_by``:
  the actor may be a system process rather than a user (the
  period-close worker, a scheduled billing job), and audit-trail
  semantics live in :mod:`app.adapters.db.audit`, not here.

**App-layer invariant:** ``payslip.net_cents`` must equal
``gross_cents - sum(deductions_cents.values())``. SQLite's CHECK
dialect cannot evaluate a JSON-aggregated expression portably so
the rule is enforced in the domain layer (the payroll closer's
snapshot path) rather than at the DB. Integration tests cover the
happy path; a drift here is a data bug, not a schema bug.

See ``docs/specs/02-domain-model.md`` §"pay_rule", §"pay_period",
§"payslip", and ``docs/specs/09-time-payroll-expenses.md`` §"Pay
rules", §"Pay period", §"Payslip".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``user.id`` / ``workspace.id``
# FKs below resolve against ``Base.metadata`` only if the target
# packages have been imported, so we register them here as a side
# effect.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["PayPeriod", "PayRule", "PayoutDestination", "Payslip"]


# Allowed ``pay_period.state`` values — the v1 lifecycle matching
# the canonical ``pay_period_status`` enum in §02 §"Enums" and §09
# §"Pay period": ``open | locked | paid``. ``open`` (the worker is
# still accumulating hours) -> ``locked`` (period is closed, all
# payslips drafted, no new hours accrue) -> ``paid`` (every payslip
# marked paid, flipped by the payslip-paid trigger in the domain
# layer — see §09 §"Transition to paid").
_PAY_PERIOD_STATE_VALUES: tuple[str, ...] = ("open", "locked", "paid")


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``time`` / ``tasks`` / ``stays`` /
    ``places`` modules so the enum CHECK constraints below stay
    readable.
    """
    return "'" + "', '".join(values) + "'"


class PayRule(Base):
    """The hourly rate + overtime / night / weekend multipliers for a user.

    A pay rule binds a ``(workspace, user)`` pair to a monetary rate
    for a wall-clock interval. The v1 slice carries the minimum the
    period-close worker needs: currency (ISO-4217), base cents per
    hour, three multipliers, effective_from / effective_to window,
    and the usual audit pair (``created_by`` / ``created_at``).

    Multiple overlapping rules are resolved at the domain layer: the
    rule with the greatest ``effective_from ≤ period.ends_at`` and
    ``(effective_to IS NULL OR effective_to ≥ period.starts_at)``
    wins. The composite ``(workspace_id, user_id, effective_from)``
    index rides the equality on the first two columns plus the
    MAX() on the third.

    The richer §09 pay-rule surface (the ``kind`` enum,
    ``piecework_json``, ``holiday_rule_json``, ``monthly_cents``, …)
    lands with the period-close domain follow-up; the v1 slice is a
    straight hourly rate + three multipliers, enough to compute a
    payslip end-to-end without the full rule library.
    """

    __tablename__ = "pay_rule"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``RESTRICT`` — see the module docstring. A pay_rule row is a
    # payroll-law anchor (§09 "Labour-law compliance"); hard-deleting
    # the target user would silently disconnect every payslip that
    # cited this rate.
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # ISO-4217 currency code — stored as a 3-char text column with a
    # CHECK on length. The §02 §"Money" cascade treats this column as
    # the per-row currency override (workspace.default_currency →
    # property.default_currency → pay_rule.currency); the enum of
    # known codes lives in the domain layer so a fresh jurisdiction
    # can be added without a migration.
    currency: Mapped[str] = mapped_column(String, nullable=False)
    # Integer cents per hour (§02 "Money"). ``BigInteger`` because the
    # minor-unit count is currency-dependent (BHD is 3dp) and a very
    # high hourly rate in a 3dp currency comfortably exceeds INT32.
    base_cents_per_hour: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # ``Numeric(4, 2)`` for portability — overtime / night / weekend
    # multipliers are tiny rationals (1.25, 1.5, 2.0) and using a
    # Decimal avoids the binary-floating-point surprise at
    # aggregation time. SQLite stores Numeric as TEXT; Postgres uses
    # NUMERIC natively. Default 1.5 / 1.25 / 1.5 mirror the
    # common-case defaults in §09 "Overtime rule shape".
    overtime_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.5")
    )
    night_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.25")
    )
    weekend_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.5")
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # NULL means "open-ended" — the rule applies until a successor
    # row with a later ``effective_from`` supersedes it.
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft-ref :class:`str` — the creator may be a user id or a
    # system-actor id (e.g. the signup wizard that seeded the first
    # rule). Audit linkage lives in :mod:`app.adapters.db.audit`.
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # ISO-4217 codes are exactly 3 characters. A CHECK on length
        # is the cheapest portable guard; the domain layer validates
        # against a known-codes set before the write reaches here.
        CheckConstraint("LENGTH(currency) = 3", name="currency_length"),
        CheckConstraint("base_cents_per_hour >= 0", name="base_cents_per_hour_nonneg"),
        # Multipliers >= 1 — a premium, never a discount. The §09
        # domain allows finer-grained rules (e.g. a night rate that
        # is below base for a split shift); those lands with the
        # richer surface, the v1 slice is the simple "premium" case.
        CheckConstraint("overtime_multiplier >= 1", name="overtime_multiplier_min"),
        CheckConstraint("night_multiplier >= 1", name="night_multiplier_min"),
        CheckConstraint("weekend_multiplier >= 1", name="weekend_multiplier_min"),
        # Per-acceptance: "current rule for (workspace, user) as of a
        # moment" — the period-close worker's hot path. Leading
        # ``workspace_id`` lets the tenant filter ride the same
        # B-tree; ``user_id`` is the equality filter; ``effective_from``
        # carries the MAX() ranking.
        Index(
            "ix_pay_rule_workspace_user_effective_from",
            "workspace_id",
            "user_id",
            "effective_from",
        ),
    )


class PayPeriod(Base):
    """A wall-clock window one or more payslips belong to.

    A pay period is the workspace-wide window the period-close worker
    aggregates shift hours over: "every payslip for the workspace's
    April 2026 payroll". The v1 slice carries the starts_at / ends_at
    window (CHECK enforces strict ordering), the three-state
    ``state`` enum, the locked-at / locked-by pair for the close
    audit, and ``created_at``.

    UNIQUE ``(workspace_id, starts_at, ends_at)`` enforces the
    one-period-per-window invariant: a workspace cannot have two
    April 2026 periods. The richer §09 surface (per-engagement
    periods, divergent frequencies, ``pay_period_entry`` line rows)
    lands with the period-close domain follow-up; the v1 slice
    models "one flat period per workspace per window".
    """

    __tablename__ = "pay_period"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Three-state lifecycle: ``open`` (the worker is still
    # accumulating hours), ``locked`` (the period is closed, all
    # payslips are drafted, no new hours accrue), ``paid`` (every
    # payslip has been marked paid, flipped automatically by the
    # payslip-paid trigger in the domain layer — see §09 §"Transition
    # to paid"). Persisted as a plain string + CHECK; upgrading to a
    # native ENUM on Postgres is a portability loss not worth it yet.
    # Matches the canonical ``pay_period_status`` enum in §02 §"Enums".
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft-ref :class:`str` — see the module docstring. NULL until
    # the period is locked; once locked, the pair is the authoritative
    # wall-clock + actor for the close audit.
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"state IN ({_in_clause(_PAY_PERIOD_STATE_VALUES)})",
            name="state",
        ),
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        UniqueConstraint(
            "workspace_id",
            "starts_at",
            "ends_at",
            name="uq_pay_period_workspace_window",
        ),
    )


class Payslip(Base):
    """One computed pay document for a ``(pay_period, user)`` pair.

    A payslip snapshots the hours a user worked within a pay period,
    the cents owed gross, a mapping of deductions, and the resulting
    net. UNIQUE ``(pay_period_id, user_id)`` enforces the v1
    acceptance criterion: exactly one payslip per (period, user).
    The domain layer's period-close worker is the only writer.

    ``deductions_cents`` is a ``{reason: cents}`` JSON dict — empty
    when no deductions apply. The SQL CHECK cannot portably verify
    ``net_cents == gross_cents - sum(deductions_cents)`` (SQLite's
    JSON aggregate functions are not CHECK-safe and Postgres /
    SQLite differ on ``jsonb`` vs ``json`` semantics). That
    invariant is enforced in the closer's snapshot path — see the
    module docstring.

    ``pdf_blob_hash`` is NULL on an unissued payslip and set to the
    signed PDF's content-addressed hash once the payslip is issued
    (the §15 §"Right to erasure" path keeps the PDF pseudonymised).
    The hash-only column means a purge can drop the PDF without
    touching payroll history.
    """

    __tablename__ = "payslip"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE — deleting an ``open`` period sweeps its draft
    # payslips. Domain layer refuses to delete a ``locked`` or
    # ``paid`` period; at that point the payslip rows are the
    # payroll-law evidence.
    pay_period_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("pay_period.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``RESTRICT`` — see the module docstring. A payslip is labour-law
    # evidence (§09); the erasure path is
    # ``crewday admin purge --person`` which anonymises the user row
    # rather than dropping it.
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Hours as ``Numeric(10, 2)`` — two-decimal precision is the
    # convention for payroll reports (e.g. 151.67 h in a month). The
    # (10, 2) width fits 99_999_999.99 hours comfortably — four
    # orders of magnitude above the plausible maximum for a single
    # payslip, which leaves headroom for a backfill of historical
    # data at migration time.
    shift_hours_decimal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    overtime_hours_decimal: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    # Money as integer cents — see the module docstring. ``BigInteger``
    # because BHD / JOD are 3-dp minor units and accumulated gross
    # for a monthly-salaried worker in a 3-dp currency can exceed
    # INT32 for a single period; INT64 is pragmatic future-proofing.
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # ``Mapped[Any]`` is the documented exception for SQLAlchemy JSON
    # columns — see :mod:`app.adapters.db.audit` / :mod:`workspace`.
    # At the API boundary, callers type the payload as
    # ``dict[str, int]`` locally and coerce in.
    deductions_cents: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    net_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL until the payslip is issued; set to the PDF's content hash
    # once signed. The PDF itself lives in blob storage so a purge
    # can drop it while keeping the row intact.
    pdf_blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    payout_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    payout_manifest_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("shift_hours_decimal >= 0", name="shift_hours_decimal_nonneg"),
        CheckConstraint(
            "overtime_hours_decimal >= 0", name="overtime_hours_decimal_nonneg"
        ),
        CheckConstraint("gross_cents >= 0", name="gross_cents_nonneg"),
        # ``net_cents`` can legitimately be negative when deductions
        # exceed gross (rare, but valid — e.g. a cash advance
        # repayment). No CHECK on the sign; the domain layer guards
        # against nonsensical values.
        # Per-acceptance: one payslip per (period, user).
        UniqueConstraint(
            "pay_period_id",
            "user_id",
            name="uq_payslip_pay_period_user",
        ),
    )


class PayoutDestination(Base):
    """Worker payout routing destination.

    The full payout service lands in the payroll feature stream; this
    table is the privacy-critical subset needed by the purge flow. The
    secret routing payload lives in ``secret_envelope`` and this row
    carries only a pointer plus display metadata that can be scrubbed
    while preserving historical payslip references.
    """

    __tablename__ = "payout_destination"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    display_stub: Mapped[str | None] = mapped_column(String, nullable=True)
    secret_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("LENGTH(currency) = 3", name="currency_length"),
        Index("ix_payout_destination_workspace_user", "workspace_id", "user_id"),
    )
