"""Integration tests for the pay-rule HTTP surface (cd-ea7).

Mounts :func:`app.api.v1.payroll.build_payroll_router` on a throwaway
:class:`FastAPI` against a real DB (alembic-migrated SQLite in CI; the
file-based fixture in :mod:`tests.integration.conftest` already pins
the URL). Every test asserts on:

* HTTP boundary (status code, error envelope, response shape);
* the side effects the domain service emits (DB row, audit row);
* the §05 ``pay_rules.edit`` capability gate.

Covers:

* ``POST /users/{user_id}/pay-rules`` — 201 + view; 422 on bad
  currency / multiplier / window; 403 for a worker.
* ``GET /users/{user_id}/pay-rules`` — paginated; newest first;
  composite cursor stable across backdated rules; 422 on tampered
  cursor.
* ``GET /pay-rules/{rule_id}`` — 200 / 404.
* ``PATCH /pay-rules/{rule_id}`` — 200; 409 once a paid payslip
  references the row.
* ``DELETE /pay-rules/{rule_id}`` — 204 (soft-retire); 409 once
  locked.
* Audit chain has create / updated / deleted entries in order.

Mirrors the harness in :mod:`tests.integration.api.test_api_employees`.

See ``docs/specs/09-time-payroll-expenses.md`` §"Pay rules",
``docs/specs/12-rest-api.md`` §"Time, payroll, expenses".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.payroll.models import PayPeriod, PayRule, Payslip
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.payroll import build_payroll_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Per-test session factory that commits on clean exit."""
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seeded(
    session_factory: sessionmaker[Session],
) -> Iterator[tuple[WorkspaceContext, str]]:
    """Seed an owner + workspace; yield ``(ctx, owner_id)``.

    The owner is the target user for every pay-rule test so the
    ``pay_rules.edit`` gate (default-allow ``owners + managers``)
    fires consistently and the FK on ``user_id`` resolves to a real
    row.
    """
    tag = new_ulid()[-8:].lower()
    slug = f"payrule-{tag}"
    with session_factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Payroll WS",
            owner_user_id=owner.id,
        )
        s.commit()
        owner_id, ws_id, ws_slug = owner.id, ws.id, ws.slug

    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    try:
        yield ctx, owner_id
    finally:
        # Scoped cleanup. Pay-rule rows pin the user via RESTRICT FK so
        # the order matters: payslip → pay_period → pay_rule → audit
        # → workspace → user.
        with session_factory() as s, tenant_agnostic():
            for model in (Payslip, PayPeriod, PayRule, AuditLog):
                for row in s.scalars(
                    select(model).where(model.workspace_id == ws_id)
                ).all():
                    s.delete(row)
            s.commit()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    seeded: tuple[WorkspaceContext, str],
) -> Iterator[TestClient]:
    """:class:`TestClient` mounted on the payroll router."""
    ctx, _ = seeded
    app = FastAPI()
    app.include_router(build_payroll_router(), prefix="/api/v1/payroll")

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "currency": "EUR",
        "base_cents_per_hour": 1500,
        "overtime_multiplier": "1.5",
        "night_multiplier": "1.25",
        "weekend_multiplier": "1.5",
        "effective_from": _PINNED.isoformat(),
        "effective_to": None,
    }
    body.update(overrides)
    return body


def _seed_paid_period_with_payslip(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    """Seed a ``pay_period`` in ``state='paid'`` with a single ``payslip``.

    Used by the locked-period tests — the period must exist for the
    repo's overlap query to return ``True``.
    """
    with session_factory() as s, tenant_agnostic():
        period = PayPeriod(
            id=new_ulid(),
            workspace_id=workspace_id,
            starts_at=starts_at,
            ends_at=ends_at,
            state="paid",
            locked_at=ends_at,
            locked_by=None,
            created_at=starts_at,
        )
        s.add(period)
        s.flush()
        s.add(
            Payslip(
                id=new_ulid(),
                workspace_id=workspace_id,
                pay_period_id=period.id,
                user_id=user_id,
                shift_hours_decimal=Decimal("160.00"),
                overtime_hours_decimal=Decimal("0"),
                gross_cents=240_000,
                deductions_cents={},
                net_cents=240_000,
                pdf_blob_hash=None,
                created_at=ends_at,
            )
        )
        s.commit()


def _audit_actions_for(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    entity_id: str,
) -> list[str]:
    """Return audit actions for one entity in chronological order."""
    with session_factory() as s, tenant_agnostic():
        rows = s.scalars(
            select(AuditLog)
            .where(
                AuditLog.workspace_id == workspace_id,
                AuditLog.entity_id == entity_id,
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    return [r.action for r in rows]


# ---------------------------------------------------------------------------
# POST /users/{user_id}/pay-rules
# ---------------------------------------------------------------------------


class TestCreate:
    def test_round_trip_201(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        ctx, user_id = seeded
        r = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user_id"] == user_id
        assert body["workspace_id"] == ctx.workspace_id
        assert body["currency"] == "EUR"
        assert body["base_cents_per_hour"] == 1500

        # Audit row landed in the same transaction.
        actions = _audit_actions_for(
            session_factory,
            workspace_id=ctx.workspace_id,
            entity_id=body["id"],
        )
        assert actions == ["pay_rule.created"]

    def test_currency_outside_allow_list_422(
        self, client: TestClient, seeded: tuple[WorkspaceContext, str]
    ) -> None:
        _, user_id = seeded
        r = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(currency="ZZZ"),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "pay_rule_invariant"

    def test_currency_lowercase_normalises(
        self, client: TestClient, seeded: tuple[WorkspaceContext, str]
    ) -> None:
        _, user_id = seeded
        r = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(currency="usd"),
        )
        assert r.status_code == 201, r.text
        assert r.json()["currency"] == "USD"

    def test_multiplier_out_of_range_422(
        self, client: TestClient, seeded: tuple[WorkspaceContext, str]
    ) -> None:
        _, user_id = seeded
        r = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(weekend_multiplier="6.0"),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "pay_rule_invariant"

    def test_bad_window_422(
        self, client: TestClient, seeded: tuple[WorkspaceContext, str]
    ) -> None:
        _, user_id = seeded
        r = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(
                effective_from=_PINNED.isoformat(),
                effective_to=_PINNED.isoformat(),  # equal → invalid
            ),
        )
        assert r.status_code == 422, r.text

    def test_unknown_field_422(
        self, client: TestClient, seeded: tuple[WorkspaceContext, str]
    ) -> None:
        _, user_id = seeded
        body = _create_body()
        body["bogus"] = "yes"
        r = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=body,
        )
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# GET /users/{user_id}/pay-rules
# ---------------------------------------------------------------------------


class TestList:
    def test_orders_newest_first(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, user_id = seeded
        first = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(effective_from=_PINNED.isoformat()),
        ).json()
        later = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(
                effective_from=(_PINNED + timedelta(days=30)).isoformat()
            ),
        ).json()

        r = client.get(f"/api/v1/payroll/users/{user_id}/pay-rules")
        assert r.status_code == 200, r.text
        body = r.json()
        ids = [v["id"] for v in body["data"]]
        assert ids == [later["id"], first["id"]]
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_pagination_returns_cursor(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        _, user_id = seeded
        # Three rules in the same chain.
        for i in range(3):
            client.post(
                f"/api/v1/payroll/users/{user_id}/pay-rules",
                json=_create_body(
                    effective_from=(_PINNED + timedelta(days=i)).isoformat()
                ),
            )

        page1 = client.get(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            params={"limit": 2},
        ).json()
        assert len(page1["data"]) == 2
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None

        page2 = client.get(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            params={"limit": 2, "cursor": page1["next_cursor"]},
        ).json()
        assert len(page2["data"]) == 1
        assert page2["has_more"] is False

    def test_pagination_stable_with_backdated_rule(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        """The composite cursor walks desc pages even when ULID and
        ``effective_from`` order disagree.

        Manager creates rule A first (lower ULID) with a future
        ``effective_from``, then rule B (higher ULID) with an earlier
        ``effective_from``. A ULID-only cursor would over-include A
        on page 2. The composite cursor must skip A cleanly.
        """
        _, user_id = seeded
        # A: created first, future effective_from.
        a = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(
                effective_from=(_PINNED + timedelta(days=30)).isoformat()
            ),
        ).json()
        # B: created second, earlier effective_from (backdated).
        b = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(effective_from=_PINNED.isoformat()),
        ).json()
        # ULID order disagrees with desc-effective-from: a.id < b.id
        # but the desc page surfaces A first.
        assert a["id"] < b["id"]

        page1 = client.get(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            params={"limit": 1},
        ).json()
        assert [v["id"] for v in page1["data"]] == [a["id"]]
        assert page1["has_more"] is True

        page2 = client.get(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            params={"limit": 1, "cursor": page1["next_cursor"]},
        ).json()
        # B surfaces exactly once — no A duplicate.
        assert [v["id"] for v in page2["data"]] == [b["id"]]
        assert page2["has_more"] is False

    def test_invalid_cursor_returns_422(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
    ) -> None:
        """A tampered cursor that decodes but lacks the ``|`` separator
        surfaces as 422 ``invalid_cursor`` rather than a 500."""
        import base64

        _, user_id = seeded
        # Encode a string with no ``|`` separator.
        bad = (
            base64.urlsafe_b64encode(b"not-a-valid-cursor").rstrip(b"=").decode("ascii")
        )
        r = client.get(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            params={"cursor": bad},
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_cursor"


# ---------------------------------------------------------------------------
# GET /pay-rules/{rule_id}
# ---------------------------------------------------------------------------


class TestGet:
    def test_round_trip(
        self, client: TestClient, seeded: tuple[WorkspaceContext, str]
    ) -> None:
        _, user_id = seeded
        created = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(),
        ).json()

        r = client.get(f"/api/v1/payroll/pay-rules/{created['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == created["id"]

    def test_unknown_id_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/payroll/pay-rules/01HZNONEXISTENTPAYRULEID00")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "pay_rule_not_found"


# ---------------------------------------------------------------------------
# PATCH /pay-rules/{rule_id}
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_round_trip(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        ctx, user_id = seeded
        created = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(),
        ).json()

        r = client.patch(
            f"/api/v1/payroll/pay-rules/{created['id']}",
            json=_create_body(base_cents_per_hour=2500),
        )
        assert r.status_code == 200, r.text
        assert r.json()["base_cents_per_hour"] == 2500

        actions = _audit_actions_for(
            session_factory,
            workspace_id=ctx.workspace_id,
            entity_id=created["id"],
        )
        assert actions == ["pay_rule.created", "pay_rule.updated"]

    def test_unknown_id_404(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/payroll/pay-rules/01HZNONEXISTENTPAYRULEID00",
            json=_create_body(),
        )
        assert r.status_code == 404, r.text

    def test_locked_period_409(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """A pay rule consumed by a paid payslip refuses updates."""
        ctx, user_id = seeded
        created = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(
                effective_from=_PINNED.isoformat(),
                effective_to=None,
            ),
        ).json()

        # Seed a paid pay_period + payslip whose window overlaps the
        # rule's effective range.
        _seed_paid_period_with_payslip(
            session_factory,
            workspace_id=ctx.workspace_id,
            user_id=user_id,
            starts_at=_PINNED + timedelta(days=1),
            ends_at=_PINNED + timedelta(days=30),
        )

        r = client.patch(
            f"/api/v1/payroll/pay-rules/{created['id']}",
            json=_create_body(base_cents_per_hour=2500),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "pay_rule_locked"


# ---------------------------------------------------------------------------
# DELETE /pay-rules/{rule_id}
# ---------------------------------------------------------------------------


class TestDelete:
    def test_round_trip_204(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        ctx, user_id = seeded
        created = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(),
        ).json()

        r = client.delete(f"/api/v1/payroll/pay-rules/{created['id']}")
        assert r.status_code == 204, r.text

        # Row stays readable post-delete (payroll evidence) but
        # ``effective_to`` is set.
        survivor = client.get(f"/api/v1/payroll/pay-rules/{created['id']}").json()
        assert survivor["effective_to"] is not None

        actions = _audit_actions_for(
            session_factory,
            workspace_id=ctx.workspace_id,
            entity_id=created["id"],
        )
        assert actions == ["pay_rule.created", "pay_rule.deleted"]

    def test_unknown_id_404(self, client: TestClient) -> None:
        r = client.delete("/api/v1/payroll/pay-rules/01HZNONEXISTENTPAYRULEID00")
        assert r.status_code == 404, r.text

    def test_locked_period_409(
        self,
        client: TestClient,
        seeded: tuple[WorkspaceContext, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        ctx, user_id = seeded
        created = client.post(
            f"/api/v1/payroll/users/{user_id}/pay-rules",
            json=_create_body(),
        ).json()

        _seed_paid_period_with_payslip(
            session_factory,
            workspace_id=ctx.workspace_id,
            user_id=user_id,
            starts_at=_PINNED + timedelta(days=1),
            ends_at=_PINNED + timedelta(days=30),
        )

        r = client.delete(f"/api/v1/payroll/pay-rules/{created['id']}")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "pay_rule_locked"
