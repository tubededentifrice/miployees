"""Unit tests for :mod:`app.api.admin.workspaces`.

Covers the four workspace-lifecycle routes spec §12 "Admin
surface" pins:

* ``GET /workspaces`` — list every workspace + interim verification
  state + members count.
* ``GET /workspaces/{id}`` — summary card with rolling-30d LLM
  aggregates.
* ``POST /workspaces/{id}/trust`` — promote
  ``verification_state``; idempotent re-call; 404 for missing.
* ``POST /workspaces/{id}/archive`` — owners-only soft-archive
  (404 today because cd-zkr deferred); idempotent.

Auth gating relies on the cd-yj4k dep tested in
:mod:`tests.unit.api.admin.test_deps`; this module focuses on
the per-route response shape and side-effect contracts.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.api.admin._workspace_state import (
    ARCHIVED_AT_KEY,
    VERIFICATION_STATE_KEY,
)
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    grant_deployment_admin,
    issue_session,
    seed_user,
    seed_workspace,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("workspaces")


@pytest.fixture
def engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from build_client(settings, session_factory, monkeypatch)


def _admin_cookie(
    session_factory: sessionmaker[Session], settings: Settings
) -> tuple[str, str]:
    """Seed a deployment admin + return ``(user_id, cookie_value)``."""
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada Lovelace")
        grant_deployment_admin(s, user_id=user_id)
        s.commit()
    return user_id, issue_session(session_factory, user_id=user_id, settings=settings)


def _add_llm_usage(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    capability: str,
    cost_cents: int,
    created_at: datetime,
) -> None:
    with session_factory() as s, tenant_agnostic():
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability=capability,
                model_id="01HW00000000000000000MD01",
                tokens_in=1000,
                tokens_out=500,
                cost_cents=cost_cents,
                latency_ms=100,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                created_at=created_at,
            )
        )
        s.commit()


def _add_property_workspace(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    label: str,
) -> None:
    property_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            Property(
                id=property_id,
                name=label,
                kind="residence",
                address=f"{label} address",
                address_json={},
                country="US",
                timezone="Etc/UTC",
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="",
                created_at=PINNED,
            )
        )
        s.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace_id,
                label=label,
                membership_role="owner_workspace",
                status="active",
                created_at=PINNED,
            )
        )
        s.commit()


class TestListWorkspaces:
    """``GET /admin/api/v1/workspaces``."""

    def test_returns_every_workspace_oldest_first(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws_old = seed_workspace(
                s,
                slug="alpha",
                quota_json={"llm_budget_cents_30d": 500},
                created_at=PINNED,
            )
            ws_new = seed_workspace(
                s, slug="beta", created_at=PINNED + timedelta(hours=1)
            )
            s.commit()
        _add_property_workspace(
            session_factory, workspace_id=ws_old, label="North House"
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws_old,
            capability="chat.manager",
            cost_cents=123,
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.get("/admin/api/v1/workspaces")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = [row["id"] for row in body["workspaces"]]
        assert ids == [ws_old, ws_new]
        # Default verification state surfaces when no key written.
        assert body["workspaces"][0]["verification_state"] == "unverified"
        assert body["workspaces"][0]["properties_count"] == 1
        assert body["workspaces"][0]["members_count"] == 0
        assert body["workspaces"][0]["spent_cents_30d"] == 123
        assert body["workspaces"][0]["cap_cents_30d"] == 500
        assert body["workspaces"][0]["archived_at"] is None
        assert body["workspaces"][0]["created_at"].endswith("+00:00")

    def test_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            stranger = seed_user(s, email="x@example.com", display_name="X")
            s.commit()
        cookie = issue_session(session_factory, user_id=stranger, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/admin/api/v1/workspaces")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


class TestGetWorkspace:
    """``GET /admin/api/v1/workspaces/{id}``."""

    def test_returns_summary_with_aggregates(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="ada-ws", quota_json={"users_max": 5})
            s.commit()
        # Two LLM calls inside the rolling window.
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=120,
            created_at=now - timedelta(days=1),
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=80,
            created_at=now - timedelta(hours=2),
        )
        # One LLM call from outside the window — must NOT appear in the sum.
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=999,
            created_at=now - timedelta(days=45),
        )
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.get(f"/admin/api/v1/workspaces/{ws}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == ws
        assert body["slug"] == "ada-ws"
        assert body["llm_calls_30d"] == 2
        assert body["llm_spend_cents_30d"] == 200
        assert body["verification_state"] == "unverified"
        assert body["archived_at"] is None

    def test_404_for_unknown_workspace(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/admin/api/v1/workspaces/01HBOGUS00000000000000000")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


class TestTrustWorkspace:
    """``POST /admin/api/v1/workspaces/{id}/trust``."""

    def test_first_call_promotes_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="trusted-ws")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.post(f"/admin/api/v1/workspaces/{ws}/trust")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"id": ws, "verification_state": "trusted"}

        with session_factory() as s, tenant_agnostic():
            row = s.get(Workspace, ws)
            assert row is not None
            assert row.settings_json[VERIFICATION_STATE_KEY] == "trusted"
            audit_rows = s.scalars(
                select(AuditLog)
                .where(AuditLog.scope_kind == "deployment")
                .where(AuditLog.entity_id == ws)
                .where(AuditLog.action == "workspace.trusted")
            ).all()
            assert len(audit_rows) == 1
            assert audit_rows[0].diff == {
                "verification_state": {"before": "unverified", "after": "trusted"}
            }

    def test_idempotent_re_trust_no_extra_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="re-trust-ws")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        first = client.post(f"/admin/api/v1/workspaces/{ws}/trust")
        assert first.status_code == 200
        second = client.post(f"/admin/api/v1/workspaces/{ws}/trust")
        assert second.status_code == 200
        assert second.json() == {"id": ws, "verification_state": "trusted"}

        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog)
                .where(AuditLog.entity_id == ws)
                .where(AuditLog.action == "workspace.trusted")
            ).all()
            assert len(audits) == 1

    def test_404_for_unknown_workspace(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post("/admin/api/v1/workspaces/01HBOGUS00000000000000000/trust")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


class TestArchiveWorkspace:
    """``POST /admin/api/v1/workspaces/{id}/archive``.

    Owners-only — cd-zkr deferred → every caller 404s today.
    """

    def test_404_for_admin_who_is_not_owner(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="ws-archive")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.post(f"/admin/api/v1/workspaces/{ws}/archive")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

        # Defence in depth: row stays untouched.
        with session_factory() as s, tenant_agnostic():
            row = s.get(Workspace, ws)
            assert row is not None
            assert ARCHIVED_AT_KEY not in row.settings_json

    def test_404_for_non_admin_without_workspace_id_leak(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        # No admin grant — the auth dep itself 404s before the
        # owner gate runs. The check matters because spec §12 says
        # the surface must not advertise its own existence.
        with session_factory() as s:
            stranger = seed_user(s, email="other@example.com", display_name="Other")
            s.commit()
        cookie = issue_session(session_factory, user_id=stranger, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.post("/admin/api/v1/workspaces/01HBOGUS00000000000000000/archive")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"
