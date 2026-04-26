"""Unit tests for :mod:`app.domain.payroll.rules` (cd-ea7).

The full DB round-trip (real :class:`PayRule` row, the locked-period
guard against a real ``pay_period`` + ``payslip`` chain, the audit
writer's redaction seam) lives under
``tests/integration/payroll/test_pay_rules_api.py``. These tests
exercise the **domain seam** — confirming the service runs every
validator + the locked-period guard + the audit-writer call against
an in-memory fake repository.

We cover:

* Happy-path create / update / soft-delete + audit emission.
* Every validation refusal (currency outside the ISO-4217 allow-list,
  multipliers out of range, bad effective window,
  base_cents_per_hour out of range).
* Locked-period refusal on update + soft-delete (``has_paid_payslip_overlap``
  fake returns ``True``).
* Soft-delete idempotency: re-deleting an already-retired rule
  preserves the earlier ``effective_to`` (payroll-law evidence).
* Pagination: list returns ``limit + 1`` rows the router can use to
  decide ``has_more``; composite cursor stays stable across
  out-of-order ULID / ``effective_from`` combinations (backdated
  rules).

Capability enforcement (``pay_rules.edit``) lives in the integration
suite — it depends on a live :class:`Session` to look up the owners
group membership; the unit suite stubs it out via a fake session
that the resolver short-circuits to ``allow``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.domain.payroll.ports import PayRuleRepository, PayRuleRow
from app.domain.payroll.rules import (
    PayRuleCreate,
    PayRuleInvariantViolated,
    PayRuleLocked,
    PayRuleNotFound,
    PayRuleUpdate,
    create_rule,
    get_rule,
    list_rules,
    soft_delete_rule,
    update_rule,
)
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_WS_ID = "01HWA00000000000000000WS01"
_USER_ID = "01HWA00000000000000000USR1"
_ACTOR_ID = "01HWA00000000000000000USR2"


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WS_ID,
        workspace_slug="ws",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal SA :class:`Session` stand-in.

    The audit writer calls :meth:`add` with the assembled
    :class:`AuditLog` row; we capture the writes so the tests can
    assert on the action key + entity kind without a real DB.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)


@pytest.fixture(autouse=True)
def _allow_authz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit :func:`app.authz.require` to ``allow``.

    The unit suite avoids the real :class:`Session` + role-grant
    bootstrap; we monkey-patch the ``require`` symbol the service
    binds at import time to a no-op so the capability gate is
    structurally exercised but always returns ``allow``. The
    integration suite covers the real enforcer end-to-end.
    """

    def _allow(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.domain.payroll.rules.require", _allow)


class _FakeRepo(PayRuleRepository):
    """In-memory :class:`PayRuleRepository` stub.

    Honours the workspace-scoping invariant — every read/write
    filters on ``workspace_id`` so a misconfigured caller still
    surfaces as ``None`` / ``KeyError`` rather than silently
    leaking across tenants.
    """

    def __init__(self, *, locked: bool = False) -> None:
        self._rows: dict[str, PayRuleRow] = {}
        self._session = _FakeSession()
        self._locked = locked
        self.lock_calls: list[dict[str, Any]] = []

    @property
    def session(self) -> Any:
        return self._session

    # -- Reads -----------------------------------------------------------

    def get(self, *, workspace_id: str, rule_id: str) -> PayRuleRow | None:
        row = self._rows.get(rule_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return row

    def list_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        limit: int,
        after_cursor: str | None = None,
    ) -> Sequence[PayRuleRow]:
        rows = [
            r
            for r in self._rows.values()
            if r.workspace_id == workspace_id and r.user_id == user_id
        ]
        rows.sort(key=lambda r: (r.effective_from, r.id), reverse=True)
        if after_cursor is not None:
            # Mirror the SA concretion's composite-cursor split so
            # the desc-pagination predicate stays stable when
            # ``effective_from`` and ULID ordering disagree (e.g.
            # backdated rules).
            if "|" not in after_cursor:
                raise ValueError(f"pay_rule cursor missing '|': {after_cursor!r}")
            iso, cursor_id = after_cursor.split("|", 1)
            cursor_from = datetime.fromisoformat(iso)
            rows = [
                r for r in rows if (r.effective_from, r.id) < (cursor_from, cursor_id)
            ]
        return rows[: limit + 1]

    def has_paid_payslip_overlap(
        self,
        *,
        workspace_id: str,
        user_id: str,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> bool:
        self.lock_calls.append(
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
        )
        return self._locked

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
        row = PayRuleRow(
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
        self._rows[rule_id] = row
        return row

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
        existing = self._rows[rule_id]
        if existing.workspace_id != workspace_id:
            raise KeyError(rule_id)
        row = PayRuleRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            user_id=existing.user_id,
            currency=currency,
            base_cents_per_hour=base_cents_per_hour,
            overtime_multiplier=overtime_multiplier,
            night_multiplier=night_multiplier,
            weekend_multiplier=weekend_multiplier,
            effective_from=effective_from,
            effective_to=effective_to,
            created_by=existing.created_by,
            created_at=existing.created_at,
        )
        self._rows[rule_id] = row
        return row

    def soft_delete(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        now: datetime,
    ) -> PayRuleRow:
        existing = self._rows[rule_id]
        if existing.workspace_id != workspace_id:
            raise KeyError(rule_id)
        # Mirror the SA concretion's idempotent stamp: preserve any
        # earlier retirement timestamp (``effective_to`` already set
        # in the past) so a re-delete does not destroy historical
        # evidence.
        new_to = (
            existing.effective_to
            if existing.effective_to is not None and existing.effective_to <= now
            else now
        )
        row = PayRuleRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            user_id=existing.user_id,
            currency=existing.currency,
            base_cents_per_hour=existing.base_cents_per_hour,
            overtime_multiplier=existing.overtime_multiplier,
            night_multiplier=existing.night_multiplier,
            weekend_multiplier=existing.weekend_multiplier,
            effective_from=existing.effective_from,
            effective_to=new_to,
            created_by=existing.created_by,
            created_at=existing.created_at,
        )
        self._rows[rule_id] = row
        return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_body(**overrides: Any) -> PayRuleCreate:
    payload: dict[str, Any] = {
        "currency": "EUR",
        "base_cents_per_hour": 1500,
        "overtime_multiplier": Decimal("1.5"),
        "night_multiplier": Decimal("1.25"),
        "weekend_multiplier": Decimal("1.5"),
        "effective_from": _PINNED,
        "effective_to": None,
    }
    payload.update(overrides)
    return PayRuleCreate.model_validate(payload)


def _update_body(**overrides: Any) -> PayRuleUpdate:
    payload: dict[str, Any] = {
        "currency": "EUR",
        "base_cents_per_hour": 2000,
        "overtime_multiplier": Decimal("1.5"),
        "night_multiplier": Decimal("1.25"),
        "weekend_multiplier": Decimal("1.5"),
        "effective_from": _PINNED,
        "effective_to": None,
    }
    payload.update(overrides)
    return PayRuleUpdate.model_validate(payload)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    """Happy path + every validation refusal."""

    def test_round_trip(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        view = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(),
            clock=clock,
        )
        assert view.user_id == _USER_ID
        assert view.workspace_id == _WS_ID
        assert view.currency == "EUR"
        assert view.base_cents_per_hour == 1500
        assert view.created_by == _ACTOR_ID
        # One audit row landed on the fake session.
        assert len(repo.session.added) == 1
        assert repo.session.added[0].action == "pay_rule.created"
        assert repo.session.added[0].entity_kind == "pay_rule"

    def test_currency_lowercased_normalises(self) -> None:
        repo = _FakeRepo()
        view = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(currency="eur"),
            clock=FrozenClock(_PINNED),
        )
        assert view.currency == "EUR"

    def test_currency_outside_allow_list_raises(self) -> None:
        with pytest.raises(PayRuleInvariantViolated) as exc_info:
            create_rule(
                _FakeRepo(),
                _ctx(),
                user_id=_USER_ID,
                body=_create_body(currency="ZZZ"),
                clock=FrozenClock(_PINNED),
            )
        assert "ISO-4217" in str(exc_info.value)

    def test_currency_wrong_length_raises_validation_error(self) -> None:
        # Pydantic guard fires first on a wire-shape violation.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _create_body(currency="EU")

    @pytest.mark.parametrize(
        "field",
        [
            "overtime_multiplier",
            "night_multiplier",
            "weekend_multiplier",
        ],
    )
    def test_multiplier_below_minimum_raises(self, field: str) -> None:
        with pytest.raises(PayRuleInvariantViolated) as exc_info:
            create_rule(
                _FakeRepo(),
                _ctx(),
                user_id=_USER_ID,
                body=_create_body(**{field: Decimal("0.5")}),
                clock=FrozenClock(_PINNED),
            )
        assert field in str(exc_info.value)

    @pytest.mark.parametrize(
        "field",
        [
            "overtime_multiplier",
            "night_multiplier",
            "weekend_multiplier",
        ],
    )
    def test_multiplier_above_maximum_raises(self, field: str) -> None:
        with pytest.raises(PayRuleInvariantViolated) as exc_info:
            create_rule(
                _FakeRepo(),
                _ctx(),
                user_id=_USER_ID,
                body=_create_body(**{field: Decimal("5.5")}),
                clock=FrozenClock(_PINNED),
            )
        assert field in str(exc_info.value)

    def test_multiplier_at_boundary_accepted(self) -> None:
        # 1.0 and 5.0 are inclusive boundaries.
        view = create_rule(
            _FakeRepo(),
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(
                overtime_multiplier=Decimal("1.0"),
                weekend_multiplier=Decimal("5.0"),
            ),
            clock=FrozenClock(_PINNED),
        )
        assert view.overtime_multiplier == Decimal("1.0")
        assert view.weekend_multiplier == Decimal("5.0")

    def test_effective_to_equal_to_from_raises(self) -> None:
        with pytest.raises(PayRuleInvariantViolated) as exc_info:
            create_rule(
                _FakeRepo(),
                _ctx(),
                user_id=_USER_ID,
                body=_create_body(effective_from=_PINNED, effective_to=_PINNED),
                clock=FrozenClock(_PINNED),
            )
        assert "effective_to" in str(exc_info.value)

    def test_effective_to_before_from_raises(self) -> None:
        with pytest.raises(PayRuleInvariantViolated):
            create_rule(
                _FakeRepo(),
                _ctx(),
                user_id=_USER_ID,
                body=_create_body(
                    effective_from=_PINNED,
                    effective_to=_PINNED - timedelta(hours=1),
                ),
                clock=FrozenClock(_PINNED),
            )

    def test_open_ended_window_accepted(self) -> None:
        view = create_rule(
            _FakeRepo(),
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_to=None),
            clock=FrozenClock(_PINNED),
        )
        assert view.effective_to is None

    def test_negative_base_cents_rejected_by_dto(self) -> None:
        from pydantic import ValidationError

        # Pydantic ``ge=0`` fires before the domain validator.
        with pytest.raises(ValidationError):
            _create_body(base_cents_per_hour=-1)

    def test_implausibly_high_base_cents_rejected_by_dto(self) -> None:
        from pydantic import ValidationError

        # The DTO carries ``le=_BASE_CENTS_MAX`` so a unit-confusion
        # bug (annual salary into hourly cents) trips the wire-shape
        # validator before the domain layer.
        with pytest.raises(ValidationError):
            _create_body(base_cents_per_hour=10_000_000)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_round_trip(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo, _ctx(), user_id=_USER_ID, body=_create_body(), clock=clock
        )
        # Reset audit captures so the assertion below is unambiguous.
        repo.session.added.clear()

        updated = update_rule(
            repo,
            _ctx(),
            rule_id=created.id,
            body=_update_body(base_cents_per_hour=2500),
            clock=clock,
        )
        assert updated.base_cents_per_hour == 2500
        assert len(repo.session.added) == 1
        assert repo.session.added[0].action == "pay_rule.updated"

    def test_unknown_id_raises_not_found(self) -> None:
        with pytest.raises(PayRuleNotFound):
            update_rule(
                _FakeRepo(),
                _ctx(),
                rule_id="01HWUNKNOWN000000000000000",
                body=_update_body(),
                clock=FrozenClock(_PINNED),
            )

    def test_locked_period_refused(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo, _ctx(), user_id=_USER_ID, body=_create_body(), clock=clock
        )
        # Flip the lock signal — the next update must refuse.
        repo._locked = True

        with pytest.raises(PayRuleLocked):
            update_rule(
                repo,
                _ctx(),
                rule_id=created.id,
                body=_update_body(base_cents_per_hour=2500),
                clock=clock,
            )

    def test_lock_check_uses_existing_window(self) -> None:
        """Locked-period guard targets the row's stored window.

        A caller that tries to widen ``effective_to`` mid-update must
        not be able to dodge the lock by sending a future-only window
        — the guard runs against the row's stored ``effective_from``
        / ``effective_to`` (the historical evidence anchor), not the
        new body's window.
        """
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_from=_PINNED, effective_to=None),
            clock=clock,
        )
        repo._locked = True
        repo.lock_calls.clear()

        with pytest.raises(PayRuleLocked):
            update_rule(
                repo,
                _ctx(),
                rule_id=created.id,
                # Try to dodge by squeezing the new window into a
                # future range that never overlaps a paid period.
                body=_update_body(
                    effective_from=_PINNED + timedelta(days=365),
                    effective_to=_PINNED + timedelta(days=730),
                ),
                clock=clock,
            )
        assert len(repo.lock_calls) == 1
        # Guard saw the **existing** window, not the new one.
        assert repo.lock_calls[0]["effective_from"] == _PINNED
        assert repo.lock_calls[0]["effective_to"] is None

    def test_validation_runs_after_lock_check(self) -> None:
        """A locked rule with bad payload still surfaces the lock error.

        Order of checks: not-found → locked → DTO validation. A
        caller editing a locked rule should see ``PayRuleLocked``
        even when their payload is also invalid — the lock is the
        sharper signal.
        """
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo, _ctx(), user_id=_USER_ID, body=_create_body(), clock=clock
        )
        repo._locked = True

        with pytest.raises(PayRuleLocked):
            update_rule(
                repo,
                _ctx(),
                rule_id=created.id,
                body=_update_body(currency="ZZZ"),
                clock=clock,
            )


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------


class TestSoftDelete:
    def test_round_trip_sets_effective_to_now(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo, _ctx(), user_id=_USER_ID, body=_create_body(), clock=clock
        )
        repo.session.added.clear()

        later = FrozenClock(_PINNED + timedelta(days=1))
        view = soft_delete_rule(repo, _ctx(), rule_id=created.id, clock=later)
        assert view.effective_to == _PINNED + timedelta(days=1)
        # Original rule remains readable post-delete (payroll evidence).
        assert get_rule(repo, _ctx(), rule_id=created.id).effective_to == (
            _PINNED + timedelta(days=1)
        )
        assert len(repo.session.added) == 1
        assert repo.session.added[0].action == "pay_rule.deleted"

    def test_unknown_id_raises_not_found(self) -> None:
        with pytest.raises(PayRuleNotFound):
            soft_delete_rule(
                _FakeRepo(),
                _ctx(),
                rule_id="01HWUNKNOWN000000000000000",
                clock=FrozenClock(_PINNED),
            )

    def test_locked_period_refused(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo, _ctx(), user_id=_USER_ID, body=_create_body(), clock=clock
        )
        repo._locked = True
        with pytest.raises(PayRuleLocked):
            soft_delete_rule(repo, _ctx(), rule_id=created.id, clock=clock)

    def test_soft_delete_preserves_earlier_retirement(self) -> None:
        """Re-deleting an already-retired rule keeps the original timestamp.

        The Protocol contract is "no-op write that still reports the
        (unchanged) projection". Overwriting ``effective_to`` with a
        later ``now`` would destroy the historical evidence of when
        the rule was first retired — payroll-law audit trails must
        keep that anchor stable.
        """
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        created = create_rule(
            repo, _ctx(), user_id=_USER_ID, body=_create_body(), clock=clock
        )
        # First retirement at T+1.
        first_retirement = _PINNED + timedelta(days=1)
        first_view = soft_delete_rule(
            repo,
            _ctx(),
            rule_id=created.id,
            clock=FrozenClock(first_retirement),
        )
        assert first_view.effective_to == first_retirement

        # Second retirement at T+10 (later) — must NOT overwrite.
        repo.session.added.clear()
        second_retirement = _PINNED + timedelta(days=10)
        second_view = soft_delete_rule(
            repo,
            _ctx(),
            rule_id=created.id,
            clock=FrozenClock(second_retirement),
        )
        # Earlier retirement timestamp survives.
        assert second_view.effective_to == first_retirement
        # Audit row still emitted (the operation is observable even
        # when the row state does not change).
        assert len(repo.session.added) == 1
        assert repo.session.added[0].action == "pay_rule.deleted"

    def test_soft_delete_lowers_retirement_date_when_now_earlier(self) -> None:
        """Stamping with a strictly earlier ``now`` brings the retirement forward.

        Pathological-but-defensible: if the existing ``effective_to``
        is in the future (e.g. a manager set a planned retirement
        date) and an admin runs ``soft_delete`` now, the rule should
        be retired immediately rather than waiting for the future
        date. The bug is one-directional — preserve the earliest
        retirement, not just any prior value.
        """
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        # Create with a future ``effective_to``.
        future = _PINNED + timedelta(days=365)
        create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_to=future),
            clock=clock,
        )
        rule_id = next(iter(repo._rows))

        view = soft_delete_rule(
            repo,
            _ctx(),
            rule_id=rule_id,
            clock=FrozenClock(_PINNED + timedelta(days=1)),
        )
        # ``now`` (T+1) is earlier than the previous future-dated
        # ``effective_to`` (T+365), so the retirement comes forward.
        assert view.effective_to == _PINNED + timedelta(days=1)


# ---------------------------------------------------------------------------
# List + get
# ---------------------------------------------------------------------------


class TestListAndGet:
    def test_get_unknown_raises(self) -> None:
        with pytest.raises(PayRuleNotFound):
            get_rule(_FakeRepo(), _ctx(), rule_id="01HWUNKNOWN000000000000000")

    def test_list_orders_newest_first(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        # Create two rules with distinct ``effective_from`` values.
        # Use the same user so they share a chain.
        first = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_from=_PINNED),
            clock=clock,
        )
        later = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_from=_PINNED + timedelta(days=30)),
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
        )
        results = list_rules(repo, _ctx(), user_id=_USER_ID, limit=10)
        ids = [v.id for v in results]
        # Newest first.
        assert ids == [later.id, first.id]

    def test_list_returns_limit_plus_one_for_pagination(self) -> None:
        repo = _FakeRepo()
        for i in range(3):
            create_rule(
                repo,
                _ctx(),
                user_id=_USER_ID,
                body=_create_body(effective_from=_PINNED + timedelta(days=i)),
                clock=FrozenClock(_PINNED + timedelta(seconds=i)),
            )
        # ``limit=2`` means three rows surface so the router can
        # decide ``has_more=True``.
        results = list_rules(repo, _ctx(), user_id=_USER_ID, limit=2)
        assert len(results) == 3

    def test_pagination_stable_when_effective_from_disagrees_with_ulid_order(
        self,
    ) -> None:
        """Composite cursor walks desc pages even with backdated rules.

        Manager creates rule A with ``effective_from = T+30`` first
        (so A has the lower ULID), then creates rule B with
        ``effective_from = T`` (later ULID). The desc-by-effective_from
        order is ``[A, B]``. A ULID-only cursor that pointed at A
        (the first row of page 1) would over-fetch A on page 2 (its
        ULID is *less* than B's), repeating the row. The composite
        cursor must skip A cleanly and hand back B exactly once.
        """
        from app.domain.payroll.rules import cursor_for_view

        repo = _FakeRepo()
        # A: created first (lower ULID), but ``effective_from`` is
        # in the future.
        a = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_from=_PINNED + timedelta(days=30)),
            clock=FrozenClock(_PINNED),
        )
        # B: created second (higher ULID), but ``effective_from`` is
        # earlier — backdated rule.
        b = create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(effective_from=_PINNED),
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
        )
        # ULID order: a.id < b.id, but desc-effective-from puts A first.
        assert a.id < b.id

        # Page 1 (limit=1).
        page1 = list_rules(repo, _ctx(), user_id=_USER_ID, limit=1)
        # Repo returns ``limit + 1`` = 2 rows so the router can
        # decide ``has_more=True``; the first one is A.
        assert page1[0].id == a.id
        cursor = cursor_for_view(page1[0])

        # Page 2 with the composite cursor — must NOT re-include A.
        page2 = list_rules(
            repo,
            _ctx(),
            user_id=_USER_ID,
            limit=1,
            after_cursor=cursor,
        )
        ids = [v.id for v in page2]
        assert a.id not in ids
        assert b.id in ids

    def test_list_other_user_isolated(self) -> None:
        repo = _FakeRepo()
        create_rule(
            repo,
            _ctx(),
            user_id=_USER_ID,
            body=_create_body(),
            clock=FrozenClock(_PINNED),
        )
        # Another user's chain is empty.
        results = list_rules(
            repo,
            _ctx(),
            user_id="01HWA00000000000000000USR9",
            limit=10,
        )
        assert results == []
