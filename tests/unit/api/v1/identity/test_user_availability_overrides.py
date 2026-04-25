"""HTTP-level tests for ``/user_availability_overrides`` (cd-uqw1).

Covers the CRUD + hybrid-approval state-machine contract per spec §12
"Users / work roles / settings" and §06 "user_availability_overrides":

* Hybrid approval matrix (§06 "Approval logic (hybrid model)"):
  * Worker adds hours on an off-pattern day → auto-approved.
  * Worker confirms off (override available=false on off pattern) → auto-approved.
  * Worker removes a working day → approval_required=True, pending.
  * Worker narrows working hours → approval_required=True, pending.
  * Worker extends working hours → auto-approved.
  * Manager creating any override → auto-approved regardless.
* Authorisation: workers can only manage their own; managers
  manage anyone's.
* Cursor pagination per §12.
* Cross-workspace probes collapse to 404.
* Audit row lands on every transition.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserWeeklyAvailability,
)
from app.api.v1.user_availability_overrides import (
    build_user_availability_overrides_router,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [("", build_user_availability_overrides_router())], factory, ctx
    )


def _seed_weekly(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    weekday: int,
    starts_local: time | None,
    ends_local: time | None,
) -> None:
    """Insert one ``user_weekly_availability`` row for the user/weekday.

    The :func:`tests.factories.identity` helpers don't seed a weekly
    pattern by default; the hybrid approval calculator needs one to
    exercise the "Working pattern" branches in §06.
    """
    with factory() as s:
        s.add(
            UserWeeklyAvailability(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                weekday=weekday,
                starts_local=starts_local,
                ends_local=ends_local,
                updated_at=datetime.now(tz=UTC),
            )
        )
        s.commit()


def _seed_worker(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=email.split("@")[0])
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


# ---------------------------------------------------------------------------
# Hybrid approval matrix — each row of §06 "Approval logic (hybrid model)"
# ---------------------------------------------------------------------------


class TestHybridApprovalMatrix:
    """Each case from the §06 approval-logic table."""

    def test_worker_off_pattern_adds_hours_auto_approves(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Off pattern + override available=true → ``approval_required=False``.

        2026-05-04 is a Monday (weekday=0). Seed a null-hours row so
        the pattern is "off"; a worker adding work for that date
        auto-approves per §06.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=None,
            ends_local=None,
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "13:00:00",
                "reason": "Cover gap",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False
        assert body["approved_at"] is not None
        assert body["approved_by"] == worker_id

    def test_worker_off_pattern_confirms_off_auto_approves(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Off pattern + override available=false → ``approval_required=False``."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=None,
            ends_local=None,
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False
        assert body["approved_at"] is not None

    def test_worker_working_removes_day_requires_approval(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Working pattern + override available=false → pending."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is True
        assert body["approved_at"] is None
        assert body["approved_by"] is None

    def test_worker_working_narrows_hours_requires_approval(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Working 09-17 + override 09-12 (narrows) → pending."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "12:00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is True
        assert body["approved_at"] is None

    def test_worker_working_extends_hours_auto_approves(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Working 09-17 + override 09-19 (extends end) → auto-approves."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "19:00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False
        assert body["approved_at"] is not None

    def test_worker_working_matches_hours_auto_approves(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Working 09-17 + override 09-17 (matches) → auto-approves.

        Matching is degenerate "no change" — no shrink, no expansion,
        same coverage as the weekly pattern. Per §06 it doesn't reduce
        availability, so no approval is required.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "17:00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False

    def test_worker_working_null_hours_falls_back_auto_approves(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Working pattern + override available=true with null hours → auto-approves.

        §06 "user_availability_overrides" §"Invariants": null hours on
        an ``available=true`` override falls back to the weekly
        pattern's hours — same coverage as the pattern → no approval
        required.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": True},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False

    def test_no_weekly_row_treated_as_off(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """No weekly row at all → off → adding hours auto-approves.

        A user with no row for the date's weekday should be treated as
        "off" by the approval calculator — same surface as a row with
        both ``starts_local`` and ``ends_local`` null.
        """
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "12:00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False

    def test_manager_create_for_worker_auto_approves_even_when_required(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Manager-created row always auto-approves even when matrix says required.

        Worker has a working pattern; manager creates an override that
        narrows it. The matrix would say ``approval_required=True``,
        but the catalog gate ``availability_overrides.edit_others``
        triggers the auto-approve override per §06's "Owner/manager-
        created overrides are always auto-approved".
        """
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker(
            factory, workspace_id=ws_id, email="amanager@example.com"
        )
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "user_id": worker_id,
                "date": "2026-05-04",
                "available": True,
                "starts_local": "10:00:00",
                "ends_local": "12:00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Matrix: worker-narrows-hours → True. But manager-created.
        assert body["approval_required"] is True
        assert body["approved_at"] is not None
        assert body["approved_by"] == ctx.actor_id


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


class TestAuthorisation:
    def test_worker_cannot_create_for_other(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker creating an override for someone else collapses to 403."""
        ctx, factory, ws_id, _ = worker_ctx
        other_id = _seed_worker(factory, workspace_id=ws_id, email="other@example.com")
        client = _client(ctx, factory)

        resp = client.post(
            "/user_availability_overrides",
            json={
                "user_id": other_id,
                "date": "2026-05-04",
                "available": False,
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "permission_denied"

    def test_worker_cannot_list_inbox(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Bare GET (no ``user_id``) is the manager inbox — 403 for workers."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/user_availability_overrides")
        assert resp.status_code == 403

    def test_worker_cannot_list_others(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker listing ``?user_id=<other>`` collapses to 403."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get(
            "/user_availability_overrides?user_id=01HWOTHER000000000000000"
        )
        assert resp.status_code == 403

    def test_worker_can_list_self_no_capability_required(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker can list their own overrides without ``view_others``."""
        ctx, factory, _, worker_id = worker_ctx
        client = _client(ctx, factory)
        client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        )

        resp = client.get(f"/user_availability_overrides?user_id={worker_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["user_id"] == worker_id

    def test_owner_lists_inbox(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner sees every workspace override on the bare GET."""
        ctx, factory, ws_id = owner_ctx
        # Seed two overrides: one by the owner, one by a fresh worker.
        client = _client(ctx, factory)
        client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        )
        worker_id = _seed_worker(factory, workspace_id=ws_id, email="ww@example.com")
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        worker_client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-05", "available": False},
        )

        resp = client.get("/user_availability_overrides")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["data"]) == 2
        assert {r["user_id"] for r in body["data"]} == {ctx.actor_id, worker_id}

    def test_worker_cannot_approve(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker hitting approve on a pending override is 403."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)
        # Worker creates a pending override (narrows hours).
        override = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "12:00:00",
            },
        ).json()
        assert override["approval_required"] is True
        resp = client.post(f"/user_availability_overrides/{override['id']}/approve")
        assert resp.status_code == 403

    def test_worker_cannot_reject(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)
        override = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()
        resp = client.post(f"/user_availability_overrides/{override['id']}/reject")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# State machine: approve / reject / patch / delete
# ---------------------------------------------------------------------------


class TestApprove:
    def test_owner_approves_worker_pending(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner approves a worker's pending override; second approve = 409."""
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker(factory, workspace_id=ws_id, email="wapp@example.com")
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        # Worker requests "off Monday" → pending.
        override = worker_client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()
        assert override["approved_at"] is None

        owner_client = _client(ctx, factory)
        resp = owner_client.post(
            f"/user_availability_overrides/{override['id']}/approve"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["approved_at"] is not None
        assert body["approved_by"] == ctx.actor_id

        # Second approve = 409.
        resp2 = owner_client.post(
            f"/user_availability_overrides/{override['id']}/approve"
        )
        assert resp2.status_code == 409

        # Audit chain: created + approved.
        with factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(AuditLog.entity_id == override["id"])
                ).all()
            )
        actions = sorted(r.action for r in rows)
        assert actions == [
            "user_availability_override.approved",
            "user_availability_override.created",
        ]


class TestReject:
    def test_owner_rejects_with_reason(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Reject soft-deletes, folds reason into ``reason``, audits transition."""
        ctx, factory, ws_id = owner_ctx
        worker_id = _seed_worker(factory, workspace_id=ws_id, email="wrej@example.com")
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        override = worker_client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": False,
                "reason": "Doctor",
            },
        ).json()

        owner_client = _client(ctx, factory)
        resp = owner_client.post(
            f"/user_availability_overrides/{override['id']}/reject",
            json={"reason_md": "Coverage gap"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_at"] is not None
        assert "Doctor" in (body["reason"] or "")
        assert "Rejected: Coverage gap" in (body["reason"] or "")

        # Row hidden from default listing (tombstone filter).
        listing = owner_client.get(
            f"/user_availability_overrides?user_id={worker_id}"
        ).json()
        assert listing["data"] == []

        # Audit row carries the rejected action.
        with factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(AuditLog.entity_id == override["id"])
                ).all()
            )
        assert any(r.action == "user_availability_override.rejected" for r in rows)

    def test_reject_approved_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Rejecting an already-approved row collapses to 409."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        # Owner self-create is auto-approved.
        override = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()
        resp = client.post(f"/user_availability_overrides/{override['id']}/reject")
        assert resp.status_code == 409


class TestPatch:
    def test_worker_can_edit_own_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker editing their own pending override succeeds."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)
        # Worker requests "off Monday" → pending.
        created = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()
        assert created["approved_at"] is None

        resp = client.patch(
            f"/user_availability_overrides/{created['id']}",
            json={"reason": "Updated context"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reason"] == "Updated context"

    def test_patch_approved_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """An approved override rejects PATCH with 409."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        # Owner self-create lands approved.
        created = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()
        resp = client.patch(
            f"/user_availability_overrides/{created['id']}",
            json={"reason": "x"},
        )
        assert resp.status_code == 409
        assert (
            resp.json()["detail"]["error"]
            == "user_availability_override_transition_forbidden"
        )

    def test_patch_invalid_hours_pairing_422(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """PATCHing only ``starts_local`` (clearing ``ends_local``) → 422."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)
        # Worker requests narrowed hours → pending.
        created = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "12:00:00",
            },
        ).json()

        # Half-set PATCH: clear ends_local, keep starts_local.
        resp = client.patch(
            f"/user_availability_overrides/{created['id']}",
            json={"ends_local": None},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "user_availability_override_invariant"

    def test_patch_explicit_null_available_is_unchanged(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Sending ``available: null`` is "unchanged", not "set to false".

        Regression: an earlier shape evaluated ``not new_available``
        when the patch carried an explicit JSON ``null`` for
        ``available``, which Python's truthiness reading collapsed to
        the "available=false with hours" rule and produced a spurious
        422. ``available`` is non-nullable on the row, so a sent
        ``null`` must be treated as "field not provided" — nothing
        else mutates and the call is a no-op.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)
        # Pending working override with hours.
        created = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "12:00:00",
            },
        ).json()

        # Explicit ``available: null`` alone — must not trip the
        # "available=false carries hours" branch.
        resp = client.patch(
            f"/user_availability_overrides/{created['id']}",
            json={"available": None},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        assert body["starts_local"] == "09:00:00"
        assert body["ends_local"] == "12:00:00"


class TestDelete:
    def test_worker_withdraws_own_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker DELETEs their own pending override; row is tombstoned."""
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)
        override = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()

        resp = client.delete(f"/user_availability_overrides/{override['id']}")
        assert resp.status_code == 204

        with factory() as s:
            row = s.get(UserAvailabilityOverride, override["id"])
            assert row is not None
            assert row.deleted_at is not None

        # Second DELETE = 404.
        resp2 = client.delete(f"/user_availability_overrides/{override['id']}")
        assert resp2.status_code == 404

    def test_worker_cannot_delete_other(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A worker DELETing someone else's row is 403."""
        ctx, factory, ws_id = owner_ctx
        # Owner-created override (auto-approved).
        owner_client = _client(ctx, factory)
        override = owner_client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()
        # Worker tries to delete it.
        worker_id = _seed_worker(factory, workspace_id=ws_id, email="wdel@example.com")
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        resp = worker_client.delete(f"/user_availability_overrides/{override['id']}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_hours_pairing_422(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Half-set ``starts_local`` without ``ends_local`` rejects at the DTO."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
            },
        )
        assert resp.status_code == 422

    def test_backwards_window_422(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``ends_local <= starts_local`` rejects at the DTO."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "12:00:00",
                "ends_local": "09:00:00",
            },
        )
        assert resp.status_code == 422

    def test_unavailable_with_hours_422(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``available=false`` with hours rejects at the DTO."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": False,
                "starts_local": "09:00:00",
                "ends_local": "17:00:00",
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_list_paginated(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Cursor envelope walks forward across multiple pages."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        for i in range(3):
            client.post(
                "/user_availability_overrides",
                json={
                    "date": f"2026-05-{i + 1:02d}",
                    "available": False,
                },
            )

        resp = client.get("/user_availability_overrides?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        assert len(body["data"]) == 2

        resp2 = client.get(
            f"/user_availability_overrides?cursor={body['next_cursor']}&limit=2"
        )
        body2 = resp2.json()
        assert resp2.status_code == 200
        assert body2["has_more"] is False
        assert len(body2["data"]) == 1

    def test_list_filter_approved(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``?approved=true`` narrows to approved rows; ``=false`` to pending."""
        ctx, factory, ws_id = owner_ctx
        owner_client = _client(ctx, factory)
        # Owner self-create is auto-approved.
        owner_client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        )
        # Worker self-create on working day is pending.
        worker_id = _seed_worker(factory, workspace_id=ws_id, email="wfa@example.com")
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        worker_ctx_local = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = _client(worker_ctx_local, factory)
        worker_client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-11", "available": False},
        )

        approved = owner_client.get("/user_availability_overrides?approved=true").json()
        pending = owner_client.get("/user_availability_overrides?approved=false").json()
        assert len(approved["data"]) == 1
        assert approved["data"][0]["approved_at"] is not None
        assert len(pending["data"]) == 1
        assert pending["data"][0]["approved_at"] is None

    def test_list_filter_date_window(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``?from=`` / ``?to=`` slice the date range."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        for d in ("2026-05-01", "2026-06-01", "2026-07-01"):
            client.post(
                "/user_availability_overrides",
                json={"date": d, "available": False},
            )

        resp = client.get("/user_availability_overrides?from=2026-06-01&to=2026-06-30")
        body = resp.json()
        assert resp.status_code == 200
        assert len(body["data"]) == 1
        assert body["data"][0]["date"] == "2026-06-01"


# ---------------------------------------------------------------------------
# Cross-workspace
# ---------------------------------------------------------------------------


class TestCrossWorkspace:
    def test_cross_workspace_blocked(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A row in workspace A is invisible from workspace B's caller."""
        ctx_a, factory, _ = owner_ctx
        client_a = _client(ctx_a, factory)
        override = client_a.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        ).json()

        with factory() as s:
            owner_b = bootstrap_user(
                s,
                email="owner-b-uao@example.com",
                display_name="Owner B",
            )
            ws_b = bootstrap_workspace(
                s,
                slug="ws-overrides-b",
                name="WS B",
                owner_user_id=owner_b.id,
            )
            s.commit()
            ctx_b = ctx_for(
                workspace_id=ws_b.id,
                workspace_slug=ws_b.slug,
                actor_id=owner_b.id,
                grant_role="manager",
                actor_was_owner_member=True,
            )
        client_b = _client(ctx_b, factory)

        listing = client_b.get("/user_availability_overrides").json()
        assert listing["data"] == []

        for path in (
            f"/user_availability_overrides/{override['id']}/approve",
            f"/user_availability_overrides/{override['id']}/reject",
        ):
            r = client_b.post(path)
            assert r.status_code == 404, path

        r_patch = client_b.patch(
            f"/user_availability_overrides/{override['id']}",
            json={"reason": "x"},
        )
        assert r_patch.status_code == 404

        r_del = client_b.delete(f"/user_availability_overrides/{override['id']}")
        assert r_del.status_code == 404


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_create_writes_audit(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``user_availability_override.created`` audit row lands."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/user_availability_overrides",
            json={"date": "2026-05-04", "available": False},
        )
        assert resp.status_code == 201
        override_id = resp.json()["id"]
        with factory() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(AuditLog.entity_id == override_id)
                ).all()
            )
        assert len(rows) == 1
        assert rows[0].action == "user_availability_override.created"
        assert rows[0].entity_kind == "user_availability_override"


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


class TestOpenApiShape:
    def test_routes_carry_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/user_availability_overrides"]["get"]
        assert "identity" in op["tags"]
        assert "user_availability_overrides" in op["tags"]
