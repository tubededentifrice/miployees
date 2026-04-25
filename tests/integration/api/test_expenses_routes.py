"""Integration tests for :mod:`app.api.v1.expenses` HTTP surface (cd-t6y2).

Mounts the expenses router on a throwaway FastAPI app, overrides the
workspace-context + db-session + storage deps with seeded fixtures,
and drives the full router → domain → DB chain over HTTP.

Coverage:

* Worker self-service — create, patch (draft only), submit, cancel,
  attachments (happy + edge cases), pagination.
* Cross-user listing — 403 without ``expenses.approve``.
* Manager flow — approve (no edits / with edits / wrong state),
  reject (empty reason / happy), reimburse (happy / replay).
* Cross-tenant 404.
* Manager queue — worker probe → 403, manager → only submitted.
* Autofill placeholder → 501.

See ``docs/specs/12-rest-api.md`` §"Time, payroll, expenses",
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.workspace.models import WorkEngagement
from app.api.deps import current_workspace_context, db_session, get_storage
from app.api.v1.expenses import router as expenses_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Per-test session factory that commits on clean exit."""
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def storage() -> InMemoryStorage:
    """Shared in-memory storage backend across the test client."""
    return InMemoryStorage()


def _push_blob(storage: InMemoryStorage, *, payload: bytes = b"x") -> str:
    """Write a fake blob into ``storage`` and return its 64-char hash.

    The hash is content-derived so two distinct payloads round-trip
    distinct rows (the domain caps a claim at 10 attachments and the
    test that exercises the cap relies on distinct hashes).
    """
    import hashlib
    import io

    raw = hashlib.sha256(payload + new_ulid().encode()).hexdigest()
    storage.put(raw, io.BytesIO(payload), content_type="image/jpeg")
    return raw


def _grant(
    session: Session, *, workspace_id: str, user_id: str, grant_role: str
) -> None:
    """Insert a :class:`RoleGrant` so the authz seam fires through."""
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role=grant_role,
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        session.flush()


def _engagement(session: Session, *, workspace_id: str, user_id: str) -> str:
    """Seed a :class:`WorkEngagement` and return its id."""
    eng_id = new_ulid()
    with tenant_agnostic():
        session.add(
            WorkEngagement(
                id=eng_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind="payroll",
                supplier_org_id=None,
                pay_destination_id=None,
                reimbursement_destination_id=None,
                started_on=_PINNED.date(),
                archived_on=None,
                notes_md="",
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        session.flush()
    return eng_id


@pytest.fixture
def seeded(
    session_factory: sessionmaker[Session],
) -> Iterator[dict[str, Any]]:
    """Seed a workspace + worker / manager / peer + engagements.

    Yields a dict with keys::

        workspace_id, slug, owner_id, owner_eng_id, manager_id,
        manager_eng_id, worker_id, worker_eng_id, peer_id, peer_eng_id,
        owner_ctx, manager_ctx, worker_ctx, peer_ctx,
        foreign_workspace_id, foreign_slug, foreign_eng_id, foreign_ctx
    """
    tag = new_ulid()[-8:].lower()
    slug = f"exp-{tag}"
    foreign_slug = f"exp-foreign-{tag}"
    handles: dict[str, Any] = {}
    with session_factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        manager = bootstrap_user(
            s, email=f"manager-{tag}@example.com", display_name="Manager"
        )
        worker = bootstrap_user(
            s, email=f"worker-{tag}@example.com", display_name="Worker"
        )
        peer = bootstrap_user(s, email=f"peer-{tag}@example.com", display_name="Peer")
        ws = bootstrap_workspace(
            s, slug=slug, name="Expenses WS", owner_user_id=owner.id
        )
        # Manager + worker + peer grants. ``bootstrap_workspace`` already
        # seeds the owner's grant; ours add the secondary roles.
        _grant(s, workspace_id=ws.id, user_id=manager.id, grant_role="manager")
        _grant(s, workspace_id=ws.id, user_id=worker.id, grant_role="worker")
        _grant(s, workspace_id=ws.id, user_id=peer.id, grant_role="worker")

        owner_eng = _engagement(s, workspace_id=ws.id, user_id=owner.id)
        manager_eng = _engagement(s, workspace_id=ws.id, user_id=manager.id)
        worker_eng = _engagement(s, workspace_id=ws.id, user_id=worker.id)
        peer_eng = _engagement(s, workspace_id=ws.id, user_id=peer.id)

        # Foreign workspace owned by the same person — the
        # cross-tenant probe should still 404 because the worker's
        # claim lives in the home workspace, not this one.
        foreign_ws = bootstrap_workspace(
            s, slug=foreign_slug, name="Foreign WS", owner_user_id=owner.id
        )
        foreign_eng = _engagement(s, workspace_id=foreign_ws.id, user_id=owner.id)
        s.commit()

        handles.update(
            {
                "workspace_id": ws.id,
                "slug": ws.slug,
                "owner_id": owner.id,
                "owner_eng_id": owner_eng,
                "manager_id": manager.id,
                "manager_eng_id": manager_eng,
                "worker_id": worker.id,
                "worker_eng_id": worker_eng,
                "peer_id": peer.id,
                "peer_eng_id": peer_eng,
                "foreign_workspace_id": foreign_ws.id,
                "foreign_slug": foreign_ws.slug,
                "foreign_eng_id": foreign_eng,
            }
        )

    handles["owner_ctx"] = build_workspace_context(
        workspace_id=handles["workspace_id"],
        workspace_slug=handles["slug"],
        actor_id=handles["owner_id"],
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    handles["manager_ctx"] = build_workspace_context(
        workspace_id=handles["workspace_id"],
        workspace_slug=handles["slug"],
        actor_id=handles["manager_id"],
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
    )
    handles["worker_ctx"] = build_workspace_context(
        workspace_id=handles["workspace_id"],
        workspace_slug=handles["slug"],
        actor_id=handles["worker_id"],
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    handles["peer_ctx"] = build_workspace_context(
        workspace_id=handles["workspace_id"],
        workspace_slug=handles["slug"],
        actor_id=handles["peer_id"],
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    handles["foreign_ctx"] = build_workspace_context(
        workspace_id=handles["foreign_workspace_id"],
        workspace_slug=handles["foreign_slug"],
        actor_id=handles["owner_id"],
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    yield handles


def _client_for(
    session_factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    storage: InMemoryStorage,
) -> TestClient:
    """Return a TestClient pinned to ``ctx`` + ``storage``."""
    app = FastAPI()
    app.include_router(expenses_router, prefix="/api/v1/expenses")

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

    def _storage() -> InMemoryStorage:
        return storage

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    app.dependency_overrides[get_storage] = _storage
    return TestClient(app)


def _create_body(
    *,
    work_engagement_id: str,
    vendor: str = "Acme Hardware",
    purchased_at: datetime = _PURCHASED,
    currency: str = "EUR",
    total_amount_cents: int = 1250,
    category: str = "supplies",
) -> dict[str, Any]:
    """Build a JSON-shaped payload for ``POST /expenses``."""
    return {
        "work_engagement_id": work_engagement_id,
        "vendor": vendor,
        "purchased_at": purchased_at.isoformat(),
        "currency": currency,
        "total_amount_cents": total_amount_cents,
        "category": category,
    }


# ---------------------------------------------------------------------------
# Worker self-service
# ---------------------------------------------------------------------------


class TestWorkerSelfService:
    def test_create_then_patch_then_read(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["state"] == "draft"
            assert body["currency"] == "EUR"
            assert body["attachments"] == []
            cid = body["id"]

            # Patch while still draft works.
            r = client.patch(
                f"/api/v1/expenses/{cid}",
                json={"vendor": "Renamed Vendor", "note_md": "edited"},
            )
            assert r.status_code == 200, r.text
            patched = r.json()
            assert patched["vendor"] == "Renamed Vendor"
            assert patched["note_md"] == "edited"

            # Read back.
            r = client.get(f"/api/v1/expenses/{cid}")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == cid

    def test_patch_after_submit_returns_409(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]

            r = client.post(f"/api/v1/expenses/{cid}/submit")
            assert r.status_code == 200, r.text

            r = client.patch(
                f"/api/v1/expenses/{cid}",
                json={"vendor": "Too late"},
            )
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "claim_not_editable"

    def test_submit_twice_returns_409(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]

            r = client.post(f"/api/v1/expenses/{cid}/submit")
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "submitted"

            r = client.post(f"/api/v1/expenses/{cid}/submit")
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "claim_state_transition_invalid"

    def test_cancel_draft_soft_deletes(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]

            r = client.delete(f"/api/v1/expenses/{cid}")
            assert r.status_code == 204, r.text

            # Soft-deleted claims are invisible to subsequent reads.
            r = client.get(f"/api/v1/expenses/{cid}")
            assert r.status_code == 404, r.text
            assert r.json()["detail"]["error"] == "claim_not_found"

    def test_cancel_submitted_marks_rejected(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]
            client.post(f"/api/v1/expenses/{cid}/submit")

            r = client.delete(f"/api/v1/expenses/{cid}")
            assert r.status_code == 204, r.text

            # The row stays visible — cancel of a submitted claim flips
            # to rejected (worker-initiated) per the service contract.
            r = client.get(f"/api/v1/expenses/{cid}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "rejected"
            assert body["decision_note_md"]
            assert "cancelled by requester" in body["decision_note_md"]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


class TestAttachments:
    def _seed_draft(
        self,
        client: TestClient,
        seeded: dict[str, Any],
    ) -> str:
        r = client.post(
            "/api/v1/expenses",
            json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
        )
        return str(r.json()["id"])

    def test_attach_happy_path(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            cid = self._seed_draft(client, seeded)
            blob = _push_blob(storage)
            r = client.post(
                f"/api/v1/expenses/{cid}/attachments",
                json={
                    "blob_hash": blob,
                    "content_type": "image/jpeg",
                    "size_bytes": 1024,
                },
            )
            assert r.status_code == 201, r.text
            attached = r.json()
            assert attached["claim_id"] == cid
            assert attached["blob_hash"] == blob
            assert attached["kind"] == "receipt"

            # Listing surfaces the attachment too.
            r = client.get(f"/api/v1/expenses/{cid}/attachments")
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert len(data) == 1
            assert data[0]["id"] == attached["id"]

    def test_attach_rejects_bad_mime(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            cid = self._seed_draft(client, seeded)
            blob = _push_blob(storage)
            r = client.post(
                f"/api/v1/expenses/{cid}/attachments",
                json={
                    "blob_hash": blob,
                    "content_type": "image/svg+xml",
                    "size_bytes": 256,
                },
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "blob_mime_not_allowed"

    def test_attach_rejects_eleventh(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            cid = self._seed_draft(client, seeded)
            for i in range(10):
                blob = _push_blob(storage, payload=f"seed-{i}".encode())
                r = client.post(
                    f"/api/v1/expenses/{cid}/attachments",
                    json={
                        "blob_hash": blob,
                        "content_type": "image/jpeg",
                        "size_bytes": 64,
                    },
                )
                assert r.status_code == 201, r.text

            # 11th attempt — the cap fires.
            blob = _push_blob(storage, payload=b"overflow")
            r = client.post(
                f"/api/v1/expenses/{cid}/attachments",
                json={
                    "blob_hash": blob,
                    "content_type": "image/jpeg",
                    "size_bytes": 64,
                },
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "too_many_attachments"

    def test_detach_on_draft_succeeds(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            cid = self._seed_draft(client, seeded)
            blob = _push_blob(storage)
            r = client.post(
                f"/api/v1/expenses/{cid}/attachments",
                json={
                    "blob_hash": blob,
                    "content_type": "image/jpeg",
                    "size_bytes": 64,
                },
            )
            aid = r.json()["id"]

            r = client.delete(f"/api/v1/expenses/{cid}/attachments/{aid}")
            assert r.status_code == 204, r.text

            r = client.get(f"/api/v1/expenses/{cid}/attachments")
            assert r.json()["data"] == []


# ---------------------------------------------------------------------------
# Listing + pagination
# ---------------------------------------------------------------------------


class TestListing:
    def test_pagination_round_trip(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            ids: list[str] = []
            for i in range(3):
                r = client.post(
                    "/api/v1/expenses",
                    json=_create_body(
                        work_engagement_id=seeded["worker_eng_id"],
                        vendor=f"Vendor {i}",
                    ),
                )
                ids.append(r.json()["id"])

            r = client.get("/api/v1/expenses", params={"limit": 1})
            assert r.status_code == 200, r.text
            page1 = r.json()
            assert len(page1["data"]) == 1
            assert page1["has_more"] is True
            assert page1["next_cursor"] is not None

            r = client.get(
                "/api/v1/expenses",
                params={"limit": 1, "cursor": page1["next_cursor"]},
            )
            page2 = r.json()
            assert len(page2["data"]) == 1
            # Pagination is descending by ULID, so ``page2`` must hold an
            # older claim than ``page1``.
            assert page2["data"][0]["id"] != page1["data"][0]["id"]

    def test_cross_user_listing_without_cap_is_403(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.get(
                "/api/v1/expenses",
                params={"user_id": seeded["peer_id"]},
            )
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["error"] == "claim_permission_denied"

    def test_cross_tenant_get_is_404(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # Worker creates a claim in the home workspace.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]

        # Foreign owner cannot see it.
        with _client_for(session_factory, seeded["foreign_ctx"], storage) as client:
            r = client.get(f"/api/v1/expenses/{cid}")
            assert r.status_code == 404, r.text

    def test_mine_true_returns_only_caller_claims(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # Worker files two of their own claims; the peer (a separate
        # worker in the same workspace) files one. ``mine=true`` must
        # surface exactly the worker's two — never the peer's.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            for i in range(2):
                client.post(
                    "/api/v1/expenses",
                    json=_create_body(
                        work_engagement_id=seeded["worker_eng_id"],
                        vendor=f"Worker vendor {i}",
                    ),
                )

        with _client_for(session_factory, seeded["peer_ctx"], storage) as client:
            client.post(
                "/api/v1/expenses",
                json=_create_body(
                    work_engagement_id=seeded["peer_eng_id"],
                    vendor="Peer vendor",
                ),
            )

        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.get("/api/v1/expenses", params={"mine": "true"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["data"]) == 2
            vendors = {row["vendor"] for row in body["data"]}
            assert vendors == {"Worker vendor 0", "Worker vendor 1"}
            # Envelope keys are the standard cd-t6y2 shape.
            assert body["has_more"] is False
            assert body["next_cursor"] is None

    def test_mine_true_empty_returns_envelope_with_data_empty(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # Worker has filed nothing yet — the envelope is well-formed
        # with an empty ``data`` list, NOT a 404 / 204.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.get("/api/v1/expenses", params={"mine": "true"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body == {
                "data": [],
                "next_cursor": None,
                "has_more": False,
            }

    def test_mine_true_worker_authorized(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # A plain worker (no ``expenses.approve`` capability) reads
        # ``mine=true`` successfully — the explicit self-only filter
        # short-circuits the manager-cap branch in
        # :func:`list_expense_claims_route`.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            client.post(
                "/api/v1/expenses",
                json=_create_body(
                    work_engagement_id=seeded["worker_eng_id"],
                    vendor="Solo claim",
                ),
            )
            r = client.get("/api/v1/expenses", params={"mine": "true"})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert len(data) == 1
            assert data[0]["vendor"] == "Solo claim"

    def test_mine_true_filters_other_users_in_same_workspace(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # Owner + manager + peer + worker all file a claim — the
        # worker's ``mine=true`` listing must hold exactly the worker
        # row; cross-engagement claims sharing the same workspace are
        # filtered out.
        for ctx_key, eng_key, vendor in (
            ("owner_ctx", "owner_eng_id", "Owner expense"),
            ("manager_ctx", "manager_eng_id", "Manager expense"),
            ("peer_ctx", "peer_eng_id", "Peer expense"),
            ("worker_ctx", "worker_eng_id", "Worker expense"),
        ):
            with _client_for(session_factory, seeded[ctx_key], storage) as client:
                client.post(
                    "/api/v1/expenses",
                    json=_create_body(
                        work_engagement_id=seeded[eng_key],
                        vendor=vendor,
                    ),
                )

        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.get("/api/v1/expenses", params={"mine": "true"})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert [row["vendor"] for row in data] == ["Worker expense"]

    def test_mine_true_cross_workspace_blocked(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # The worker files a claim in the home workspace. Listing
        # ``mine=true`` from the foreign workspace context (same actor
        # has no engagement there) returns an empty envelope — every
        # claim is workspace-scoped at rest, so cross-tenant probes
        # collapse to "no rows" rather than leaking the home claim.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            client.post(
                "/api/v1/expenses",
                json=_create_body(
                    work_engagement_id=seeded["worker_eng_id"],
                    vendor="Home-only",
                ),
            )

        # The owner's foreign workspace is the cross-tenant probe
        # surface — the seeded fixture only wires ``foreign_ctx`` for
        # the owner. ``mine=true`` there narrows to the owner's own
        # claims in that workspace; nothing was filed, so the envelope
        # is empty and the worker's home-workspace claim is invisible.
        with _client_for(session_factory, seeded["foreign_ctx"], storage) as client:
            r = client.get("/api/v1/expenses", params={"mine": "true"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["data"] == []
            assert body["has_more"] is False

    def test_mine_true_with_explicit_user_id_is_422(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # The two filters answer different questions; combining them
        # is a caller bug we want to surface loudly, not silently
        # privilege one over the other.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.get(
                "/api/v1/expenses",
                params={"mine": "true", "user_id": seeded["worker_id"]},
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "mine_user_id_conflict"


# ---------------------------------------------------------------------------
# Manager flow
# ---------------------------------------------------------------------------


class TestManagerFlow:
    def _seed_submitted(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> str:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]
            client.post(f"/api/v1/expenses/{cid}/submit")
        return str(cid)

    def test_approve_no_edits(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        cid = self._seed_submitted(session_factory, storage, seeded)
        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            r = client.post(f"/api/v1/expenses/{cid}/approve")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "approved"
            assert body["decided_by"] == seeded["manager_id"]

    def test_approve_with_edits(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        cid = self._seed_submitted(session_factory, storage, seeded)
        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            r = client.post(
                f"/api/v1/expenses/{cid}/approve",
                json={"vendor": "Manager-renamed", "total_amount_cents": 999},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "approved"
            assert body["vendor"] == "Manager-renamed"
            assert body["total_amount_cents"] == 999

    def test_approve_on_draft_returns_409(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # Worker creates but never submits.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            cid = r.json()["id"]

        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            r = client.post(f"/api/v1/expenses/{cid}/approve")
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "claim_not_approvable"

    def test_reject_empty_reason_is_422(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        cid = self._seed_submitted(session_factory, storage, seeded)
        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            r = client.post(
                f"/api/v1/expenses/{cid}/reject",
                json={"reason_md": ""},
            )
            # FastAPI's RequestValidationError on the DTO ``min_length=1``
            # surfaces as 422 with the standard ``detail[]`` envelope.
            assert r.status_code == 422, r.text

    def test_reject_happy_path(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        cid = self._seed_submitted(session_factory, storage, seeded)
        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            r = client.post(
                f"/api/v1/expenses/{cid}/reject",
                json={"reason_md": "duplicate of last week's claim"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "rejected"
            assert body["decision_note_md"] == "duplicate of last week's claim"

    def test_reimburse_round_trip(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        cid = self._seed_submitted(session_factory, storage, seeded)
        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            client.post(f"/api/v1/expenses/{cid}/approve")

            r = client.post(
                f"/api/v1/expenses/{cid}/reimburse",
                json={"via": "bank"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "reimbursed"

            # Second reimburse fails — claim is no longer approved.
            r = client.post(
                f"/api/v1/expenses/{cid}/reimburse",
                json={"via": "bank"},
            )
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "claim_not_reimbursable"


# ---------------------------------------------------------------------------
# Pending queue + autofill
# ---------------------------------------------------------------------------


class TestPendingQueue:
    def test_worker_probe_is_403(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.get("/api/v1/expenses/pending")
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["error"] == "approval_permission_denied"

    def test_manager_sees_only_submitted(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        # Worker creates two claims; submits one.
        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            submitted_id = r.json()["id"]
            client.post(f"/api/v1/expenses/{submitted_id}/submit")

            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            draft_id = r.json()["id"]

        with _client_for(session_factory, seeded["manager_ctx"], storage) as client:
            r = client.get("/api/v1/expenses/pending")
            assert r.status_code == 200, r.text
            ids = [row["id"] for row in r.json()["data"]]
            assert submitted_id in ids
            assert draft_id not in ids


class TestAutofillPlaceholder:
    def test_unconfigured_returns_503(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        """cd-95zb: when ``settings.llm_ocr_model`` is unset (the
        default in the test harness), ``POST /expenses/autofill``
        returns 503 ``autofill_not_configured`` so callers can
        distinguish "feature disabled" from "transient error".

        We override :func:`app.api.deps.get_llm` with a do-nothing
        stub so the dep doesn't 503 on the missing client first; the
        new gate then fires on the missing model id.
        """
        from app.api.deps import get_llm

        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            client.app.dependency_overrides[get_llm] = _build_stub_llm()
            # multipart upload — FastAPI's multipart parser requires at
            # least one field even though the route gates on the
            # disabled-feature flag first.
            r = client.post(
                "/api/v1/expenses/autofill",
                files={"image": ("r.jpg", b"x", "image/jpeg")},
            )
            assert r.status_code == 503, r.text
            assert r.json()["detail"]["error"] == "autofill_not_configured"

    def test_happy_path_records_usage_row(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        """A happy preview-path call lands one ``LlmUsage`` row keyed
        on the workspace + ``capability='expenses.autofill'`` so the
        §11 budget envelope sees the spend even though the route
        does not write a claim row.
        """
        from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
        from app.api.deps import get_llm
        from app.config import Settings, get_settings

        settings = Settings(
            database_url="sqlite:///:memory:",
            llm_ocr_model="test/gemma-vision",
        )
        payload = {
            "vendor": "Bistro 42",
            "amount": "27.50",
            "currency": "EUR",
            "purchased_at": "2026-04-17T12:30:00+00:00",
            "category": "food",
            "confidence": {
                "vendor": 0.95,
                "amount": 0.95,
                "currency": 0.95,
                "purchased_at": 0.95,
                "category": 0.95,
            },
        }

        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            client.app.dependency_overrides[get_llm] = _build_stub_llm(
                chat_payload=payload
            )
            client.app.dependency_overrides[get_settings] = lambda: settings
            r = client.post(
                "/api/v1/expenses/autofill",
                files={"image": ("r.jpg", b"\x89PNG\x00bytes", "image/jpeg")},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["vendor"] == "Bistro 42"
            assert body["amount_cents"] == 2750
            assert body["currency"] == "EUR"
            assert body["category"] == "food"

        # One usage row landed under the workspace.
        with session_factory() as s, tenant_agnostic():
            from sqlalchemy import select as sa_select

            rows = list(
                s.scalars(
                    sa_select(LlmUsageRow).where(
                        LlmUsageRow.workspace_id == seeded["workspace_id"]
                    )
                )
            )
            assert len(rows) == 1
            assert rows[0].capability == "expenses.autofill"
            assert rows[0].status == "ok"
            assert rows[0].tokens_in == 42
            assert rows[0].tokens_out == 17

    def test_attach_route_runs_autofill_when_llm_wired(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        """cd-95zb: when ``settings.llm_ocr_model`` is set AND the
        LLM dep returns a usable client, the attach route plumbs
        the runner through to ``attach_receipt`` so the first
        attachment auto-populates the claim's worker-typed fields.
        """
        from app.adapters.db.expenses.models import ExpenseClaim
        from app.api.v1.expenses import _optional_llm
        from app.config import Settings, get_settings

        settings = Settings(
            database_url="sqlite:///:memory:",
            llm_ocr_model="test/gemma-vision",
        )
        payload = {
            "vendor": "Bistro 42",
            "amount": "27.50",
            "currency": "EUR",
            "purchased_at": "2026-04-17T12:30:00+00:00",
            "category": "food",
            "confidence": {
                "vendor": 0.95,
                "amount": 0.95,
                "currency": 0.95,
                "purchased_at": 0.95,
                "category": 0.95,
            },
        }

        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            client.app.dependency_overrides[_optional_llm] = _build_stub_llm(
                chat_payload=payload
            )
            client.app.dependency_overrides[get_settings] = lambda: settings

            # Create claim with manual placeholder values.
            r = client.post(
                "/api/v1/expenses",
                json=_create_body(work_engagement_id=seeded["worker_eng_id"]),
            )
            assert r.status_code == 201, r.text
            claim_id = r.json()["id"]

            # Attach a blob — runner fires inside the same UoW.
            blob = _push_blob(storage)
            r = client.post(
                f"/api/v1/expenses/{claim_id}/attachments",
                json={
                    "blob_hash": blob,
                    "content_type": "image/jpeg",
                    "size_bytes": 16,
                },
            )
            assert r.status_code == 201, r.text

        # Re-read the claim — the runner overwrote the worker-typed
        # fields with the LLM's high-confidence extraction.
        from sqlalchemy import select as sa_select

        with session_factory() as s, tenant_agnostic():
            claim = s.scalars(
                sa_select(ExpenseClaim).where(ExpenseClaim.id == claim_id)
            ).one()
            assert claim.vendor == "Bistro 42"
            assert claim.total_amount_cents == 2750
            assert claim.category == "food"

    def test_rate_limited_emits_retry_after(
        self,
        session_factory: sessionmaker[Session],
        storage: InMemoryStorage,
        seeded: dict[str, Any],
    ) -> None:
        """A 429-equivalent surfaces as 503 ``extraction_rate_limited``
        with a ``Retry-After`` header so the SPA backs off.
        """
        from app.api.deps import get_llm
        from app.config import Settings, get_settings

        settings = Settings(
            database_url="sqlite:///:memory:",
            llm_ocr_model="test/gemma-vision",
        )

        # Define a stub that raises an LlmRateLimited-shaped exception
        # (the autofill module routes by class name, not import).
        class LlmRateLimited(RuntimeError):
            pass

        with _client_for(session_factory, seeded["worker_ctx"], storage) as client:
            client.app.dependency_overrides[get_llm] = _build_stub_llm(
                chat_error=LlmRateLimited("rate limited"),
            )
            client.app.dependency_overrides[get_settings] = lambda: settings
            r = client.post(
                "/api/v1/expenses/autofill",
                files={"image": ("r.jpg", b"x", "image/jpeg")},
            )
            assert r.status_code == 503, r.text
            assert r.json()["detail"]["error"] == "extraction_rate_limited"
            assert r.headers.get("retry-after") == "60"


# ---------------------------------------------------------------------------
# Stub LLM client used by the autofill tests
# ---------------------------------------------------------------------------


def _build_stub_llm(
    *,
    chat_payload: dict[str, Any] | str | None = None,
    chat_error: Exception | None = None,
) -> Any:
    """Return a callable suitable for ``app.dependency_overrides[get_llm]``.

    The returned closure mints a fresh stub per request so per-test
    state doesn't leak.
    """
    import json as _json

    from app.adapters.llm.ports import LLMCapabilityMissing, LLMResponse, LLMUsage

    class _StubLLM:
        def complete(  # pragma: no cover
            self,
            *,
            model_id: str,
            prompt: str,
            max_tokens: int = 1024,
            temperature: float = 0.0,
        ) -> LLMResponse:
            raise NotImplementedError

        def chat(
            self,
            *,
            model_id: str,
            messages: Any,
            max_tokens: int = 1024,
            temperature: float = 0.0,
        ) -> LLMResponse:
            if chat_error is not None:
                raise chat_error
            text = (
                _json.dumps(chat_payload)
                if isinstance(chat_payload, dict)
                else (chat_payload if isinstance(chat_payload, str) else "{}")
            )
            return LLMResponse(
                text=text,
                usage=LLMUsage(
                    prompt_tokens=42,
                    completion_tokens=17,
                    total_tokens=59,
                ),
                model_id=model_id,
                finish_reason="stop",
            )

        def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
            return "Vendor: Stub\nTotal: 27.50 EUR\n2026-04-17"

        def stream_chat(  # pragma: no cover
            self,
            *,
            model_id: str,
            messages: Any,
            max_tokens: int = 1024,
            temperature: float = 0.0,
        ) -> Any:
            raise LLMCapabilityMissing("stream_chat")

    return lambda: _StubLLM()
