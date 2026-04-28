"""SA-backed repositories implementing :mod:`app.domain.payroll.ports`.

The concrete class here adapts SQLAlchemy ``Session`` work to the
Protocol surface :mod:`app.domain.payroll.rules` consumes (cd-ea7):

* :class:`SqlAlchemyPayRuleRepository` — wraps the ``pay_rule`` table
  plus the ``payslip`` join through ``pay_period`` the
  locked-period guard needs.

The repo carries an open ``Session`` and never commits beyond what
the underlying statements require — the caller's UoW owns the
transaction boundary (§01 "Key runtime invariants" #3). Mutating
methods flush so a peer read in the same UoW (and the audit
writer's FK reference to ``entity_id``) sees the new row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import Session

from app.adapters.db.payroll.models import PayPeriod, PayRule, Payslip
from app.domain.payroll.ports import (
    PayPeriodRepository,
    PayPeriodRow,
    PayRuleRepository,
    PayRuleRow,
)

__all__ = [
    "SqlAlchemyPayPeriodRepository",
    "SqlAlchemyPayRuleRepository",
]


# Composite cursor separator. ``|`` is illegal in an ISO-8601 datetime
# string and not a ULID character, so a single literal split is
# unambiguous.
_CURSOR_SEP = "|"


def _split_cursor(cursor: str) -> tuple[datetime, str]:
    """Parse the ``"<isoformat>|<id>"`` cursor into ``(effective_from, id)``.

    A malformed cursor surfaces as :class:`ValueError`. The router's
    :func:`~app.api.pagination.decode_cursor` already maps decode
    errors to HTTP 422; if a base64-decoded cursor passes that gate
    but fails this split, the caller has tampered with the value
    and the bare exception is the right surface (the router can
    add a 422 mapping if that surface escapes).
    """
    if _CURSOR_SEP not in cursor:
        raise ValueError(f"pay_rule cursor missing '{_CURSOR_SEP}': {cursor!r}")
    iso, rule_id = cursor.split(_CURSOR_SEP, 1)
    return datetime.fromisoformat(iso), rule_id


def _to_row(row: PayRule) -> PayRuleRow:
    """Project an ORM ``PayRule`` into the seam-level row.

    Field-by-field copy — :class:`PayRuleRow` is frozen so the
    domain never mutates the ORM-managed instance through a shared
    reference.
    """
    return PayRuleRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        currency=row.currency,
        base_cents_per_hour=row.base_cents_per_hour,
        overtime_multiplier=row.overtime_multiplier,
        night_multiplier=row.night_multiplier,
        weekend_multiplier=row.weekend_multiplier,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _period_to_row(row: PayPeriod) -> PayPeriodRow:
    return PayPeriodRow(
        id=row.id,
        workspace_id=row.workspace_id,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        state=row.state,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        created_at=row.created_at,
    )


class SqlAlchemyPayPeriodRepository(PayPeriodRepository):
    """SA-backed concretion of :class:`PayPeriodRepository`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get(self, *, workspace_id: str, period_id: str) -> PayPeriodRow | None:
        row = self._session.scalars(
            select(PayPeriod).where(
                PayPeriod.id == period_id,
                PayPeriod.workspace_id == workspace_id,
            )
        ).one_or_none()
        return _period_to_row(row) if row is not None else None

    def list(self, *, workspace_id: str) -> Sequence[PayPeriodRow]:
        rows = self._session.scalars(
            select(PayPeriod)
            .where(PayPeriod.workspace_id == workspace_id)
            .order_by(PayPeriod.starts_at.desc(), PayPeriod.id.desc())
        ).all()
        return [_period_to_row(row) for row in rows]

    def has_overlap(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        exclude_period_id: str | None = None,
    ) -> bool:
        stmt = select(
            exists().where(
                PayPeriod.workspace_id == workspace_id,
                PayPeriod.starts_at < ends_at,
                PayPeriod.ends_at > starts_at,
            )
        )
        if exclude_period_id is not None:
            stmt = select(
                exists().where(
                    PayPeriod.workspace_id == workspace_id,
                    PayPeriod.id != exclude_period_id,
                    PayPeriod.starts_at < ends_at,
                    PayPeriod.ends_at > starts_at,
                )
            )
        return bool(self._session.scalar(stmt))

    def insert(
        self,
        *,
        period_id: str,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> PayPeriodRow:
        row = PayPeriod(
            id=period_id,
            workspace_id=workspace_id,
            starts_at=starts_at,
            ends_at=ends_at,
            state="open",
            created_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _period_to_row(row)

    def lock(
        self,
        *,
        workspace_id: str,
        period_id: str,
        locked_at: datetime,
        locked_by: str | None,
    ) -> PayPeriodRow:
        row = self._session.scalars(
            select(PayPeriod).where(
                PayPeriod.id == period_id,
                PayPeriod.workspace_id == workspace_id,
            )
        ).one()
        row.state = "locked"
        row.locked_at = locked_at
        row.locked_by = locked_by
        self._session.flush()
        return _period_to_row(row)

    def reopen(self, *, workspace_id: str, period_id: str) -> PayPeriodRow:
        row = self._session.scalars(
            select(PayPeriod).where(
                PayPeriod.id == period_id,
                PayPeriod.workspace_id == workspace_id,
            )
        ).one()
        row.state = "open"
        row.locked_at = None
        row.locked_by = None
        self._session.execute(
            update(Payslip)
            .where(
                Payslip.workspace_id == workspace_id,
                Payslip.pay_period_id == period_id,
            )
            .values(status="draft", issued_at=None, paid_at=None)
        )
        self._session.flush()
        return _period_to_row(row)

    def mark_paid(self, *, workspace_id: str, period_id: str) -> PayPeriodRow:
        row = self._session.scalars(
            select(PayPeriod).where(
                PayPeriod.id == period_id,
                PayPeriod.workspace_id == workspace_id,
            )
        ).one()
        row.state = "paid"
        self._session.flush()
        return _period_to_row(row)

    def delete(self, *, workspace_id: str, period_id: str) -> None:
        row = self._session.scalars(
            select(PayPeriod).where(
                PayPeriod.id == period_id,
                PayPeriod.workspace_id == workspace_id,
            )
        ).one()
        self._session.delete(row)
        self._session.flush()

    def has_paid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        Payslip.workspace_id == workspace_id,
                        Payslip.pay_period_id == period_id,
                        Payslip.status == "paid",
                        Payslip.paid_at.is_not(None),
                    )
                )
            )
        )

    def has_unpaid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        Payslip.workspace_id == workspace_id,
                        Payslip.pay_period_id == period_id,
                        or_(Payslip.status != "paid", Payslip.paid_at.is_(None)),
                    )
                )
            )
        )


class SqlAlchemyPayRuleRepository(PayRuleRepository):
    """SA-backed concretion of :class:`PayRuleRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    outside what the underlying statements require — the caller's
    UoW owns the transaction boundary (§01 "Key runtime invariants"
    #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        rule_id: str,
    ) -> PayRuleRow | None:
        # ``workspace_id`` predicate is defence-in-depth on top of the
        # ORM tenant filter — a misconfigured filter must fail loud,
        # not silently. Pay rules carry no ``deleted_at`` column;
        # ``effective_to`` is the soft-retire signal and the row stays
        # readable past it.
        row = self._session.scalars(
            select(PayRule).where(
                PayRule.id == rule_id,
                PayRule.workspace_id == workspace_id,
            )
        ).one_or_none()
        return _to_row(row) if row is not None else None

    def list_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        limit: int,
        after_cursor: str | None = None,
    ) -> Sequence[PayRuleRow]:
        # Order ``effective_from DESC, id DESC`` so the newest rule
        # surfaces first (matches §09 "Pay-rule selection" — greatest
        # ``effective_from`` wins, ULID-sort breaks ties; descending
        # walks the most-recent-first cursor naturally).
        stmt = (
            select(PayRule)
            .where(
                PayRule.workspace_id == workspace_id,
                PayRule.user_id == user_id,
            )
            .order_by(PayRule.effective_from.desc(), PayRule.id.desc())
            .limit(limit + 1)
        )
        if after_cursor is not None:
            # Composite cursor: ``"<effective_from-isoformat>|<id>"``.
            # ``effective_from`` is workspace-author-controlled (a
            # manager may backdate or future-date a rule), so a
            # ULID-only cursor would skip or repeat rows whenever
            # ``effective_from`` disagrees with ULID order. The
            # OR-expanded inequality below walks the desc page
            # deterministically and stays portable across SQLite and
            # Postgres (a row-tuple ``<`` comparison is supported on
            # Postgres but unreliable on SQLite).
            cursor_from, cursor_id = _split_cursor(after_cursor)
            stmt = stmt.where(
                or_(
                    PayRule.effective_from < cursor_from,
                    and_(
                        PayRule.effective_from == cursor_from,
                        PayRule.id < cursor_id,
                    ),
                )
            )
        rows = self._session.scalars(stmt).all()
        return [_to_row(r) for r in rows]

    def has_paid_payslip_overlap(
        self,
        *,
        workspace_id: str,
        user_id: str,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> bool:
        # Window overlap predicate (see ``has_paid_payslip_overlap``
        # on the Protocol):
        # ``rule.effective_from <= period.ends_at`` AND
        # ``rule.effective_to IS NULL OR rule.effective_to >= period.starts_at``.
        # ``effective_to=None`` collapses the second clause to TRUE,
        # which we encode as ``or_(effective_to is None, …)`` so the
        # SQL stays portable between SQLite and Postgres.
        if effective_to is None:
            window_overlap = effective_from <= PayPeriod.ends_at
        else:
            window_overlap = and_(
                effective_from <= PayPeriod.ends_at,
                effective_to >= PayPeriod.starts_at,
            )

        stmt = select(
            exists().where(
                Payslip.workspace_id == workspace_id,
                Payslip.user_id == user_id,
                Payslip.pay_period_id == PayPeriod.id,
                PayPeriod.state == "paid",
                window_overlap,
            )
        )
        result = self._session.scalar(stmt)
        return bool(result)

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        rule_id: str,
        workspace_id: str,
        user_id: str,
        currency: str,
        base_cents_per_hour: int,
        overtime_multiplier: Decimal,
        night_multiplier: Decimal,
        weekend_multiplier: Decimal,
        effective_from: datetime,
        effective_to: datetime | None,
        created_by: str | None,
        now: datetime,
    ) -> PayRuleRow:
        row = PayRule(
            id=rule_id,
            workspace_id=workspace_id,
            user_id=user_id,
            currency=currency,
            base_cents_per_hour=base_cents_per_hour,
            overtime_multiplier=overtime_multiplier,
            night_multiplier=night_multiplier,
            weekend_multiplier=weekend_multiplier,
            effective_from=effective_from,
            effective_to=effective_to,
            created_by=created_by,
            created_at=now,
        )
        self._session.add(row)
        # No ``IntegrityError`` catch — the domain layer narrows
        # currency, multipliers, and window order before the write
        # reaches here, so a flush-time CHECK trip is a programming
        # error and a stack trace is the right surface.
        self._session.flush()
        return _to_row(row)

    def update(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        currency: str,
        base_cents_per_hour: int,
        overtime_multiplier: Decimal,
        night_multiplier: Decimal,
        weekend_multiplier: Decimal,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> PayRuleRow:
        # Caller has already confirmed the row exists via :meth:`get`;
        # use the same workspace-scoped SELECT shape so the caller's
        # UoW reuses the identity-map entry rather than spawning a
        # second instance for the same primary key.
        row = self._session.scalars(
            select(PayRule).where(
                PayRule.id == rule_id,
                PayRule.workspace_id == workspace_id,
            )
        ).one()
        row.currency = currency
        row.base_cents_per_hour = base_cents_per_hour
        row.overtime_multiplier = overtime_multiplier
        row.night_multiplier = night_multiplier
        row.weekend_multiplier = weekend_multiplier
        row.effective_from = effective_from
        row.effective_to = effective_to
        self._session.flush()
        return _to_row(row)

    def soft_delete(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        now: datetime,
    ) -> PayRuleRow:
        row = self._session.scalars(
            select(PayRule).where(
                PayRule.id == rule_id,
                PayRule.workspace_id == workspace_id,
            )
        ).one()
        # Pay rules are payroll-law evidence — never hard-deleted.
        # "Delete" stamps ``effective_to`` so the rule no longer
        # applies to future periods but historical payslips keep
        # their FK link.
        #
        # Idempotency: if the row is already retired (``effective_to``
        # set and ``<= now``), preserve the **earlier** retirement
        # timestamp. Overwriting it with ``now`` would destroy the
        # historical evidence of when the rule was first retired —
        # a payroll-law audit trail must keep that anchor stable.
        # The Protocol docstring contracts this as "no-op write that
        # still reports the (unchanged) projection back to the
        # caller". We still flush so the audit writer's
        # ``entity_id`` reference + the post-flush ``_to_row`` read
        # see a consistent snapshot.
        if row.effective_to is None or row.effective_to > now:
            row.effective_to = now
        self._session.flush()
        return _to_row(row)
