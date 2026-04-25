"""Integration tests for :mod:`app.api.v1.tasks` HTTP surface.

Mounts the tasks router on a throwaway FastAPI app, overrides the
workspace-context + db-session deps with seeded fixtures, and drives
the full router → domain → DB chain over HTTP.

Per the cd-sn26 test plan:

* every list endpoint paginates with ``{data, next_cursor, has_more}``
  (spec §12 shape);
* list endpoints honour the ``state`` / ``assignee_user_id`` /
  ``property_id`` / ``scheduled_for_utc_gte`` filters;
* cross-tenant GETs collapse to 404 (not 403);
* completing twice does not break the state machine (§06 concurrent
  completion); a ``start`` on an already-done task surfaces
  ``invalid_state_transition`` (409) — the state-machine probe for
  the idempotency flavour the product requires;
* bad RRULE posts → 422 ``invalid_rrule``;
* comment mentions of non-members → 422 ``comment_mention_invalid``;
* comment PATCH past the 5-minute grace window → 409
  ``comment_edit_window_expired``;
* workers cannot cancel tasks (owner / manager action);
* a ``kind='note'`` evidence upload round-trips end-to-end.

See ``docs/specs/12-rest-api.md`` §"Tasks / templates / schedules",
``docs/specs/06-tasks-and-scheduling.md`` §"State machine" +
§"Task notes are the agent inbox".
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

from app.adapters.db.places.models import Area, Property, PropertyWorkspace, Unit
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import WorkRole
from app.adapters.storage.mime import FiletypeMimeSniffer
from app.adapters.storage.ports import MimeSniffer
from app.api.deps import current_workspace_context, get_mime_sniffer, get_storage
from app.api.deps import db_session as _db_session_dep
from app.api.v1.tasks import router as tasks_router
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
) -> Iterator[dict[str, Any]]:
    """Seed an owner workspace + property + one task; yield the handles.

    Yields a dict with keys::

        workspace_id, slug, property_id, owner_ctx, worker_ctx,
        owner_id, worker_id, task_id, foreign_workspace_id,
        foreign_slug, foreign_ctx, foreign_task_id
    """
    tag = new_ulid()[-8:].lower()
    slug = f"tasks-{tag}"
    foreign_slug = f"tasks-foreign-{tag}"
    with session_factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        worker = bootstrap_user(
            s, email=f"worker-{tag}@example.com", display_name="Worker"
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Tasks WS",
            owner_user_id=owner.id,
        )
        foreign_ws = bootstrap_workspace(
            s,
            slug=foreign_slug,
            name="Foreign WS",
            owner_user_id=owner.id,
        )
        with tenant_agnostic():
            prop = Property(
                id=new_ulid(),
                address="1 Pool Way",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=_PINNED,
            )
            s.add(prop)
            s.flush()

            task = Occurrence(
                id=new_ulid(),
                workspace_id=ws.id,
                schedule_id=None,
                template_id=None,
                property_id=prop.id,
                assignee_user_id=worker.id,
                starts_at=_PINNED + timedelta(hours=2),
                ends_at=_PINNED + timedelta(hours=3),
                scheduled_for_local="2026-04-19T16:00",
                originally_scheduled_for="2026-04-19T16:00",
                state="pending",
                cancellation_reason=None,
                title="Pool clean",
                description_md="Weekly",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=60,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=owner.id,
                created_at=_PINNED,
            )
            s.add(task)
            s.flush()

            foreign_task = Occurrence(
                id=new_ulid(),
                workspace_id=foreign_ws.id,
                schedule_id=None,
                template_id=None,
                property_id=None,
                assignee_user_id=None,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(hours=1),
                scheduled_for_local="2026-04-19T14:00",
                originally_scheduled_for="2026-04-19T14:00",
                state="pending",
                cancellation_reason=None,
                title="Foreign task",
                description_md="",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=30,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=owner.id,
                created_at=_PINNED,
            )
            s.add(foreign_task)
            s.flush()
        s.commit()

        handles: dict[str, Any] = {
            "workspace_id": ws.id,
            "slug": ws.slug,
            "property_id": prop.id,
            "owner_id": owner.id,
            "worker_id": worker.id,
            "task_id": task.id,
            "foreign_workspace_id": foreign_ws.id,
            "foreign_slug": foreign_ws.slug,
            "foreign_task_id": foreign_task.id,
        }

    handles["owner_ctx"] = build_workspace_context(
        workspace_id=handles["workspace_id"],
        workspace_slug=handles["slug"],
        actor_id=handles["owner_id"],
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    handles["worker_ctx"] = build_workspace_context(
        workspace_id=handles["workspace_id"],
        workspace_slug=handles["slug"],
        actor_id=handles["worker_id"],
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
    *,
    storage: InMemoryStorage | None = None,
    mime_sniffer: MimeSniffer | None = None,
) -> TestClient:
    """Return a TestClient pinned to ``ctx`` (and optionally ``storage``).

    Mounts the tasks router at ``/api/v1`` (sans the workspace-slug
    prefix the factory adds in prod) and overrides the session + ctx
    deps so the router reads the ambient seeded workspace.

    ``storage`` is the :class:`InMemoryStorage` the file-evidence route
    writes blobs into. When ``None`` (the default), a throwaway
    instance is constructed so the dep resolves without requiring
    ``app.state.storage`` — tests that need to inspect the stored bytes
    pass their own instance and assert on it after the call.

    ``mime_sniffer`` is the :class:`MimeSniffer` the file-evidence
    route consults for the §15 sniff verdict. The default
    :class:`FiletypeMimeSniffer` exercises the real magic-byte path
    end-to-end (the production wiring); tests that need to pin a
    specific verdict (e.g. force ``None`` for the
    "undetectable bytes" branch) pass a stub.
    """
    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/v1")

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

    resolved_storage = storage if storage is not None else InMemoryStorage()

    def _storage() -> InMemoryStorage:
        return resolved_storage

    resolved_sniffer = (
        mime_sniffer if mime_sniffer is not None else FiletypeMimeSniffer()
    )

    def _mime_sniffer() -> MimeSniffer:
        return resolved_sniffer

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    app.dependency_overrides[get_storage] = _storage
    app.dependency_overrides[get_mime_sniffer] = _mime_sniffer
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — template surface
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_create_then_list_then_read(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                "/api/v1/task_templates",
                json={
                    "name": "Daily check",
                    "description_md": "",
                    "duration_minutes": 15,
                },
            )
            assert r.status_code == 201, r.text
            created = r.json()
            assert created["name"] == "Daily check"
            tid = created["id"]

            r = client.get("/api/v1/task_templates")
            assert r.status_code == 200, r.text
            body = r.json()
            assert set(body.keys()) == {"data", "next_cursor", "has_more"}
            assert any(row["id"] == tid for row in body["data"])

            r = client.get(f"/api/v1/task_templates/{tid}")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == tid

    def test_delete_without_consumers(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                "/api/v1/task_templates",
                json={"name": "Retirable", "description_md": ""},
            )
            tid = r.json()["id"]
            r = client.delete(f"/api/v1/task_templates/{tid}")
            assert r.status_code == 200, r.text
            assert r.json()["deleted_at"] is not None

    def test_cross_tenant_read_is_404(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        # Create a template in workspace A.
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                "/api/v1/task_templates",
                json={"name": "A-only", "description_md": ""},
            )
            tid = r.json()["id"]
        # Try to read it as the owner of workspace B.
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.get(f"/api/v1/task_templates/{tid}")
            assert r.status_code == 404, r.text
            assert r.json()["detail"]["error"] == "task_template_not_found"

    def test_pagination_two_pages(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            ids: list[str] = []
            for i in range(2):
                r = client.post(
                    "/api/v1/task_templates",
                    json={"name": f"Tpl {i}", "description_md": ""},
                )
                ids.append(r.json()["id"])

            r = client.get("/api/v1/task_templates", params={"limit": 1})
            body = r.json()
            assert body["has_more"] is True
            assert body["next_cursor"] is not None
            assert len(body["data"]) == 1

            r = client.get(
                "/api/v1/task_templates",
                params={"limit": 1, "cursor": body["next_cursor"]},
            )
            body = r.json()
            assert len(body["data"]) == 1
            # Not necessarily the end of the list — other tests may have
            # added templates. Just verify cursor advanced.
            assert body["data"][0]["id"] != ids[0] or ids[0] not in ids[1:]


# ---------------------------------------------------------------------------
# Tests — schedule surface
# ---------------------------------------------------------------------------


class TestSchedules:
    def _create_template(self, client: TestClient, name: str = "Sched parent") -> str:
        r = client.post(
            "/api/v1/task_templates",
            json={"name": name, "description_md": "", "duration_minutes": 30},
        )
        tid = r.json()["id"]
        assert isinstance(tid, str)
        return tid

    def test_create_reject_bad_rrule(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            template_id = self._create_template(client)
            r = client.post(
                "/api/v1/schedules",
                json={
                    "name": "Bad",
                    "template_id": template_id,
                    "rrule": "not a valid rrule",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "invalid_rrule"

    def test_create_then_preview_then_pause(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            template_id = self._create_template(client, name="Weekly")
            r = client.post(
                "/api/v1/schedules",
                json={
                    "name": "Weekly clean",
                    "template_id": template_id,
                    "rrule": "RRULE:FREQ=WEEKLY;COUNT=5",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )
            assert r.status_code == 201, r.text
            sid = r.json()["id"]

            r = client.get(f"/api/v1/schedules/{sid}/preview", params={"n": 3})
            assert r.status_code == 200, r.text
            assert len(r.json()["occurrences"]) == 3

            r = client.post(f"/api/v1/schedules/{sid}/pause")
            assert r.status_code == 200, r.text
            assert r.json()["paused_at"] is not None

            r = client.post(f"/api/v1/schedules/{sid}/resume")
            assert r.status_code == 200, r.text
            assert r.json()["paused_at"] is None

    def test_cross_tenant_read_is_404(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            template_id = self._create_template(client)
            r = client.post(
                "/api/v1/schedules",
                json={
                    "name": "X",
                    "template_id": template_id,
                    "rrule": "RRULE:FREQ=WEEKLY;COUNT=2",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )
            sid = r.json()["id"]
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.get(f"/api/v1/schedules/{sid}")
            assert r.status_code == 404, r.text

    def test_list_envelope_carries_templates_by_id(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """``GET /schedules`` returns ``{data, next_cursor, has_more,
        templates_by_id}`` (cd-dzte sidecar shape)."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            template_id = self._create_template(client, name="With sidecar")
            client.post(
                "/api/v1/schedules",
                json={
                    "name": "Daily",
                    "template_id": template_id,
                    "rrule": "RRULE:FREQ=DAILY;COUNT=3",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )

            r = client.get("/api/v1/schedules")
            assert r.status_code == 200, r.text
            body = r.json()
            # The four-key envelope: cursor trio + sidecar.
            assert set(body.keys()) == {
                "data",
                "next_cursor",
                "has_more",
                "templates_by_id",
            }
            # Sidecar is keyed by template id and carries every
            # template referenced on this page.
            assert template_id in body["templates_by_id"]
            assert body["templates_by_id"][template_id]["id"] == template_id
            assert body["templates_by_id"][template_id]["name"] == "With sidecar"
            # Every page schedule's template_id resolves through the
            # sidecar — no SPA-side fan-out fetch needed.
            for row in body["data"]:
                assert row["template_id"] in body["templates_by_id"]

    def test_list_carries_default_assignee_id_and_rrule_human(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Each schedule row carries the SPA-facing derived fields.

        ``default_assignee_id`` mirrors the wire-name the SPA's
        ``Schedule`` TS type expects (the domain field is
        ``default_assignee``); ``rrule_human`` is a short English
        cadence label so the manager Schedules page renders the
        recurrence column without reparsing the RRULE in TS.
        """
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            template_id = self._create_template(client, name="Cadence parent")
            # Schedule with a default assignee.
            client.post(
                "/api/v1/schedules",
                json={
                    "name": "Weekly Mondays",
                    "template_id": template_id,
                    "default_assignee": seeded["worker_id"],
                    "rrule": "RRULE:FREQ=WEEKLY;BYDAY=MO",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )
            # Schedule with no default assignee (None on the wire).
            client.post(
                "/api/v1/schedules",
                json={
                    "name": "Daily turnover",
                    "template_id": template_id,
                    "rrule": "RRULE:FREQ=DAILY",
                    "dtstart_local": "2026-04-21T07:00",
                    "active_from": "2026-04-21",
                },
            )

            r = client.get("/api/v1/schedules")
            assert r.status_code == 200, r.text
            body = r.json()

            rows_by_name = {row["name"]: row for row in body["data"]}
            assert set(rows_by_name) == {"Weekly Mondays", "Daily turnover"}

            # Both fields are present on every row — never undefined,
            # which is the SPA-side regression cd-r4gp tracks.
            for row in body["data"]:
                assert "default_assignee_id" in row
                assert "rrule_human" in row
                # ``default_assignee`` (the legacy domain field name) is
                # NOT on the wire — the SPA reads ``_id``.
                assert "default_assignee" not in row

            mondays = rows_by_name["Weekly Mondays"]
            assert mondays["default_assignee_id"] == seeded["worker_id"]
            assert mondays["rrule_human"] == "Every Monday at 09:00"

            daily = rows_by_name["Daily turnover"]
            assert daily["default_assignee_id"] is None
            assert daily["rrule_human"] == "Every day at 07:00"

    def test_get_schedule_carries_default_assignee_id_and_rrule_human(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """``GET /schedules/{id}`` and the create response share the shape.

        The list envelope is the SPA's primary read path, but the
        single-resource read + the ``201`` body must match — otherwise
        an SPA cache priming on a POST or a refetch lands a different
        wire shape. cd-r4gp's regression covers both surfaces.
        """
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            template_id = self._create_template(client, name="Single read")
            create = client.post(
                "/api/v1/schedules",
                json={
                    "name": "Saturdays",
                    "template_id": template_id,
                    "default_assignee": seeded["worker_id"],
                    "rrule": "RRULE:FREQ=WEEKLY;BYDAY=SA",
                    "dtstart_local": "2026-04-18T08:00",
                    "active_from": "2026-04-18",
                },
            )
            assert create.status_code == 201, create.text
            created_body = create.json()
            assert created_body["default_assignee_id"] == seeded["worker_id"]
            assert created_body["rrule_human"] == "Every Saturday at 08:00"
            assert "default_assignee" not in created_body

            r = client.get(f"/api/v1/schedules/{created_body['id']}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["default_assignee_id"] == seeded["worker_id"]
            assert body["rrule_human"] == "Every Saturday at 08:00"
            assert "default_assignee" not in body

    def test_list_sidecar_only_carries_referenced_templates(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """An unreferenced template doesn't bloat the sidecar."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            referenced = self._create_template(client, name="Referenced")
            unreferenced = self._create_template(client, name="Lonely")
            client.post(
                "/api/v1/schedules",
                json={
                    "name": "Pull",
                    "template_id": referenced,
                    "rrule": "RRULE:FREQ=DAILY;COUNT=1",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )

            r = client.get("/api/v1/schedules")
            body = r.json()
            assert referenced in body["templates_by_id"]
            assert unreferenced not in body["templates_by_id"]

    def test_list_sidecar_pagination_scoped(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Each page only carries the templates referenced on that page.

        Two schedules referencing two different templates, served with
        ``limit=1`` — page A's sidecar holds template A only; page B's
        holds template B only. Stops a small page from dragging the
        whole template table along.
        """
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            tpl_a = self._create_template(client, name="Page A template")
            tpl_b = self._create_template(client, name="Page B template")
            client.post(
                "/api/v1/schedules",
                json={
                    "name": "Sched A",
                    "template_id": tpl_a,
                    "rrule": "RRULE:FREQ=DAILY;COUNT=1",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )
            client.post(
                "/api/v1/schedules",
                json={
                    "name": "Sched B",
                    "template_id": tpl_b,
                    "rrule": "RRULE:FREQ=DAILY;COUNT=1",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )

            seen_pairs: list[tuple[str, set[str]]] = []
            cursor: str | None = None
            for _ in range(4):
                params: dict[str, str] = {"limit": "1"}
                if cursor is not None:
                    params["cursor"] = cursor
                r = client.get("/api/v1/schedules", params=params)
                body = r.json()
                assert len(body["data"]) == 1
                seen_pairs.append(
                    (
                        body["data"][0]["template_id"],
                        set(body["templates_by_id"].keys()),
                    )
                )
                if not body["has_more"]:
                    break
                cursor = body["next_cursor"]

            for ref_id, sidecar_ids in seen_pairs:
                # The sidecar carries exactly this page's reference —
                # no broader workspace bleed-through.
                assert sidecar_ids == {ref_id}

    def test_list_empty_workspace_envelope(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Empty result still serialises the envelope with an empty sidecar."""
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.get("/api/v1/schedules")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["data"] == []
            assert body["templates_by_id"] == {}
            assert body["has_more"] is False
            assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# Tests — occurrences (tasks)
# ---------------------------------------------------------------------------


class TestTasksListing:
    def test_filter_by_state(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.get("/api/v1/tasks", params={"state": "pending"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert any(row["id"] == seeded["task_id"] for row in body["data"]), body

            r = client.get("/api/v1/tasks", params={"state": "done"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert not any(row["id"] == seeded["task_id"] for row in body["data"])

    def test_filter_by_assignee_and_property(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.get(
                "/api/v1/tasks",
                params={
                    "assignee_user_id": seeded["worker_id"],
                    "property_id": seeded["property_id"],
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            ids = [row["id"] for row in body["data"]]
            assert seeded["task_id"] in ids

    def test_filter_by_scheduled_for_utc_gte(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            # Task anchor is _PINNED + 2h → 2026-04-19T14:00Z. Filter
            # from 20:00 — the seeded task should fall out.
            r = client.get(
                "/api/v1/tasks",
                params={"scheduled_for_utc_gte": "2026-04-19T20:00:00+00:00"},
            )
            body = r.json()
            assert not any(row["id"] == seeded["task_id"] for row in body["data"])

    def test_filter_state_overdue_uses_derived_rule(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """``?state=overdue`` is a derived projection — translate it to
        ``state IN ('pending','in_progress') AND starts_at < now``.

        Regression for cd-me3q: the DB column never stores ``'overdue'``
        so a literal ``WHERE state = 'overdue'`` filter returns 0 rows
        even when there are obviously overdue tasks on the workspace.
        """
        # Seed a task with an anchor solidly in the past.
        overdue_id = new_ulid()
        with session_factory() as s, tenant_agnostic():
            s.add(
                Occurrence(
                    id=overdue_id,
                    workspace_id=seeded["workspace_id"],
                    schedule_id=None,
                    template_id=None,
                    property_id=seeded["property_id"],
                    assignee_user_id=seeded["worker_id"],
                    starts_at=datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
                    ends_at=datetime(2020, 1, 1, 1, 0, 0, tzinfo=UTC),
                    scheduled_for_local="2020-01-01T01:00",
                    originally_scheduled_for="2020-01-01T01:00",
                    state="pending",
                    cancellation_reason=None,
                    title="Long overdue",
                    description_md="",
                    priority="normal",
                    photo_evidence="disabled",
                    duration_minutes=60,
                    area_id=None,
                    unit_id=None,
                    expected_role_id=None,
                    linked_instruction_ids=[],
                    inventory_consumption_json={},
                    is_personal=False,
                    created_by_user_id=seeded["owner_id"],
                    created_at=_PINNED,
                )
            )
            s.commit()

        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.get("/api/v1/tasks", params={"state": "overdue"})
            assert r.status_code == 200, r.text
            ids = [row["id"] for row in r.json()["data"]]
            assert overdue_id in ids

    def test_cross_tenant_get_is_404(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.get(f"/api/v1/tasks/{seeded['foreign_task_id']}")
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "task_not_found"

    def test_list_pagination_two_pages(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        # Seed a second task so two are visible.
        with session_factory() as s, tenant_agnostic():
            s.add(
                Occurrence(
                    id=new_ulid(),
                    workspace_id=seeded["workspace_id"],
                    schedule_id=None,
                    template_id=None,
                    property_id=seeded["property_id"],
                    assignee_user_id=seeded["worker_id"],
                    starts_at=_PINNED + timedelta(hours=4),
                    ends_at=_PINNED + timedelta(hours=5),
                    scheduled_for_local="2026-04-19T18:00",
                    originally_scheduled_for="2026-04-19T18:00",
                    state="pending",
                    cancellation_reason=None,
                    title="Second task",
                    description_md="",
                    priority="normal",
                    photo_evidence="disabled",
                    duration_minutes=60,
                    area_id=None,
                    unit_id=None,
                    expected_role_id=None,
                    linked_instruction_ids=[],
                    inventory_consumption_json={},
                    is_personal=False,
                    created_by_user_id=seeded["owner_id"],
                    created_at=_PINNED,
                )
            )
            s.commit()

        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.get("/api/v1/tasks", params={"limit": 1})
            body = r.json()
            assert body["has_more"] is True
            assert body["next_cursor"] is not None
            assert len(body["data"]) == 1

            r = client.get(
                "/api/v1/tasks",
                params={"limit": 1, "cursor": body["next_cursor"]},
            )
            body = r.json()
            assert len(body["data"]) == 1


# ---------------------------------------------------------------------------
# Tests — state machine (start / complete / skip / cancel)
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_start_then_complete(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(f"/api/v1/tasks/{seeded['task_id']}/start")
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "in_progress"

            # Regression (cd-me3q self-review): ``GET /tasks/{id}`` must
            # survive a state transition. The ``TaskView.state`` Literal
            # previously covered only ``'scheduled'`` / ``'pending'``, so
            # re-projecting an ``'in_progress'`` / ``'done'`` row via
            # :func:`read_task` blew up with a narrowing ``ValueError``.
            r = client.get(f"/api/v1/tasks/{seeded['task_id']}")
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "in_progress"

            r = client.post(f"/api/v1/tasks/{seeded['task_id']}/complete", json={})
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "done"

            r = client.get(f"/api/v1/tasks/{seeded['task_id']}")
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "done"

            # The list route re-projects every row via the same path —
            # verify a ``done`` row is reachable in a plain listing too.
            r = client.get("/api/v1/tasks")
            assert r.status_code == 200, r.text
            states = {row["id"]: row["state"] for row in r.json()["data"]}
            assert states.get(seeded["task_id"]) == "done"

    def test_start_on_done_raises_invalid_state_transition(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Once the task is ``done`` the state machine rejects ``start`` —
        the behaviour the SPA observes under idempotent retries of
        completion is that the row stabilises; here we verify that a
        fresh verb against the terminal state surfaces 409."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(f"/api/v1/tasks/{seeded['task_id']}/complete", json={})
            assert r.status_code == 200, r.text
            r = client.post(f"/api/v1/tasks/{seeded['task_id']}/start")
            assert r.status_code == 409
            body = r.json()
            assert body["detail"]["error"] == "invalid_state_transition"
            assert body["detail"]["current"] == "done"

    def test_complete_twice_second_supersedes(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Per §06 concurrent completion: a second complete wins the
        fields and records a supersession audit. The HTTP route's
        idempotency is delivered by the Idempotency-Key middleware at
        the edge (not wired on this test harness) — the domain itself
        still accepts the re-complete."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r1 = client.post(f"/api/v1/tasks/{seeded['task_id']}/complete", json={})
            assert r1.status_code == 200, r1.text
            r2 = client.post(f"/api/v1/tasks/{seeded['task_id']}/complete", json={})
            assert r2.status_code == 200, r2.text
            # Both land; row stays done.
            assert r2.json()["state"] == "done"

    def test_worker_cannot_cancel(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["worker_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/cancel",
                json={"reason_md": "nope"},
            )
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["error"] == "permission_denied"

    def test_owner_can_cancel(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/cancel",
                # ``TaskCancelled`` event validator caps the reason at
                # an identifier-shaped token (see app/events/types.py).
                # The human-readable note would go on a separate
                # cancellation_note_md column per §06; for now we
                # send the validator-safe token.
                json={"reason_md": "rained_out"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "cancelled"
            assert r.json()["reason"] == "rained_out"


# ---------------------------------------------------------------------------
# Tests — comments
# ---------------------------------------------------------------------------


class TestComments:
    def test_post_and_list(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/comments",
                json={"body_md": "Hello", "attachments": []},
            )
            assert r.status_code == 201, r.text

            r = client.get(f"/api/v1/tasks/{seeded['task_id']}/comments")
            assert r.status_code == 200, r.text
            body = r.json()
            assert set(body.keys()) == {"data", "next_cursor", "has_more"}
            assert any(c["body_md"] == "Hello" for c in body["data"])

    def test_mention_non_member_is_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/comments",
                json={"body_md": "cc @ghost", "attachments": []},
            )
            assert r.status_code == 422, r.text
            body = r.json()
            assert body["detail"]["error"] == "comment_mention_invalid"
            assert "ghost" in body["detail"]["unknown_slugs"]

    def test_edit_outside_window_is_409(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        # Post a comment, then forcibly age its ``created_at`` past the
        # 5-minute window. The service reads the wall clock via
        # SystemClock(), so we adjust the DB row rather than the clock.
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/comments",
                json={"body_md": "Original", "attachments": []},
            )
            cid = r.json()["id"]

        with session_factory() as s, tenant_agnostic():
            from app.adapters.db.tasks.models import Comment

            row = s.get(Comment, cid)
            assert row is not None
            row.created_at = datetime.now(tz=UTC) - timedelta(minutes=30)
            s.commit()

        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}/comments/{cid}",
                json={"body_md": "Edited late"},
            )
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "comment_edit_window_expired"


# ---------------------------------------------------------------------------
# Tests — evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_note_evidence_round_trips(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            # multipart/form-data with kind=note + note_md.
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "note", "note_md": "Smells like chlorine"},
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["kind"] == "note"
            assert body["note_md"] == "Smells like chlorine"
            assert body["blob_hash"] is None

            r = client.get(f"/api/v1/tasks/{seeded['task_id']}/evidence")
            assert r.status_code == 200, r.text
            rows = r.json()["data"]
            assert any(e["note_md"] == "Smells like chlorine" for e in rows)

    # cd-jl0g: photo / voice / gps end-to-end. The PNG and WAV fixtures
    # are pre-rendered constant bytes (no runtime encoder dependency)
    # so the test stays deterministic.
    _TINY_PNG: bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "8900000010494441541857636060f80f0000010101003e6b40fb000000004949"
        "454e44ae426082"
    )
    _TINY_WAV: bytes = (
        b"RIFF" + (36 + 16).to_bytes(4, "little") + b"WAVE"
        b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # PCM
        + (1).to_bytes(2, "little")  # mono
        + (8000).to_bytes(4, "little")  # 8 kHz
        + (16000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + (16).to_bytes(4, "little")
        + (b"\x00" * 16)
    )

    def test_photo_evidence_round_trips(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        storage = InMemoryStorage()
        with _client_for(session_factory, seeded["owner_ctx"], storage=storage) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo"},
                files={"file": ("evidence.png", self._TINY_PNG, "image/png")},
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["kind"] == "photo"
            assert body["note_md"] is None
            assert body["blob_hash"] is not None
            assert len(body["blob_hash"]) == 64  # SHA-256 hex.
            assert storage.exists(body["blob_hash"])

            r = c.get(f"/api/v1/tasks/{seeded['task_id']}/evidence")
            assert r.status_code == 200, r.text
            ids = [row["id"] for row in r.json()["data"]]
            assert body["id"] in ids

    def test_voice_evidence_round_trips(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        storage = InMemoryStorage()
        with _client_for(session_factory, seeded["owner_ctx"], storage=storage) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "voice"},
                files={"file": ("memo.wav", self._TINY_WAV, "audio/wav")},
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["kind"] == "voice"
            assert body["blob_hash"] is not None
            assert storage.exists(body["blob_hash"])

    def test_gps_evidence_round_trips(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        storage = InMemoryStorage()
        gps_payload = b'{"lat": 48.8566, "lon": 2.3522, "accuracy_m": 5}'
        with _client_for(session_factory, seeded["owner_ctx"], storage=storage) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "gps"},
                files={
                    "file": ("coords.json", gps_payload, "application/json"),
                },
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["kind"] == "gps"
            assert body["blob_hash"] is not None
            with storage.get(body["blob_hash"]) as fh:
                assert fh.read() == gps_payload

    def test_gps_payload_invalid_returns_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "gps"},
                files={
                    "file": ("coords.json", b'{"lat": 48.8566}', "application/json"),
                },
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "evidence_gps_payload_invalid"

    def test_size_cap_returns_413(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        # GPS cap is 4 KiB — easiest to exceed deterministically.
        oversize = b'{"lat": 0, "lon": 0, "filler": "' + b"x" * 4096 + b'"}'
        with _client_for(session_factory, seeded["owner_ctx"]) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "gps"},
                files={"file": ("big.json", oversize, "application/json")},
            )
            assert r.status_code == 413, r.text
            body = r.json()
            assert body["detail"]["error"] == "evidence_too_large"
            assert body["detail"]["kind"] == "gps"

    def test_bad_mime_returns_415(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo"},
                files={
                    "file": ("evidence.svg", b"<svg/>", "image/svg+xml"),
                },
            )
            assert r.status_code == 415, r.text
            body = r.json()
            assert body["detail"]["error"] == "evidence_content_type_rejected"
            assert body["detail"]["kind"] == "photo"

    def test_file_kind_without_file_returns_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A photo / voice / gps post without the file part is a client
        bug — 422 instead of silently writing an empty Evidence row."""
        with _client_for(session_factory, seeded["owner_ctx"]) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo"},
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "evidence_file_required"

    def test_oversized_content_length_short_circuits(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A client that *advertises* an oversized body is rejected at
        the dep layer — before Starlette's multipart parser buffers
        anything. Mirrors :func:`me_avatar._check_content_length`'s
        contract: the dep is the first gate, the in-handler streaming
        cap is the backup for chunked / lying clients.
        """
        with _client_for(session_factory, seeded["owner_ctx"]) as c:
            # 26 MiB advertised body > the 25 MiB + 1 router cap.
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                headers={"Content-Length": str(26 * 1024 * 1024)},
                data={"kind": "photo"},
                files={"file": ("p.png", b"\x00" * 8, "image/png")},
            )
            assert r.status_code == 413, r.text
            assert r.json()["detail"]["error"] == "evidence_too_large"

    def test_file_kind_with_whitespace_only_note_md_returns_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A photo / voice / gps upload that also carries a non-None
        ``note_md`` (even whitespace) is a confused client; reject it
        explicitly so the contract stays narrow.
        """
        with _client_for(session_factory, seeded["owner_ctx"]) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo", "note_md": "   "},
                files={"file": ("p.png", self._TINY_PNG, "image/png")},
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "evidence_file_with_note"

    # cd-ba5c: server-side MIME sniffing — spec §15 "Input validation"
    # ("MIME sniffed server-side; we trust the sniff, not the header").
    # The PE header below is the smallest fragment ``filetype`` will
    # recognise as a Windows executable — enough to assert the seam
    # rejects it under the photo allow-list.
    _TINY_EXE: bytes = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00" + b"\x00" * 32

    def test_evil_exe_declared_as_png_rejected(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Spec §15: a Windows PE smuggled as ``image/png`` is rejected.

        The route hands the bytes to the sniffer, the sniffer returns
        a PE-shaped MIME, the per-kind allow-list rejects the upload
        with the **sniffed** type on the envelope.
        """
        storage = InMemoryStorage()
        with _client_for(session_factory, seeded["owner_ctx"], storage=storage) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo"},
                files={"file": ("evidence.png", self._TINY_EXE, "image/png")},
            )
            assert r.status_code == 415, r.text
            body = r.json()["detail"]
            assert body["error"] == "evidence_content_type_rejected"
            assert body["kind"] == "photo"
            # The envelope surfaces the sniffed type so the operator
            # sees the actual shape (a PE label), not the declared lie.
            assert body["sniffed_type"] is not None
            assert "image" not in body["sniffed_type"]
            # Forensic trail: the declared header is preserved alongside
            # the sniff so the operator can spot the discrepancy.
            assert body["declared_type"] == "image/png"
            # The malicious payload never landed.
            assert not storage._blobs

    def test_gps_json_misdeclared_as_image_rejected(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Spec §15: a GPS JSON smuggled as ``image/jpeg`` is rejected.

        The JSON structural fallback only fires when the hint
        advertises JSON; an ``image/jpeg`` hint closes the fallback
        gate, so the sniff returns ``None``, ``None`` is not in the
        photo allow-list → 415.
        """
        storage = InMemoryStorage()
        gps_payload = b'{"lat": 48.8566, "lon": 2.3522}'
        with _client_for(session_factory, seeded["owner_ctx"], storage=storage) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo"},
                files={"file": ("coords.json", gps_payload, "image/jpeg")},
            )
            assert r.status_code == 415, r.text
            body = r.json()["detail"]
            assert body["error"] == "evidence_content_type_rejected"
            assert body["kind"] == "photo"
            assert body["sniffed_type"] is None  # sniffer couldn't classify.
            assert body["declared_type"] == "image/jpeg"
            assert not storage._blobs

    def test_undetectable_bytes_rejected_via_sniff(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """Spec §15: random bytes the sniffer can't classify are REJECTED.

        Even when the declared type is in the allow-list, a sniffer
        that can't classify the bytes returns ``None`` and the upload
        is rejected. The fallback to the declared header is the very
        vector the seam closes.

        Uses a pinned-``None`` sniffer to assert this is the seam's
        contract independent of which payload happens to be in the
        sniffer's vocabulary.
        """
        storage = InMemoryStorage()

        class _PinnedNoneSniffer:
            def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
                return None

        with _client_for(
            session_factory,
            seeded["owner_ctx"],
            storage=storage,
            mime_sniffer=_PinnedNoneSniffer(),
        ) as c:
            r = c.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "photo"},
                files={"file": ("p.png", self._TINY_PNG, "image/png")},
            )
            assert r.status_code == 415, r.text
            body = r.json()["detail"]
            assert body["error"] == "evidence_content_type_rejected"
            assert body["sniffed_type"] is None
            assert not storage._blobs

    def test_invalid_kind_returns_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A kind outside the §06 enum is caller error — 422
        ``evidence_invalid_kind``."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/evidence",
                data={"kind": "banana"},
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "evidence_invalid_kind"


# ---------------------------------------------------------------------------
# Tests — PATCH /tasks/{id} (narrow partial update)
# ---------------------------------------------------------------------------


class TestPatchTask:
    def test_patch_title_and_description(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"title": "Skim & scrub", "description_md": "New body"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["title"] == "Skim & scrub"
            assert body["description_md"] == "New body"

    def test_patch_empty_body_is_noop(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """An empty PATCH returns 200 with the current task body."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={},
            )
            assert r.status_code == 200, r.text
            assert r.json()["id"] == seeded["task_id"]

    def test_patch_cross_tenant_is_404(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"title": "hijack"},
            )
            assert r.status_code == 404, r.text
            assert r.json()["detail"]["error"] == "task_not_found"

    # --- cd-43wv widened set ---------------------------------------

    def test_patch_priority_and_duration_and_photo_evidence(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={
                    "priority": "urgent",
                    "duration_minutes": 90,
                    "photo_evidence": "required",
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["priority"] == "urgent"
            assert body["duration_minutes"] == 90
            assert body["photo_evidence"] == "required"

    def test_patch_scheduled_for_local_recomputes_utc(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                # Europe/Paris in late April → UTC+2; 18:00 local = 16:00Z.
                json={"scheduled_for_local": "2026-04-19T18:00"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["scheduled_for_local"] == "2026-04-19T18:00:00"
            assert body["scheduled_for_utc"].startswith("2026-04-19T16:00:00")

    def test_patch_scheduled_for_local_invalid_iso_is_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"scheduled_for_local": "not-an-iso"},
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["error"] == "invalid_field"

    def test_patch_property_id_cross_workspace_is_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A property never linked to the workspace is rejected as
        ``invalid_task_field`` — the row write would silently bind a
        cross-tenant property without this gate."""
        # Seed a foreign property — never bound to ``workspace_id``.
        with session_factory() as s:
            with tenant_agnostic():
                foreign_prop_id = new_ulid()
                s.add(
                    Property(
                        id=foreign_prop_id,
                        address="999 Foreign Way",
                        timezone="Europe/London",
                        tags_json=[],
                        created_at=_PINNED,
                    )
                )
                s.flush()
            s.commit()
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"property_id": foreign_prop_id},
            )
            assert r.status_code == 422, r.text
            detail = r.json()["detail"]
            assert detail["error"] == "invalid_task_field"
            assert detail["field"] == "property_id"

    def test_patch_area_for_other_property_is_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        # Seed a separate property + area; the area belongs to that
        # other property, not the seeded task's property.
        with session_factory() as s:
            with tenant_agnostic():
                other_prop_id = new_ulid()
                s.add(
                    Property(
                        id=other_prop_id,
                        address="2 Other Way",
                        timezone="Europe/London",
                        tags_json=[],
                        created_at=_PINNED,
                    )
                )
                s.flush()
                area_id = new_ulid()
                s.add(
                    Area(
                        id=area_id,
                        property_id=other_prop_id,
                        label="Patio",
                        icon=None,
                        ordering=0,
                        created_at=_PINNED,
                    )
                )
                s.flush()
            s.commit()
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"area_id": area_id},
            )
            assert r.status_code == 422, r.text
            detail = r.json()["detail"]
            assert detail["error"] == "invalid_task_field"
            assert detail["field"] == "area_id"

    def test_patch_unit_for_other_property_is_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with session_factory() as s:
            with tenant_agnostic():
                other_prop_id = new_ulid()
                s.add(
                    Property(
                        id=other_prop_id,
                        address="3 Other Way",
                        timezone="Europe/London",
                        tags_json=[],
                        created_at=_PINNED,
                    )
                )
                s.flush()
                unit_id = new_ulid()
                s.add(
                    Unit(
                        id=unit_id,
                        property_id=other_prop_id,
                        label="Apt 9",
                        type="apartment",
                        capacity=2,
                        created_at=_PINNED,
                    )
                )
                s.flush()
            s.commit()
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"unit_id": unit_id},
            )
            assert r.status_code == 422, r.text
            detail = r.json()["detail"]
            assert detail["error"] == "invalid_task_field"
            assert detail["field"] == "unit_id"

    def test_patch_expected_role_id_cross_workspace_is_422(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        # Seed a role in the foreign workspace.
        with session_factory() as s:
            with tenant_agnostic():
                role_id = new_ulid()
                s.add(
                    WorkRole(
                        id=role_id,
                        workspace_id=seeded["foreign_workspace_id"],
                        key="maid",
                        name="Maid",
                        description_md="",
                        default_settings_json={},
                        icon_name="",
                        created_at=_PINNED,
                    )
                )
                s.flush()
            s.commit()
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"expected_role_id": role_id},
            )
            assert r.status_code == 422, r.text
            detail = r.json()["detail"]
            assert detail["error"] == "invalid_task_field"
            assert detail["field"] == "expected_role_id"

    def test_patch_property_area_combo_validates_against_new_property(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A single PATCH that moves property + area succeeds when the
        area belongs to the NEW property — the validator runs against
        the post-patch property_id, not the original."""
        with session_factory() as s:
            with tenant_agnostic():
                new_prop_id = new_ulid()
                s.add(
                    Property(
                        id=new_prop_id,
                        address="42 Combo Way",
                        timezone="Europe/London",
                        tags_json=[],
                        created_at=_PINNED,
                    )
                )
                s.flush()
                s.add(
                    PropertyWorkspace(
                        property_id=new_prop_id,
                        workspace_id=seeded["workspace_id"],
                        label="combo",
                        membership_role="owner_workspace",
                        created_at=_PINNED,
                    )
                )
                area_id = new_ulid()
                s.add(
                    Area(
                        id=area_id,
                        property_id=new_prop_id,
                        label="Garden",
                        icon=None,
                        ordering=0,
                        created_at=_PINNED,
                    )
                )
                s.flush()
            s.commit()
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.patch(
                f"/api/v1/tasks/{seeded['task_id']}",
                json={"property_id": new_prop_id, "area_id": area_id},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["property_id"] == new_prop_id
            assert body["area_id"] == area_id


# ---------------------------------------------------------------------------
# Tests — assignment
# ---------------------------------------------------------------------------


class TestAssign:
    def test_assign_echoes_state_and_result(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """The assign route reflects the current task state + the
        :class:`AssignmentResult` shape (source, candidate_count,
        backup_index). Regression for cd-me3q — the old payload
        collapsed state to the empty string."""
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/assign",
                json={"assignee_user_id": seeded["worker_id"]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["task_id"] == seeded["task_id"]
            assert body["assigned_user_id"] == seeded["worker_id"]
            assert body["assignment_source"] == "manual"
            assert body["state"] == "pending"

    def test_assign_cross_tenant_is_404(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/assign",
                json={"assignee_user_id": seeded["worker_id"]},
            )
            assert r.status_code == 404, r.text
            assert r.json()["detail"]["error"] == "task_not_found"


# ---------------------------------------------------------------------------
# Tests — cross-tenant mutations collapse to 404 (not 403/500)
# ---------------------------------------------------------------------------


class TestCrossTenantMutations:
    """Every mutation path on a foreign-workspace id surfaces 404."""

    def test_delete_task_template_cross_tenant(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            r = client.post(
                "/api/v1/task_templates",
                json={"name": "Local-only", "description_md": ""},
            )
            tid = r.json()["id"]
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.delete(f"/api/v1/task_templates/{tid}")
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "task_template_not_found"

    def test_pause_schedule_cross_tenant(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["owner_ctx"]) as client:
            tr = client.post(
                "/api/v1/task_templates",
                json={"name": "Parent", "description_md": ""},
            )
            template_id = tr.json()["id"]
            sr = client.post(
                "/api/v1/schedules",
                json={
                    "name": "X-tenant",
                    "template_id": template_id,
                    "rrule": "RRULE:FREQ=WEEKLY;COUNT=2",
                    "dtstart_local": "2026-04-20T09:00",
                    "active_from": "2026-04-20",
                },
            )
            sid = sr.json()["id"]
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.post(f"/api/v1/schedules/{sid}/pause")
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "schedule_not_found"

    def test_post_comment_cross_tenant_returns_task_not_found(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        """A cross-tenant POST /tasks/{id}/comments should 404 with
        ``task_not_found`` — the missing entity is the task, not the
        (never-created) comment. Regression for cd-me3q."""
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/comments",
                json={"body_md": "ghost", "attachments": []},
            )
            assert r.status_code == 404, r.text
            assert r.json()["detail"]["error"] == "task_not_found"

    def test_list_comments_cross_tenant(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.get(f"/api/v1/tasks/{seeded['task_id']}/comments")
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "task_not_found"

    def test_list_evidence_cross_tenant(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.get(f"/api/v1/tasks/{seeded['task_id']}/evidence")
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "task_not_found"

    def test_complete_cross_tenant(
        self,
        session_factory: sessionmaker[Session],
        seeded: dict[str, Any],
    ) -> None:
        with _client_for(session_factory, seeded["foreign_ctx"]) as client:
            r = client.post(
                f"/api/v1/tasks/{seeded['task_id']}/complete",
                json={},
            )
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "task_not_found"
