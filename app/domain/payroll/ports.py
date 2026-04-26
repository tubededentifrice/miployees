"""Payroll context — repository ports.

Defines :class:`PayRuleRepository`, the seam
:mod:`app.domain.payroll.rules` uses to read and write
``pay_rule`` rows plus the ``pay_period`` / ``payslip`` lookups the
"locked-period" guard needs — without importing SQLAlchemy model
classes directly (cd-ea7).

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``) and a SQLAlchemy adapter under
``app/adapters/db/<context>/``. Mirrors the cd-kezq seam shape
introduced for places.

The repo carries an open SQLAlchemy ``Session`` so the audit writer
(:func:`app.audit.write_audit`) — which still takes a concrete
``Session`` today — can ride the same Unit of Work without forcing
callers to thread a second seam. Drops once the audit writer gains
its own Protocol.

The repo-shaped value object :class:`PayRuleRow` mirrors the domain's
:class:`~app.domain.payroll.rules.PayRuleView`. It lives on the seam
so the SA adapter has a domain-owned shape to project ORM rows into
without importing the service module that produces the view (which
would create a circular dependency between ``rules`` and this
module).

Protocol is deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this Protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "PayRuleRepository",
    "PayRuleRow",
]


# ---------------------------------------------------------------------------
# Row shape (value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PayRuleRow:
    """Immutable projection of a ``pay_rule`` row.

    Mirrors the shape of
    :class:`app.domain.payroll.rules.PayRuleView`; declared here so
    the Protocol surface does not depend on the service module
    (which itself imports this seam).
    """

    id: str
    workspace_id: str
    user_id: str
    currency: str
    base_cents_per_hour: int
    overtime_multiplier: Decimal
    night_multiplier: Decimal
    weekend_multiplier: Decimal
    effective_from: datetime
    effective_to: datetime | None
    created_by: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# PayRuleRepository
# ---------------------------------------------------------------------------


class PayRuleRepository(Protocol):
    """Read + write seam for ``pay_rule`` rows + the locked-period guard.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits outside what the underlying statements
    require — the caller's UoW owns the transaction boundary (§01
    "Key runtime invariants" #3). Methods that mutate state flush so
    the caller's next read (and the audit writer's FK reference to
    ``entity_id``) sees the new row.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its
        own Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        rule_id: str,
    ) -> PayRuleRow | None:
        """Return the row or ``None`` when invisible to the caller.

        Defence-in-depth pins the lookup to ``workspace_id`` even
        though the ORM tenant filter already narrows the read; a
        misconfigured filter must fail loud, not silently. There is
        no ``include_deleted`` flag — pay rules use the
        ``effective_to`` column as a soft-retire signal rather than
        a separate ``deleted_at`` (a row whose ``effective_to`` is
        in the past is still labour-law evidence and must remain
        readable).
        """
        ...

    def list_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        limit: int,
        after_cursor: str | None = None,
    ) -> Sequence[PayRuleRow]:
        """Return up to ``limit + 1`` rows for ``(workspace, user)``.

        Ordered ``effective_from DESC, id DESC`` so the newest rule
        for the user surfaces first — matches the §09 "Pay-rule
        selection" precedence (greatest ``effective_from`` wins).

        ``after_cursor`` is the opaque-cursor handle the
        :func:`~app.api.pagination.paginate` helper round-trips. The
        cursor is **composite** — formatted as
        ``"<effective_from-isoformat>|<id>"`` — because ``effective_from``
        is workspace-author-controlled (a manager may backdate or
        future-date a rule), so ``effective_from`` need not align
        with ULID order. A ULID-only cursor would skip or repeat
        rows whenever a backdated rule has a higher id than an
        earlier rule with a later ``effective_from``. The composite
        cursor walks the desc page deterministically:
        ``(effective_from, id) < (cursor_effective_from, cursor_id)``.
        """
        ...

    def has_paid_payslip_overlap(
        self,
        *,
        workspace_id: str,
        user_id: str,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> bool:
        """Return ``True`` iff the rule's window overlaps a paid payslip.

        The §09 §"Labour-law compliance" + §15 §"Right to erasure"
        rules pin a pay-rule once it has been consumed by a payslip
        in a paid pay_period — editing or hard-deleting it would
        retro-corrupt payroll evidence.

        The check is structurally:

        * any ``payslip`` whose ``user_id`` matches the rule's,
          whose parent ``pay_period`` is in ``state = 'paid'``, and
          whose ``(starts_at, ends_at)`` window overlaps the rule's
          ``[effective_from, effective_to]`` window — counts as a
          "consumed" rule.

        Window-overlap semantics: two windows overlap iff
        ``effective_from <= period.ends_at`` AND
        ``(effective_to IS NULL OR effective_to >= period.starts_at)``.
        ``effective_to=None`` means "open-ended" — the rule still
        applies, so the second clause collapses to ``TRUE``.

        v1's :class:`Payslip` does not yet carry its own
        per-payslip state column; ``pay_period.state == 'paid'`` is
        the canonical "every payslip in the period was marked paid"
        signal (cd-a3w transition flips the period state once every
        contained payslip is paid). When the per-payslip
        ``status`` enum lands (cd-* TBD) this check upgrades to
        ``payslip.status = 'paid'`` without changing the seam shape.
        """
        ...

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
        """Insert a new ``pay_rule`` row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        FK reference to ``entity_id``) sees the new row. The DB
        CHECKs (currency length, non-negative cents, multipliers
        >= 1) are belt-and-braces — the domain layer validates
        the same predicates *plus* the upper-bound multiplier cap
        and the ISO-4217 allow-list before reaching here, so a
        flush-time violation is a programming error worth a stack
        trace rather than a typed exception.
        """
        ...

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
        """Apply a full-replacement update and return the refreshed projection.

        v1 treats the mutable surface as a complete replacement —
        the spec does not (yet) call for per-field PATCH on pay
        rules and a partial update would let a caller silently
        widen the effective window without re-asserting consent.
        Stamps no ``updated_at`` because the column is not yet on
        the v1 schema; the audit row + the row's ``created_at``
        are the canonical timestamps.

        Caller has already confirmed the row exists (via :meth:`get`)
        and that the locked-period guard does not fire.
        """
        ...

    def soft_delete(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        now: datetime,
    ) -> PayRuleRow:
        """Stamp ``effective_to = now`` and return the refreshed projection.

        Pay rules are never hard-deleted: the row is payroll-law
        evidence (§09 §"Labour-law compliance"). "Delete" here is a
        soft-retire — set ``effective_to`` so the rule no longer
        applies to future periods but historical payslips still
        link to a live row. If the row was already retired
        (``effective_to`` is set and in the past), this becomes a
        no-op write that still reports the (unchanged) projection
        back to the caller; the service is the gate that decides
        whether the operation is meaningful.
        """
        ...
