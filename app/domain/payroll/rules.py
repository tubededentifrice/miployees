"""Pay-rule CRUD service (§09 "Pay rules").

The :class:`~app.adapters.db.payroll.models.PayRule` row binds a
``(workspace, user)`` pair to a monetary rate for a wall-clock
interval. This module is the only place that inserts, updates,
soft-deletes, or reads pay-rule rows at the domain layer — the
HTTP router in :mod:`app.api.v1.payroll` is a thin DTO passthrough.

Public surface:

* **DTOs** — Pydantic v2 :class:`PayRuleCreate` /
  :class:`PayRuleUpdate` for write shapes; the immutable
  :class:`PayRuleView` for reads.
* **Service functions** — :func:`create_rule`, :func:`list_rules`,
  :func:`get_rule`, :func:`update_rule`, :func:`soft_delete_rule`.
  Every function takes a :class:`~app.tenancy.WorkspaceContext` as
  its first argument; workspace scoping flows through the repo.
* **Errors** — :class:`PayRuleNotFound` (404),
  :class:`PayRuleInvariantViolated` (422),
  :class:`PayRuleLocked` (409).

**Domain validation** (in addition to DB CHECKs in
:mod:`app.adapters.db.payroll.models`):

* ``currency`` must be in :data:`app.util.currency.ISO_4217_ALLOWLIST`
  after upper-casing — the DB CHECK only enforces ``LENGTH = 3``.
* All three multipliers in ``[1.0, 5.0]``. The DB CHECK enforces
  the lower bound (``>= 1``) but not the upper. A multiplier above
  5.0 is almost certainly a unit confusion (pay-rate-per-hour
  pasted into the multiplier field) — fail loud rather than letting
  a 100x payroll out the door.
* ``effective_to`` (when set) must be strictly after
  ``effective_from``.
* ``base_cents_per_hour >= 0`` (also a DB CHECK; revalidated here
  so a Python caller bypassing the API still hits the same error).

**Authorisation.** Every mutating call enforces
``pay_rules.edit`` at workspace scope via :func:`app.authz.require`.
The catalog entry (`app/domain/identity/_action_catalog.py`) defaults
the action to ``owners + managers`` so a worker cannot edit pay
rules even with a passing tenancy filter. Reads (:func:`get_rule`,
:func:`list_rules`) inherit the workspace tenancy filter and gate
on ``pay_rules.edit`` too — pay rates carry compensation-PII and
the desk affordance is owner/manager-only.

**Locked-period guard.** A pay-rule that has been consumed by a
payslip in a paid pay_period must not be mutated — historical
evidence is fixed. :func:`update_rule` and :func:`soft_delete_rule`
both call :meth:`PayRuleRepository.has_paid_payslip_overlap`; a
``True`` return raises :class:`PayRuleLocked` (409).

**Soft-delete shape.** "Delete" stamps ``effective_to`` to ``now``
rather than dropping the row — pay rules are payroll-law evidence
(§09 §"Labour-law compliance"; §15 §"Right to erasure"). The
``RESTRICT`` FK on ``user_id`` aligns with the same intent: the
purge-person path anonymises the user row without breaking the
pay-rule chain.

**Architecture.** The module talks to a
:class:`~app.domain.payroll.ports.PayRuleRepository` Protocol —
never to the SQLAlchemy model classes directly. The SA-backed
concretion lives at
:class:`app.adapters.db.payroll.repositories.SqlAlchemyPayRuleRepository`;
unit tests inject a fake. The repo also threads its open
:class:`~sqlalchemy.orm.Session` through ``repo.session`` so the
audit writer (``app.audit.write_audit``) — which still takes a
concrete ``Session`` today — can keep using the same UoW.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation
writes one :mod:`app.audit` row in the same transaction.

See ``docs/specs/09-time-payroll-expenses.md`` §"Pay rules" /
§"Pay-rule selection when multiple rules overlap" /
§"Labour-law compliance"; ``docs/specs/02-domain-model.md``
§"pay_rule"; ``docs/specs/15-security-privacy.md``
§"Right to erasure".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.audit import write_audit
from app.authz import require
from app.domain.payroll.ports import PayRuleRepository, PayRuleRow
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.currency import is_valid_currency, normalise_currency
from app.util.ulid import new_ulid

__all__ = [
    "BASE_CENTS_MAX",
    "PayRuleCreate",
    "PayRuleInvariantViolated",
    "PayRuleLocked",
    "PayRuleNotFound",
    "PayRuleUpdate",
    "PayRuleView",
    "create_rule",
    "cursor_for_view",
    "get_rule",
    "list_rules",
    "soft_delete_rule",
    "update_rule",
]


# ---------------------------------------------------------------------------
# Validation bounds
# ---------------------------------------------------------------------------


# Multipliers in ``[1.0, 5.0]``. The DB CHECK enforces ``>= 1``; the
# upper bound lives in the domain so future jurisdictions that
# legitimately need a richer range (e.g. a 6x holiday premium) can
# raise it without a migration.
_MULTIPLIER_MIN: Decimal = Decimal("1")
_MULTIPLIER_MAX: Decimal = Decimal("5")

# Hourly cents upper bound — defence-in-depth against a unit-confusion
# bug ("paste full-year salary into hourly cents"). 1 000 000 cents/h
# is $10 000/hour, comfortably above any plausible household-manager
# rate; a higher value almost certainly indicates a wrong column.
# Module-public so the wire DTO in :mod:`app.api.v1.payroll` can
# import it rather than duplicate the literal — keeps a single source
# of truth between the wire-shape ``le=`` constraint and the domain
# guard's error message.
BASE_CENTS_MAX: int = 1_000_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PayRuleNotFound(LookupError):
    """The requested pay rule does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`get_rule`, :func:`update_rule`,
    and :func:`soft_delete_rule` when the id is unknown or lives in
    a different workspace. Matches §01 "tenant surface is not
    enumerable" — we deliberately do not distinguish
    "wrong-workspace" from "really missing".
    """


class PayRuleInvariantViolated(ValueError):
    """A write would violate a §09 / §02 invariant.

    422-equivalent. Thrown when:

    * ``currency`` is not in the ISO-4217 allow-list;
    * any multiplier is outside ``[1.0, 5.0]``;
    * ``effective_to <= effective_from``;
    * ``base_cents_per_hour`` is negative or implausibly high.

    Pydantic-level shape errors (missing field, wrong type) surface
    as :class:`pydantic.ValidationError` directly; this exception
    is reserved for cross-field rules the DTO can't express.
    """


class PayRuleLocked(RuntimeError):
    """The pay rule is consumed by a paid payslip and cannot be mutated.

    409-equivalent. §09 §"Labour-law compliance" + §15 §"Right to
    erasure" pin a pay-rule once it has been folded into a paid
    payslip — editing or hard-deleting it would retro-corrupt
    payroll evidence. Callers wanting to change the rate author a
    successor row with a later ``effective_from``, leaving the
    historical row intact.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


def _validate_currency(value: str) -> str:
    """Normalise + narrow ``value`` to the ISO-4217 allow-list.

    Returns the upper-cased canonical form on success. Anything that
    fails :func:`app.util.currency.is_valid_currency` after
    upper-case normalisation raises :class:`PayRuleInvariantViolated`
    (422 surface) — pydantic ``ValidationError`` is reserved for
    shape errors the DTO encodes structurally. The allow-list
    membership is a domain rule (the set is editable per
    deployment), not a wire-shape rule.
    """
    upper = normalise_currency(value)
    if not is_valid_currency(upper):
        raise PayRuleInvariantViolated(
            f"currency {value!r} is not a valid ISO-4217 code "
            "in the deployment allow-list"
        )
    return upper


def _validate_multiplier(value: Decimal, *, name: str) -> Decimal:
    """Narrow ``value`` to the domain's multiplier bounds.

    The DB CHECK enforces ``>= 1``; the domain caps at
    :data:`_MULTIPLIER_MAX` so a unit-confusion bug (rate pasted
    into multiplier) surfaces as a 422 rather than committing a
    100x payroll.
    """
    if value < _MULTIPLIER_MIN or value > _MULTIPLIER_MAX:
        raise PayRuleInvariantViolated(
            f"{name} must be in [{_MULTIPLIER_MIN}, {_MULTIPLIER_MAX}]; got {value}"
        )
    return value


def _validate_base_cents(value: int) -> int:
    """Narrow ``value`` to a non-negative, plausible hourly rate."""
    if value < 0:
        raise PayRuleInvariantViolated(
            f"base_cents_per_hour must be non-negative; got {value}"
        )
    if value > BASE_CENTS_MAX:
        raise PayRuleInvariantViolated(
            f"base_cents_per_hour must be <= {BASE_CENTS_MAX}; got {value}"
        )
    return value


def _validate_window(
    *,
    effective_from: datetime,
    effective_to: datetime | None,
) -> None:
    """Enforce ``effective_to`` (when set) strictly after ``effective_from``.

    A zero-length window is rejected — a rule that never applies is
    almost certainly a caller bug.
    """
    if effective_to is None:
        return
    if effective_to <= effective_from:
        raise PayRuleInvariantViolated(
            "effective_to must be strictly after effective_from"
        )


class _PayRuleBody(BaseModel):
    """Shared mutable body of :class:`PayRuleCreate` and :class:`PayRuleUpdate`.

    Held as a private base so the cross-field validator
    (``effective_to > effective_from``) and the Decimal coercion
    rules apply uniformly to both write shapes.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(..., min_length=3, max_length=3)
    base_cents_per_hour: int = Field(..., ge=0, le=BASE_CENTS_MAX)
    # Pydantic v2 coerces ``str``/``int``/``float`` → Decimal when the
    # annotation is ``Decimal``; we keep the wire shape permissive so
    # JSON ``1.5`` and string ``"1.5"`` both land cleanly.
    overtime_multiplier: Decimal = Field(default=Decimal("1.5"))
    night_multiplier: Decimal = Field(default=Decimal("1.25"))
    weekend_multiplier: Decimal = Field(default=Decimal("1.5"))
    effective_from: datetime
    effective_to: datetime | None = None


class PayRuleCreate(_PayRuleBody):
    """Request body for :func:`create_rule`.

    ``user_id`` is **deliberately absent** — it is a path parameter
    on the HTTP route and a service-call kwarg, never a body field.
    Mixing identity into the body would let a caller flip the
    ``user_id`` mid-request when callers use the same DTO for
    create + update, which is a sharp edge worth removing at the
    type level.
    """


class PayRuleUpdate(_PayRuleBody):
    """Request body for :func:`update_rule`.

    v1 treats update as a full replacement of the mutable body —
    the spec does not (yet) call for per-field PATCH on pay rules,
    and a partial update would let a caller silently widen the
    effective window without re-asserting consent. Callers send
    the full desired state; the service diffs against the current
    row and writes the before/after pair to the audit log.
    """


@dataclass(frozen=True, slots=True)
class PayRuleView:
    """Immutable read projection of a ``pay_rule`` row.

    Returned by every service read + write. Frozen / slotted so the
    domain never accidentally mutates the value through a shared
    reference.
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
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _row_to_view(row: PayRuleRow) -> PayRuleView:
    """Project a seam-level row into the public view."""
    return PayRuleView(
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


def _view_to_diff_dict(view: PayRuleView) -> dict[str, Any]:
    """Flatten a :class:`PayRuleView` into a JSON-safe audit payload."""
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "user_id": view.user_id,
        "currency": view.currency,
        "base_cents_per_hour": view.base_cents_per_hour,
        "overtime_multiplier": str(view.overtime_multiplier),
        "night_multiplier": str(view.night_multiplier),
        "weekend_multiplier": str(view.weekend_multiplier),
        "effective_from": view.effective_from.isoformat(),
        "effective_to": (
            view.effective_to.isoformat() if view.effective_to is not None else None
        ),
        "created_by": view.created_by,
        "created_at": view.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_row(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    rule_id: str,
) -> PayRuleRow:
    """Return the row or raise :class:`PayRuleNotFound`."""
    row = repo.get(workspace_id=ctx.workspace_id, rule_id=rule_id)
    if row is None:
        raise PayRuleNotFound(rule_id)
    return row


def _enforce_edit(repo: PayRuleRepository, ctx: WorkspaceContext) -> None:
    """Run the ``pay_rules.edit`` capability check at workspace scope.

    Service-layer enforcement (mirrors the property work-role
    assignment service's pattern of not relying solely on the HTTP
    Permission dep). Callers from non-HTTP transports (CLI, agent,
    background worker) get the same gate without re-implementing it.
    """
    require(
        repo.session,
        ctx,
        action_key="pay_rules.edit",
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )


def _validate_body(body: _PayRuleBody) -> tuple[str, int, Decimal, Decimal, Decimal]:
    """Apply every cross-field domain rule and return canonicalised values.

    Returns ``(currency, base_cents, overtime, night, weekend)`` —
    same order the repo's ``insert`` / ``update`` accept. The
    DTO-level pydantic shape rules (``ge=0``, ``min_length=3``) have
    already fired by the time we get here; this layer adds:

    * currency narrowed to the ISO-4217 allow-list (with upper-case
      normalisation);
    * multipliers narrowed to ``[1.0, 5.0]``;
    * window order (``effective_to`` strictly after
      ``effective_from``).

    Raises :class:`PayRuleInvariantViolated` (422) on any failure.
    """
    currency = _validate_currency(body.currency)
    base_cents = _validate_base_cents(body.base_cents_per_hour)
    overtime = _validate_multiplier(
        body.overtime_multiplier, name="overtime_multiplier"
    )
    night = _validate_multiplier(body.night_multiplier, name="night_multiplier")
    weekend = _validate_multiplier(body.weekend_multiplier, name="weekend_multiplier")
    _validate_window(
        effective_from=body.effective_from,
        effective_to=body.effective_to,
    )
    return currency, base_cents, overtime, night, weekend


def _assert_not_locked(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    effective_from: datetime,
    effective_to: datetime | None,
) -> None:
    """Raise :class:`PayRuleLocked` if any paid payslip references this rule.

    The locked-period guard fires when the rule's effective window
    overlaps a ``pay_period`` in ``state = 'paid'`` for the same
    user — see :meth:`PayRuleRepository.has_paid_payslip_overlap`
    for the overlap predicate.

    Locked rules are immutable; callers who need to change a rate
    going forward author a successor row with a later
    ``effective_from`` (the §09 selection rule picks the most
    recent matching window). The historical row remains visible so
    payslip recomputation against it is reproducible.
    """
    locked = repo.has_paid_payslip_overlap(
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    if locked:
        raise PayRuleLocked(
            "pay rule is consumed by a paid payslip; author a successor "
            "row with a later effective_from instead"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_rules(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    limit: int = 50,
    after_cursor: str | None = None,
) -> Sequence[PayRuleView]:
    """Return up to ``limit + 1`` views for ``(workspace, user_id)``.

    Ordered by ``effective_from DESC`` (newest first), with the row
    id as the tiebreaker. The router's
    :func:`~app.api.pagination.paginate` helper consumes the extra
    row to compute ``has_more``.

    ``after_cursor`` is the composite ``"<effective_from-isoformat>|<id>"``
    handle the repo expects — see
    :meth:`~app.domain.payroll.ports.PayRuleRepository.list_for_user`
    for the format rationale (effective_from is workspace-author-
    controlled, so a ULID-only cursor would skip or repeat rows).

    Gates on ``pay_rules.edit`` — pay rates carry compensation-PII
    (§15) and the v1 surface is owner/manager-only on both read and
    write. A worker calling this raises
    :class:`~app.authz.PermissionDenied` (403).
    """
    _enforce_edit(repo, ctx)
    rows = repo.list_for_user(
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        limit=limit,
        after_cursor=after_cursor,
    )
    return [_row_to_view(r) for r in rows]


def cursor_for_view(view: PayRuleView) -> str:
    """Return the composite cursor key for a ``PayRuleView``.

    The router's :func:`~app.api.pagination.paginate` helper passes
    this as ``key_getter`` so ``next_cursor`` carries both
    ``effective_from`` and ``id`` — see :func:`list_rules` for the
    composite-cursor rationale.
    """
    return f"{view.effective_from.isoformat()}|{view.id}"


def get_rule(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    rule_id: str,
) -> PayRuleView:
    """Return a single :class:`PayRuleView` or raise on miss.

    Gates on ``pay_rules.edit`` — same rationale as
    :func:`list_rules`.
    """
    _enforce_edit(repo, ctx)
    return _row_to_view(_load_row(repo, ctx, rule_id=rule_id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_rule(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    body: PayRuleCreate,
    clock: Clock | None = None,
) -> PayRuleView:
    """Insert a new ``pay_rule`` row.

    Runs every domain validation rule before reaching the DB; on
    success, writes one ``pay_rule.created`` audit row in the same
    transaction.

    The ``user_id`` is a service kwarg (not a body field) — the
    HTTP route binds it from the path. ``created_by`` is set to
    ``ctx.actor_id`` so the audit chain stays attributable to the
    actor who minted the rule.
    """
    _enforce_edit(repo, ctx)
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    currency, base_cents, overtime, night, weekend = _validate_body(body)

    rule_id = new_ulid(clock=clock)
    inserted = repo.insert(
        rule_id=rule_id,
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        currency=currency,
        base_cents_per_hour=base_cents,
        overtime_multiplier=overtime,
        night_multiplier=night,
        weekend_multiplier=weekend,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
        created_by=ctx.actor_id,
        now=now,
    )
    view = _row_to_view(inserted)
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_rule",
        entity_id=view.id,
        action="pay_rule.created",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_rule(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    rule_id: str,
    body: PayRuleUpdate,
    clock: Clock | None = None,
) -> PayRuleView:
    """Replace the mutable body of ``rule_id``.

    A full-replacement update — v1 does not expose a per-field
    PATCH on pay rules. Two pre-flight checks fire before the
    write:

    * the row must exist in the caller's workspace
      (:class:`PayRuleNotFound`, 404);
    * **the existing row's effective window** must not overlap any
      paid payslip (:class:`PayRuleLocked`, 409). The new window
      is irrelevant to the locked check — what matters is whether
      historical evidence already cites this row.

    Records one ``pay_rule.updated`` audit row with the
    before/after diff so operators can reconstruct the change.
    """
    _enforce_edit(repo, ctx)
    resolved_clock = clock if clock is not None else SystemClock()
    # ``updated_at`` does not yet exist on the v1 schema; the audit
    # row's ``created_at`` (stamped by :func:`write_audit` via the
    # same ``clock``) is the canonical "when did this change" signal
    # until the column lands.
    row = _load_row(repo, ctx, rule_id=rule_id)
    before = _row_to_view(row)

    # Lock-check on the **existing** window — the question is "has
    # this row already been folded into a paid payslip?", not "would
    # the new window overlap one". A new window that no longer
    # overlaps any paid period would still mean the rule was
    # consumed in the past, and the audit chain must keep that
    # evidence stable.
    _assert_not_locked(
        repo,
        ctx,
        user_id=row.user_id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
    )

    currency, base_cents, overtime, night, weekend = _validate_body(body)

    updated = repo.update(
        workspace_id=ctx.workspace_id,
        rule_id=rule_id,
        currency=currency,
        base_cents_per_hour=base_cents,
        overtime_multiplier=overtime,
        night_multiplier=night,
        weekend_multiplier=weekend,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
    )
    after = _row_to_view(updated)
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_rule",
        entity_id=after.id,
        action="pay_rule.updated",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def soft_delete_rule(
    repo: PayRuleRepository,
    ctx: WorkspaceContext,
    *,
    rule_id: str,
    clock: Clock | None = None,
) -> PayRuleView:
    """Soft-delete ``rule_id`` by stamping ``effective_to = now``.

    Pay rules are payroll-law evidence (§09 §"Labour-law
    compliance"; §15 §"Right to erasure") — never hard-deleted.
    The "delete" path is a soft-retire: ``effective_to`` is stamped
    so the rule no longer applies to future periods, while every
    historical payslip keeps a live FK to it.

    Pre-flight checks:

    * row must exist in the caller's workspace
      (:class:`PayRuleNotFound`, 404);
    * existing window must not overlap any paid payslip
      (:class:`PayRuleLocked`, 409) — same evidence-preservation
      rationale as :func:`update_rule`.

    Records one ``pay_rule.deleted`` audit row with the
    before/after diff.
    """
    _enforce_edit(repo, ctx)
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(repo, ctx, rule_id=rule_id)
    before = _row_to_view(row)

    _assert_not_locked(
        repo,
        ctx,
        user_id=row.user_id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
    )

    deleted = repo.soft_delete(
        workspace_id=ctx.workspace_id,
        rule_id=rule_id,
        now=now,
    )
    after = _row_to_view(deleted)
    write_audit(
        repo.session,
        ctx,
        entity_kind="pay_rule",
        entity_id=after.id,
        action="pay_rule.deleted",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after
