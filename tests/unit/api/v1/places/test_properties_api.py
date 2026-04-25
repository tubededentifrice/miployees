"""HTTP-level tests for ``/properties`` (cd-lzh1, cd-yjw5).

Exercises the workspace properties roster endpoint:

* manager / owner can list the roster (200, valid shape) with the
  full governance projection;
* worker is no longer 403 (cd-yjw5) — they get a narrowed projection
  scoped to the properties their ``role_grant`` rows visit, with
  ``client_org_id`` / ``owner_user_id`` / ``settings_override``
  masked to safe defaults;
* cross-workspace bleed-through is impossible;
* the projection joins :class:`Property`, :class:`PropertyWorkspace`,
  and :class:`Area` correctly (areas nested per property,
  soft-deleted rows excluded);
* the response is a bare ``Property[]`` array (no envelope) and
  honours every field declared in
  ``app/web/src/types/property.ts``;
* the OpenAPI document carries both ``places`` and ``properties``
  tags + the canonical ``properties.list`` operation id.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.api.v1.places import _COLOR_PALETTE, _color_for, build_properties_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_properties_router())], factory, ctx)


def _seed_property(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    name: str = "Villa Test",
    city: str = "Antibes",
    timezone: str = "Europe/Paris",
    kind: str = "vacation",
    country: str = "FR",
    locale: str | None = "fr-FR",
    address: str = "1 Test Street",
    client_org_id: str | None = None,
    owner_user_id: str | None = None,
    deleted: bool = False,
    created_at: datetime = _PINNED,
) -> str:
    """Insert a :class:`Property` row + the workspace junction.

    Mirrors the test seed used by the employees API tests but exposes
    every field the SPA's :class:`Property` projects so each assertion
    can pin its own fixture without churning a shared row.
    """
    address_json: dict[str, Any] = {
        "line1": address,
        "line2": None,
        "city": city,
        "state_province": None,
        "postal_code": None,
        "country": country,
    }
    with factory() as s:
        prop = Property(
            id=new_ulid(),
            name=name,
            kind=kind,
            address=address,
            address_json=address_json,
            country=country,
            locale=locale,
            default_currency=None,
            timezone=timezone,
            lat=None,
            lon=None,
            client_org_id=client_org_id,
            owner_user_id=owner_user_id,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=created_at,
            updated_at=created_at,
            deleted_at=_PINNED if deleted else None,
        )
        s.add(prop)
        s.flush()
        s.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace_id,
                label=name,
                membership_role="owner_workspace",
                created_at=_PINNED,
            )
        )
        s.commit()
        return prop.id


def _seed_area(
    factory: sessionmaker[Session],
    *,
    property_id: str,
    label: str,
    ordering: int = 0,
    icon: str | None = None,
) -> str:
    with factory() as s:
        row = Area(
            id=new_ulid(),
            property_id=property_id,
            label=label,
            icon=icon,
            ordering=ordering,
            created_at=_PINNED,
        )
        s.add(row)
        s.commit()
        return row.id


# ---------------------------------------------------------------------------
# AuthZ
# ---------------------------------------------------------------------------


class TestAuthZ:
    def test_owner_can_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner / manager surface holds the gate by default-allow."""
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["name"] == "Villa Sud"

    def test_manager_can_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A pure manager (no owners-group membership) passes the gate.

        ``properties.read``'s ``default_allow`` covers both ``owners``
        and ``managers``. To exercise the manager branch we seed a
        fresh user with ONLY a ``RoleGrant.grant_role='manager'`` row
        — no owners-group membership — so the resolver must fall
        through to the derived ``managers`` group check
        (:func:`app.authz.membership.is_member_of`). A regression
        that drops ``managers`` from ``default_allow`` would surface
        as a 403 here.
        """
        from app.adapters.db.authz.models import RoleGrant
        from tests.factories.identity import build_workspace_context

        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            mgr_user = bootstrap_user(
                s, email="mgr@example.com", display_name="Manager"
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=mgr_user.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            mgr_user_id = mgr_user.id
        manager_ctx = build_workspace_context(
            workspace_id=ctx.workspace_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=mgr_user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
        )
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(manager_ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text

    def test_worker_can_list_scoped_properties(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """cd-yjw5 — workers no longer 403; they list scope.view properties.

        The ``worker_ctx`` fixture seeds a workspace-wide
        ``RoleGrant(grant_role='worker', scope_property_id=None)`` so
        the worker has scope.view on every live property in the
        workspace by the §05 default fan-out. The endpoint must return
        200 with the workspace's properties — not the pre-cd-yjw5 403.
        """
        ctx, factory, ws_id, _ = worker_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert {row["name"] for row in body} == {"Villa Sud"}


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_cross_workspace_blocked(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property linked only to workspace B never bleeds into A's roster."""
        ctx, factory, ws_a_id = owner_ctx
        # Sibling workspace + owner.
        with factory() as s:
            sibling_owner = bootstrap_user(
                s, email="other@example.com", display_name="Other Owner"
            )
            ws_b = bootstrap_workspace(
                s,
                slug="ws-sibling",
                name="Sibling WS",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            ws_b_id = ws_b.id
        _seed_property(factory, workspace_id=ws_a_id, name="Villa A")
        sibling_id = _seed_property(factory, workspace_id=ws_b_id, name="Villa B")

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        ids = {row["id"] for row in body}
        assert sibling_id not in ids
        # Sanity: the workspace-A property is still listed.
        assert {row["name"] for row in body} == {"Villa A"}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestShape:
    def test_returns_bare_array_not_envelope(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Critical contract: SPA expects ``Property[]``, not ``{data, ...}``.

        cd-lzh1 records the bare-array decision (matching the cd-g6nf
        ``/employees`` precedent); a future refactor that introduces
        the standard ``{data, next_cursor, has_more}`` envelope MUST
        also migrate every ``fetchJson<Property[]>`` call site in the
        SPA in lockstep. This assertion is the sentinel that catches a
        one-sided change.
        """
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        body = client.get("/properties").json()
        assert isinstance(body, list), (
            "GET /properties must return a JSON array — see cd-lzh1"
        )
        for row in body:
            assert isinstance(row, dict)
            assert "data" not in row, "envelope sentinel leaked into row"

    def test_shape_matches_spa_type(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Every key in app/web/src/types/property.ts must round-trip."""
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        body = client.get("/properties").json()
        row = body[0]
        # SPA-required field set — keep this assertion in lockstep with
        # ``app/web/src/types/property.ts``. A mismatch surfaces as a
        # TypeError in the SPA before render, so the contract is cheap
        # to enforce here.
        expected = {
            "id",
            "name",
            "city",
            "timezone",
            "color",
            "kind",
            "areas",
            "evidence_policy",
            "country",
            "locale",
            "settings_override",
            "client_org_id",
            "owner_user_id",
        }
        assert set(row.keys()) == expected
        # Spot-check the projected types so a regression that flips a
        # string to an int (or drops a default to ``null``) trips a
        # clean assertion failure rather than confusing the SPA.
        assert isinstance(row["id"], str)
        assert isinstance(row["name"], str)
        assert isinstance(row["city"], str)
        assert isinstance(row["timezone"], str)
        assert row["color"] in {"moss", "sky", "rust"}
        assert row["kind"] in {"str", "vacation", "residence", "mixed"}
        assert isinstance(row["areas"], list)
        assert row["evidence_policy"] in {"inherit", "require", "optional", "forbid"}
        assert isinstance(row["country"], str)
        assert isinstance(row["locale"], str)
        assert isinstance(row["settings_override"], dict)
        # ``client_org_id`` / ``owner_user_id`` are nullable strings —
        # the seed sets them to ``None`` so the wire shape is JSON null.
        assert row["client_org_id"] is None
        assert row["owner_user_id"] is None


# ---------------------------------------------------------------------------
# Areas join
# ---------------------------------------------------------------------------


class TestAreas:
    def test_areas_nested_per_property(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Each property carries its own areas, ordered by ``ordering``.

        Two properties with overlapping area labels must NOT cross-
        pollinate — area rows are scoped by ``property_id``. Within a
        property the SPA's ``Property.areas`` is rendered in the
        seeded ``ordering`` order; ties break alphabetically by label.
        """
        ctx, factory, ws_id = owner_ctx
        # Property A — three areas with explicit ordering.
        prop_a = _seed_property(factory, workspace_id=ws_id, name="Villa A")
        _seed_area(factory, property_id=prop_a, label="Kitchen", ordering=2)
        _seed_area(factory, property_id=prop_a, label="Pool", ordering=1)
        _seed_area(factory, property_id=prop_a, label="Garden", ordering=3)
        # Property B — disjoint set, same workspace.
        prop_b = _seed_property(factory, workspace_id=ws_id, name="Villa B")
        _seed_area(factory, property_id=prop_b, label="Studio", ordering=0)

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        rows = {row["id"]: row for row in body}
        # Ordering ascending, alphabetical tiebreak: Pool(1) → Kitchen(2) → Garden(3).
        assert rows[prop_a]["areas"] == ["Pool", "Kitchen", "Garden"]
        assert rows[prop_b]["areas"] == ["Studio"]

    def test_property_with_no_areas_returns_empty_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property without any :class:`Area` rows projects ``areas=[]``.

        The SPA's ``Property.areas`` is typed as ``string[]`` (not
        ``string[] | null``) so a missing list MUST surface as an
        empty array, not ``null``.
        """
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Bare")
        client = _client(ctx, factory)
        row = client.get("/properties").json()[0]
        assert row["areas"] == []


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------


class TestSoftDelete:
    def test_soft_deleted_property_excluded(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property with ``deleted_at IS NOT NULL`` never surfaces.

        Soft-delete is the cd-8u5 retire flow; the read-side guard
        keeps the roster clean of retired rows even if the junction
        row survives.
        """
        ctx, factory, ws_id = owner_ctx
        live = _seed_property(factory, workspace_id=ws_id, name="Live Villa")
        # Seed a second property and soft-delete it directly.
        gone = _seed_property(factory, workspace_id=ws_id, name="Retired Villa")
        with factory() as s, tenant_agnostic():
            row = s.get(Property, gone)
            assert row is not None
            row.deleted_at = _PINNED
            s.commit()

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        ids = {r["id"] for r in body}
        assert live in ids
        assert gone not in ids


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


class TestOpenAPI:
    def test_openapi_carries_tags(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """The list operation tags as both ``places`` and ``properties``.

        ``places`` clusters the operation under the §01 places context;
        ``properties`` keeps the per-resource tag the SPA codegen
        groups operations by. The operation id is the canonical
        ``properties.list`` per spec §12 "OpenAPI".
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        schema = client.get("/openapi.json").json()
        list_op = schema["paths"]["/properties"]["get"]
        assert "places" in list_op["tags"]
        assert "properties" in list_op["tags"]
        assert list_op["operationId"] == "properties.list"


# ---------------------------------------------------------------------------
# Empty roster
# ---------------------------------------------------------------------------


class TestEmptyRoster:
    def test_no_properties_returns_empty_array(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """The flat-array contract requires a true ``[]`` when nothing matches.

        :func:`bootstrap_workspace` does NOT seed any property rows, so
        the baseline owner ctx already exercises this branch — the
        assertion pins it explicitly so a regression that returns
        ``{}`` or ``{"data": []}`` is caught loudly.
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Color stability
# ---------------------------------------------------------------------------


class TestColorStability:
    """The accent color must be stable across reloads + process restarts.

    cd-lzh1 picks the SPA-facing :data:`PropertyColor` from a
    deterministic SHA-256 of the property id (built-in :func:`hash`
    would shuffle across restarts because it is salted per-process).
    A regression that swaps SHA-256 for :func:`hash`, or that mutates
    the palette ordering, would surface as a flaky color in the
    manager UI — pinning two assertions catches both classes of
    breakage.
    """

    def test_same_id_yields_same_color_across_calls(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Two requests against the same row return the same color."""
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Stable Villa")
        client = _client(ctx, factory)
        first = client.get("/properties").json()[0]["color"]
        second = client.get("/properties").json()[0]["color"]
        assert first == second

    def test_color_matches_sha256_palette_index(self) -> None:
        """The mapping is ``palette[sha256(id)[0] % len(palette)]``.

        Pinning the recipe — not just the determinism — protects the
        contract: a future "let's just use ``hash``" optimisation
        would silently shift every property's accent on restart and
        break the manager's spatial memory. A direct hash recompute
        against the documented palette catches the swap.
        """
        for raw_id in (
            "01HZ8K2X9C3M7P5R6T8W0Y1Z2A",  # ULID-shaped sample
            "abc",
            "",
            "🦊",  # non-ASCII still hashes fine
        ):
            digest = hashlib.sha256(raw_id.encode("utf-8")).digest()
            assert _color_for(raw_id) == _COLOR_PALETTE[digest[0] % len(_COLOR_PALETTE)]


# ---------------------------------------------------------------------------
# Per-role projection (cd-yjw5)
# ---------------------------------------------------------------------------


def _seed_property_pinned_worker(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    workspace_slug: str,
    email: str,
    display_name: str,
    scope_property_ids: list[str],
) -> WorkspaceContext:
    """Seed a worker user with property-pinned ``role_grant`` rows only.

    Returns a :class:`WorkspaceContext` impersonating that worker.
    No workspace-wide (``scope_property_id IS NULL``) grant is created
    — this is the property-narrowed worker surface (cd-yjw5) where
    ``scope.view`` only resolves on the explicitly-listed properties.
    """
    from app.adapters.db.authz.models import RoleGrant
    from tests.factories.identity import build_workspace_context

    with factory() as s:
        worker = bootstrap_user(s, email=email, display_name=display_name)
        for pid in scope_property_ids:
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=pid,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
        s.commit()
        worker_id = worker.id
    return build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=worker_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )


class TestWorkerProjection:
    """Worker-scoped projection — see ``places.py`` per-role split (cd-yjw5)."""

    def test_worker_only_sees_scoped_properties(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A property the worker has no role_grant on stays invisible.

        Replaces the workspace-wide worker grant from ``worker_ctx``
        with a single property-pinned grant; only that property may
        surface. A second seeded property in the same workspace must
        NOT appear in the response, even though it lives on the
        ``property_workspace`` junction the manager view would see.
        """
        from sqlalchemy import delete

        from app.adapters.db.authz.models import RoleGrant

        ctx, factory, ws_id, worker_id = worker_ctx
        # Two properties — pin the worker to one of them by removing
        # the workspace-wide grant the fixture seeded and inserting a
        # single property-pinned grant instead.
        visible = _seed_property(factory, workspace_id=ws_id, name="Worker Villa")
        hidden = _seed_property(factory, workspace_id=ws_id, name="Manager-Only Villa")

        with factory() as s, tenant_agnostic():
            s.execute(
                delete(RoleGrant).where(
                    RoleGrant.workspace_id == ws_id,
                    RoleGrant.user_id == worker_id,
                )
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker_id,
                    grant_role="worker",
                    scope_property_id=visible,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        ids = {row["id"] for row in body}
        assert visible in ids
        assert hidden not in ids
        # Sanity: only the one property surfaces; no other rows leak.
        assert len(body) == 1

    def test_worker_with_no_grants_sees_empty_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A worker with zero ``role_grant`` rows lists nothing — silently.

        cd-yjw5 deliberately drops the ``scope.view`` action gate (a
        property-pinned-only worker is intentionally NOT in
        ``all_workers@workspace`` per :func:`is_member_of`, so a gate
        would 403 the very actor the narrowing branch was written
        for). The privacy contract is enforced inside the handler:
        the visible-property fan-out walks the actor's
        :class:`RoleGrant` rows, and a user with zero matching rows
        ends up with an empty visibility set → empty response. Pin
        the empty-array contract here so a future regression that
        widens the worker view (e.g. fans out across every workspace
        property when no grants are found) trips loudly.
        """
        ctx, factory, ws_id = owner_ctx
        # Build a worker user with NO ``role_grant`` rows at all.
        worker_ctx_local = _seed_property_pinned_worker(
            factory,
            workspace_id=ctx.workspace_id,
            workspace_slug=ctx.workspace_slug,
            email="empty-worker@example.com",
            display_name="Empty Worker",
            scope_property_ids=[],
        )
        # Seed a property the worker has NO grant on — proves the
        # narrowing fires (returns []), rather than the early "no
        # rows in the workspace" branch returning the same shape by
        # accident.
        _seed_property(factory, workspace_id=ws_id, name="Hidden Villa")

        client = _client(worker_ctx_local, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_worker_projection_omits_governance_fields(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """The three §22 / §05 governance fields mask to safe defaults.

        cd-yjw5 masks ``client_org_id`` / ``owner_user_id`` to ``None``
        and ``settings_override`` to ``{}`` for workers regardless of
        the row's underlying value. The seed sets non-NULL governance
        values; the worker projection must still read NULL/empty.
        """
        ctx, factory, ws_id, _ = worker_ctx
        _seed_property(
            factory,
            workspace_id=ws_id,
            name="Governance Villa",
            client_org_id="01HZGOVRORGCLIENTABCDEFGHJ",
            owner_user_id="01HZGOVROWNERUSERABCDEFGHJ",
        )
        client = _client(ctx, factory)
        body = client.get("/properties").json()
        assert len(body) == 1
        row = body[0]
        assert row["client_org_id"] is None
        assert row["owner_user_id"] is None
        assert row["settings_override"] == {}

    def test_worker_projection_keeps_non_governance_fields(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Worker projection still carries every non-governance field.

        The whole point of cd-yjw5 is to give worker pages enough to
        render the property name + city + timezone. Pin every other
        SPA field so a regression that masks too aggressively (e.g.
        nulls out ``city``) trips loudly here.
        """
        ctx, factory, ws_id, _ = worker_ctx
        _seed_property(
            factory,
            workspace_id=ws_id,
            name="Worker Villa",
            city="Antibes",
            timezone="Europe/Paris",
            kind="vacation",
            country="FR",
            locale="fr-FR",
        )
        client = _client(ctx, factory)
        row = client.get("/properties").json()[0]
        assert row["name"] == "Worker Villa"
        assert row["city"] == "Antibes"
        assert row["timezone"] == "Europe/Paris"
        assert row["kind"] == "vacation"
        assert row["country"] == "FR"
        assert row["locale"] == "fr-FR"
        # Stable shape — every key the SPA expects must round-trip
        # even on the masked branch.
        assert set(row.keys()) == {
            "id",
            "name",
            "city",
            "timezone",
            "color",
            "kind",
            "areas",
            "evidence_policy",
            "country",
            "locale",
            "settings_override",
            "client_org_id",
            "owner_user_id",
        }

    def test_worker_property_pinned_grant_narrows_set(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A worker with two property-pinned grants sees exactly those two.

        Three seeded properties; the worker carries two property-
        pinned grants and no workspace-wide grant. The roster must
        return precisely the two pinned ids, not the third — and not
        the full list (which would mean the workspace-wide fan-out
        branch fired by mistake).
        """
        ctx, factory, ws_id = owner_ctx
        prop_a = _seed_property(factory, workspace_id=ws_id, name="Villa A")
        prop_b = _seed_property(factory, workspace_id=ws_id, name="Villa B")
        prop_c = _seed_property(factory, workspace_id=ws_id, name="Villa C")
        worker_ctx_local = _seed_property_pinned_worker(
            factory,
            workspace_id=ctx.workspace_id,
            workspace_slug=ctx.workspace_slug,
            email="pinned-worker@example.com",
            display_name="Pinned Worker",
            scope_property_ids=[prop_a, prop_b],
        )
        client = _client(worker_ctx_local, factory)
        body = client.get("/properties").json()
        ids = {row["id"] for row in body}
        assert ids == {prop_a, prop_b}
        assert prop_c not in ids


class TestManagerProjectionUnchanged:
    """Manager / owner projection still carries the full governance set.

    cd-yjw5 only narrows the worker branch; the existing manager
    contract stays exactly as cd-lzh1 shipped it — every field present
    and unmasked, including ``client_org_id`` / ``owner_user_id`` /
    ``settings_override``.
    """

    def test_manager_projection_includes_governance_fields(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Manager (non-owner) gets the real governance values, not masks.

        Build a pure-manager ctx (no owners-group membership) so the
        projection branch is decided by ``properties.read``'s
        ``managers`` default-allow rather than the owners short-
        circuit. The seeded governance values must round-trip
        verbatim — masking them here would be a regression that
        broke the manager pages' §22 widgets.
        """
        from app.adapters.db.authz.models import RoleGrant
        from tests.factories.identity import build_workspace_context

        ctx, factory, ws_id = owner_ctx
        client_org = "01HZGOVRORGMANAGERROUNDTRIP"
        owner_user = "01HZGOVROWNERMANAGERTRIPABC"
        _seed_property(
            factory,
            workspace_id=ws_id,
            name="Governance Villa",
            client_org_id=client_org,
            owner_user_id=owner_user,
        )
        with factory() as s:
            mgr = bootstrap_user(s, email="mgr-gov@example.com", display_name="MgrGov")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=mgr.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            mgr_id = mgr.id
        manager_ctx = build_workspace_context(
            workspace_id=ctx.workspace_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=mgr_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(manager_ctx, factory)
        row = client.get("/properties").json()[0]
        assert row["client_org_id"] == client_org
        assert row["owner_user_id"] == owner_user
        # ``settings_override`` is currently a static ``{}`` placeholder
        # for both branches (the per-property column hasn't landed); the
        # real assertion is "not masked away to a placeholder when the
        # column lands". Pin the v1 default explicitly.
        assert row["settings_override"] == {}

    def test_owner_projection_includes_governance_fields(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner gets the real governance values too.

        Owners short-circuit through the resolver's owners-group
        check, so this test catches a regression that flipped
        ``mask_governance`` for owners specifically (e.g. by
        accidentally inverting the boolean).
        """
        ctx, factory, ws_id = owner_ctx
        client_org = "01HZGOVRORGOWNERROUNDTRIPXY"
        owner_user = "01HZGOVROWNEROWNERTRIPABCDE"
        _seed_property(
            factory,
            workspace_id=ws_id,
            name="Owner Governance Villa",
            client_org_id=client_org,
            owner_user_id=owner_user,
        )
        client = _client(ctx, factory)
        row = client.get("/properties").json()[0]
        assert row["client_org_id"] == client_org
        assert row["owner_user_id"] == owner_user

    def test_manager_sees_all_workspace_properties(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Manager projection is NOT narrowed by ``role_grant``.

        cd-yjw5 only narrows the worker branch. A manager with no
        property-pinned grants must still see every workspace
        property — the narrowing must not leak across role boundaries.
        """
        from app.adapters.db.authz.models import RoleGrant
        from tests.factories.identity import build_workspace_context

        ctx, factory, ws_id = owner_ctx
        prop_a = _seed_property(factory, workspace_id=ws_id, name="Villa A")
        prop_b = _seed_property(factory, workspace_id=ws_id, name="Villa B")
        with factory() as s:
            mgr = bootstrap_user(
                s, email="mgr-wide@example.com", display_name="MgrWide"
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=mgr.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            mgr_id = mgr.id
        manager_ctx = build_workspace_context(
            workspace_id=ctx.workspace_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=mgr_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(manager_ctx, factory)
        body = client.get("/properties").json()
        ids = {row["id"] for row in body}
        assert ids == {prop_a, prop_b}
