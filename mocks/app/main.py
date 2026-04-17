"""miployees — UI preview mocks (JSON API + SPA fallback).

Presentational only. Mutations are in-memory. A `role` cookie picks
employee vs manager; `/switch/<role>` toggles. `theme` cookie picks
light vs dark; `/theme/toggle` flips.

This module exposes:

- `/api/v1/*` — read/write JSON endpoints used by the Vite/React SPA
  under `mocks/web/`. Bodies are JSON, responses are JSON-serialised
  dataclasses. No Jinja templates anywhere.
- `/events` — Server-Sent Events stream emitting deterministic mock
  events so the SPA can prove its SSE + invalidation wiring.
- `/switch/<role>`, `/theme/toggle`, `/agent/sidebar/<state>` —
  cookie-setting endpoints preserved for atomicity (the server is
  authoritative for the preference cookie).
- SPA catch-all — any other GET falls through to
  `mocks/web/dist/index.html`, so deep-linking (/today, /dashboard, …)
  works in production.
- `/healthz`, `/readyz`, `/metrics` — unchanged.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import mock_data as md


BASE_DIR = Path(__file__).resolve().parent
# The SPA build lives outside the Python package so it can be produced
# by a separate Docker stage. In dev, Vite serves /src/* and proxies
# unknown paths here; in prod the Dockerfile copies dist/ to this path.
WEB_DIST = BASE_DIR.parent / "web" / "dist"

app = FastAPI(title="miployees mocks", docs_url=None, redoc_url=None, openapi_url=None)


ROLE_COOKIE = "miployees_role"
THEME_COOKIE = "miployees_theme"
AGENT_COLLAPSED_COOKIE = "miployees_agent_collapsed"
VALID_ROLES = {"employee", "manager"}
VALID_THEMES = {"light", "dark"}


def current_role(request: Request) -> str:
    r = request.cookies.get(ROLE_COOKIE)
    return r if r in VALID_ROLES else "employee"


def current_theme(request: Request) -> str:
    t = request.cookies.get(THEME_COOKIE)
    return t if t in VALID_THEMES else "light"


# ── JSON encoding helpers ─────────────────────────────────────────────

def _encode(obj: Any) -> Any:
    """Recursively serialise dataclasses + datetimes for JSONResponse.

    FastAPI's default encoder handles dataclasses but chokes on datetime
    values inside `dict` fields (e.g. `WORKSPACE_SETTINGS`); this keeps
    the output predictable for the SPA.
    """
    if isinstance(obj, md.AssetAction):
        # `next_due` is computed on read, not stored (§21).
        out = {k: _encode(v) for k, v in asdict(obj).items()}
        out["next_due_on"] = _encode(_asset_action_next_due(obj))
        return out
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _encode(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat(timespec="minutes")
    return obj


def _asset_action_next_due(action: "md.AssetAction") -> date | None:
    """Per §21: next_due = COALESCE(last_performed_at, asset.installed_on,
    asset.created_at) + interval_days. Computed on read, not stored.

    The anchor must be a stable persisted timestamp — never `TODAY` —
    so the due date doesn't drift forward on every read. If the chain
    is exhausted, return None (the action isn't due yet).
    """
    if action.interval_days is None:
        return None
    asset = md.asset_by_id(action.asset_id)
    anchor = action.last_performed_at
    if anchor is None and asset is not None:
        anchor = asset.installed_on or asset.purchased_on
    if anchor is None:
        return None
    return anchor + timedelta(days=action.interval_days)


def ok(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(_encode(payload), status_code=status_code)


# ── SSE hub ───────────────────────────────────────────────────────────

class _EventHub:
    """In-process pub/sub for SSE. One queue per subscriber.

    Writes piggyback on regular HTTP mutations (`/api/v1/*` POSTs) so
    every connected SPA sees the change without an extra round-trip.
    A background ticker also emits a `tick` every 25s so the connection
    stays alive behind proxies.
    """

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[tuple[str, str]]] = set()

    def subscribe(self) -> asyncio.Queue[tuple[str, str]]:
        q: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[str, str]]) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: Any) -> None:
        payload = json.dumps(_encode(data))
        for q in list(self._subs):
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                # Slow subscriber; drop it so fast subscribers don't stall.
                self._subs.discard(q)


hub = _EventHub()


# ── Health / ops ──────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    return {"ok": True, "checks": {"db": "ok", "redis": "ok", "llm": "ok"}}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return (
        "# HELP miployees_tasks_completed_total Total tasks completed\n"
        "# TYPE miployees_tasks_completed_total counter\n"
        'miployees_tasks_completed_total{property="Villa Sud"} 1\n'
        'miployees_tasks_pending{property="Villa Sud"} 4\n'
        "miployees_shift_active 1\n"
    )


# ── Preference endpoints (server-authoritative cookies) ───────────────

@app.get("/switch/{role}")
def switch_role(role: str) -> Response:
    if role not in VALID_ROLES:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie(ROLE_COOKIE, role, max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/theme/toggle")
@app.get("/theme/toggle")
def theme_toggle(request: Request) -> Response:
    new_theme = "dark" if current_theme(request) == "light" else "light"
    resp = JSONResponse({"ok": True, "theme": new_theme})
    resp.set_cookie(THEME_COOKIE, new_theme, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/agent/sidebar/{state}")
def agent_sidebar_set(state: str) -> Response:
    if state not in {"open", "collapsed"}:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True, "state": state})
    resp.set_cookie(
        AGENT_COLLAPSED_COOKIE,
        "1" if state == "collapsed" else "0",
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )
    return resp


# ══════════════════════════════════════════════════════════════════════
# JSON API — reads
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/me")
def api_me(request: Request) -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return ok({
        "role": current_role(request),
        "theme": current_theme(request),
        "agent_sidebar_collapsed": request.cookies.get(AGENT_COLLAPSED_COOKIE) == "1",
        "employee": emp,
        "manager_name": md.DEFAULT_MANAGER_NAME,
        "today": md.TODAY,
        "now": md.NOW,
    })


@app.get("/api/v1/properties")
def api_properties() -> Response:
    return ok(md.PROPERTIES)


@app.get("/api/v1/properties/{pid}")
def api_property(pid: str) -> Response:
    prop = md.property_by_id(pid)
    return ok({
        "property": prop,
        "property_tasks": [t for t in md.TASKS if t.property_id == pid],
        "stays": md.stays_for_property(pid),
        "inventory": md.inventory_for_property(pid),
        "instructions": [i for i in md.INSTRUCTIONS if i.property_id == pid or i.scope == "global"],
        "closures": md.closures_for_property(pid),
        "lifecycle_rules": md.lifecycle_rules_for_property(pid),
        "assets": md.assets_for_property(pid),
        "asset_documents": md.documents_for_property(pid),
    })


@app.get("/api/v1/employees")
def api_employees() -> Response:
    """Legacy alias — see `/api/v1/users`.

    In the v1 identity model (§02, §05) there is no `employee` entity;
    people who perform work are `users` with a `work_engagement` per
    workspace. The SPA still asks for /employees, and the compat
    `Employee` shape (user × engagement × work_role projection) keeps
    the UI working unchanged. New code should call /users instead.
    """
    return ok(md.EMPLOYEES)


@app.get("/api/v1/employees/{eid}")
def api_employee(eid: str) -> Response:
    emp = md.employee_by_id(eid)
    return ok({
        "subject": emp,
        "subject_tasks": md.tasks_for_employee(eid),
        "subject_expenses": md.expenses_for_employee(eid),
        "subject_leaves": md.leaves_for_employee(eid),
        "subject_payslips": md.payslips_for_employee(eid),
        "subject_shifts": md.shifts_for_employee(eid),
    })


@app.get("/api/v1/employees/{eid}/leaves")
def api_employee_leaves(eid: str) -> Response:
    return ok({"subject": md.employee_by_id(eid), "leaves": md.leaves_for_employee(eid)})


# ── v1 identity endpoints (§02, §03, §05, §22) ──────────────────────
# These are the canonical shape; /employees and /managers stay as
# aliases so the existing web UI continues to load.


@app.get("/api/v1/users")
def api_users(workspace_id: str = "") -> Response:
    """List users visible in the deployment (or scoped to a workspace).

    With a `workspace_id` query param the list is narrowed to users
    who have at least one active grant or work_engagement resolving
    into that workspace (via `USER_WORKSPACES`).
    """
    if workspace_id:
        return ok(md.users_in_workspace(workspace_id))
    return ok(md.USERS)


@app.get("/api/v1/users/{uid}")
def api_user(uid: str) -> Response:
    user = md.user_by_id(uid)
    if user is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    grants = md.role_grants_for_user(uid)
    engagements = md.work_engagements_for_user(uid)
    work_roles = md.user_work_roles_for_user(uid)
    return ok({
        "user": user,
        "role_grants": grants,
        "work_engagements": engagements,
        "user_work_roles": work_roles,
    })


@app.post("/api/v1/users/invite")
def api_users_invite(payload: dict[str, Any] = Body(...)) -> Response:
    """Unified invite (§03).

    Accepts the v1 payload shape:
        {
          email, display_name,
          grants: [ {scope_kind, scope_id, grant_role,
                      binding_org_id?, capability_override?}, ... ],
          work_engagement?: {workspace_id, engagement_kind, ...},
          user_work_roles?: [ {workspace_id, work_role_id}, ... ]
        }

    The mock does not email a magic link; it creates (or reuses) a
    `users` row and inserts the requested rows, then returns the
    resulting triple.
    """
    email = str(payload.get("email") or "").strip().lower()
    display_name = str(payload.get("display_name") or "").strip()
    if not email or not display_name:
        return JSONResponse({"detail": "email and display_name required"}, status_code=422)

    existing = md.user_by_email(email)
    if existing is not None:
        user = existing
    else:
        user = md.User(
            id=f"u-{len(md.USERS) + 1:03d}",
            email=email,
            display_name=display_name,
            languages=list(payload.get("languages") or []),
            preferred_locale=payload.get("preferred_locale"),
        )
        md.USERS.append(user)

    created_grants: list[md.RoleGrant] = []
    for gi, g in enumerate(payload.get("grants") or []):
        scope_kind = str(g.get("scope_kind") or "workspace")
        scope_id = str(g.get("scope_id") or "")
        grant_role = str(g.get("grant_role") or "worker")
        if scope_kind not in ("workspace", "property", "organization"):
            continue
        if grant_role not in ("owner", "manager", "worker", "client", "guest"):
            continue
        row = md.RoleGrant(
            id=f"rg-{user.id}-{len(md.ROLE_GRANTS) + gi + 1}",
            user_id=user.id,
            scope_kind=scope_kind,  # type: ignore[arg-type]
            scope_id=scope_id,
            grant_role=grant_role,  # type: ignore[arg-type]
            binding_org_id=g.get("binding_org_id"),
            capability_override=dict(g.get("capability_override") or {}),
            started_on=md.TODAY,
        )
        md.ROLE_GRANTS.append(row)
        created_grants.append(row)
        hub.publish("role_grant.created", {"grant": row})

    created_engagement: md.WorkEngagement | None = None
    we_payload = payload.get("work_engagement") or None
    if we_payload:
        we_workspace = str(we_payload.get("workspace_id") or "")
        if we_workspace:
            created_engagement = md.WorkEngagement(
                id=f"we-{user.id}-{we_workspace}",
                user_id=user.id,
                workspace_id=we_workspace,
                engagement_kind=str(we_payload.get("engagement_kind") or "payroll"),  # type: ignore[arg-type]
                supplier_org_id=we_payload.get("supplier_org_id"),
                started_on=md.TODAY,
            )
            md.WORK_ENGAGEMENTS.append(created_engagement)
            hub.publish("work_engagement.created", {"work_engagement": created_engagement})

    created_work_roles: list[md.UserWorkRole] = []
    for uwr_i, uwr in enumerate(payload.get("user_work_roles") or []):
        we_workspace = str(uwr.get("workspace_id") or "")
        work_role_id = str(uwr.get("work_role_id") or "")
        if not we_workspace or not work_role_id:
            continue
        row = md.UserWorkRole(
            id=f"uwr-{user.id}-{work_role_id}-{uwr_i}",
            user_id=user.id,
            workspace_id=we_workspace,
            work_role_id=work_role_id,
            started_on=md.TODAY,
        )
        md.USER_WORK_ROLES.append(row)
        created_work_roles.append(row)

    hub.publish("user.invited", {"user": user, "grants": created_grants})
    return ok({
        "user": user,
        "grants": created_grants,
        "work_engagement": created_engagement,
        "user_work_roles": created_work_roles,
    }, status_code=201)


@app.get("/api/v1/role_grants")
def api_role_grants(user_id: str = "", scope_kind: str = "", scope_id: str = "") -> Response:
    rows = [
        g for g in md.ROLE_GRANTS
        if g.revoked_at is None
        and (not user_id or g.user_id == user_id)
        and (not scope_kind or g.scope_kind == scope_kind)
        and (not scope_id or g.scope_id == scope_id)
    ]
    return ok(rows)


# ── Permission model (§02, §05) ─────────────────────────────────────


def _derived_group_members(group: "md.PermissionGroup") -> list[str]:
    """Return user_ids whose active role_grants on this scope make
    them members of a derived system group (managers / all_workers /
    all_clients)."""
    target_role_map = {
        "managers": "manager",
        "all_workers": "worker",
        "all_clients": "client",
    }
    grant_role = target_role_map.get(group.key)
    if grant_role is None:
        return []
    return sorted({
        g.user_id for g in md.ROLE_GRANTS
        if g.revoked_at is None
        and g.scope_kind == group.scope_kind
        and g.scope_id == group.scope_id
        and g.grant_role == grant_role
    })


def _explicit_group_members(group_id: str) -> list[str]:
    return sorted({
        m.user_id for m in md.PERMISSION_GROUP_MEMBERS
        if m.group_id == group_id and m.revoked_at is None
    })


def _group_member_ids(group: "md.PermissionGroup") -> list[str]:
    if group.is_derived:
        return _derived_group_members(group)
    return _explicit_group_members(group.id)


def _find_group(group_id: str) -> "md.PermissionGroup | None":
    for g in md.PERMISSION_GROUPS:
        if g.id == group_id and g.deleted_at is None:
            return g
    return None


def _groups_for_scope(scope_kind: str, scope_id: str) -> list["md.PermissionGroup"]:
    return [
        g for g in md.PERMISSION_GROUPS
        if g.deleted_at is None
        and g.scope_kind == scope_kind
        and g.scope_id == scope_id
    ]


def _scope_chain(scope_kind: str, scope_id: str) -> list[tuple[str, str]]:
    """Most-specific scope first. For a property, yields
    (property, <id>) then the property's workspaces from
    property_workspace (§02); for workspace / organization it yields
    just that scope."""
    if scope_kind == "property":
        chain: list[tuple[str, str]] = [("property", scope_id)]
        for pw in md.PROPERTY_WORKSPACES:
            if pw.property_id == scope_id:
                chain.append(("workspace", pw.workspace_id))
        return chain
    return [(scope_kind, scope_id)]


def _is_owner_member(user_id: str, scope_kind: str, scope_id: str) -> bool:
    for group in md.PERMISSION_GROUPS:
        if (
            group.deleted_at is None
            and group.key == "owners"
            and group.scope_kind == scope_kind
            and group.scope_id == scope_id
        ):
            return user_id in _explicit_group_members(group.id)
    return False


def _user_groups_on_scope(user_id: str, scope_kind: str, scope_id: str) -> list[str]:
    """Return system-group keys + user-defined group ids that contain
    user on the given scope. Used by the resolver to match rule
    subjects and catalog defaults."""
    hits: list[str] = []
    for g in _groups_for_scope(scope_kind, scope_id):
        members = _group_member_ids(g)
        if user_id in members:
            hits.append(g.key if g.group_kind == "system" else g.id)
    return hits


def _rules_for(scope_kind: str, scope_id: str, action_key: str) -> list["md.PermissionRule"]:
    return [
        r for r in md.PERMISSION_RULES
        if r.revoked_at is None
        and r.scope_kind == scope_kind
        and r.scope_id == scope_id
        and r.action_key == action_key
    ]


def _catalog_entry(action_key: str) -> "md.ActionCatalogEntry | None":
    for e in md.ACTION_CATALOG:
        if e.key == action_key:
            return e
    return None


def _resolve_action(user_id: str, action_key: str,
                    scope_kind: str, scope_id: str) -> dict[str, Any]:
    entry = _catalog_entry(action_key)
    if entry is None:
        return {
            "effect": "deny",
            "source_layer": "unknown_action",
            "source_rule_id": None,
            "matched_groups": [],
        }

    chain = _scope_chain(scope_kind, scope_id)

    # Root-only short-circuit — owners of any scope in the chain win.
    if entry.root_only:
        for sk, sid in chain:
            if _is_owner_member(user_id, sk, sid):
                return {
                    "effect": "allow",
                    "source_layer": "root_only_owners",
                    "source_rule_id": None,
                    "matched_groups": ["owners"],
                }
        return {
            "effect": "deny",
            "source_layer": "root_only_owners",
            "source_rule_id": None,
            "matched_groups": [],
        }

    # Root-protected-deny: owners cannot be denied.
    owner_on_chain = any(
        _is_owner_member(user_id, sk, sid) for sk, sid in chain
    )

    for sk, sid in chain:
        rules = _rules_for(sk, sid, action_key)
        user_groups = set(_user_groups_on_scope(user_id, sk, sid))
        matched: list["md.PermissionRule"] = []
        for rule in rules:
            if rule.subject_kind == "user" and rule.subject_id == user_id:
                matched.append(rule)
            elif rule.subject_kind == "group":
                # Match rule subject to either a system group key or
                # user-defined group id. Translate the subject_id:
                subject_group = _find_group(rule.subject_id)
                if subject_group is None:
                    continue
                target = (
                    subject_group.key if subject_group.group_kind == "system"
                    else subject_group.id
                )
                if target in user_groups and subject_group.scope_kind == sk and subject_group.scope_id == sid:
                    matched.append(rule)
                # A group-subject rule targeting a scope in the chain
                # but on a different (scope_kind, scope_id) than the
                # group itself would be a validation error; skip.
        if not matched:
            continue
        deny = next((r for r in matched if r.effect == "deny"), None)
        if deny is not None:
            if entry.root_protected_deny and owner_on_chain:
                # Owners cannot be denied; fall through to the allow
                # lookup below but without this deny influencing the
                # outcome.
                pass
            else:
                return {
                    "effect": "deny",
                    "source_layer": f"rule:{sk}",
                    "source_rule_id": deny.id,
                    "matched_groups": sorted(user_groups),
                }
        allow = next((r for r in matched if r.effect == "allow"), None)
        if allow is not None:
            return {
                "effect": "allow",
                "source_layer": f"rule:{sk}",
                "source_rule_id": allow.id,
                "matched_groups": sorted(user_groups),
            }

    # Catalog default.
    default_allow = set(entry.default_allow)
    for sk, sid in chain:
        user_groups = set(_user_groups_on_scope(user_id, sk, sid))
        hit = default_allow & user_groups
        if hit:
            return {
                "effect": "allow",
                "source_layer": "catalog_default",
                "source_rule_id": None,
                "matched_groups": sorted(hit),
            }
    return {
        "effect": "deny",
        "source_layer": "catalog_default",
        "source_rule_id": None,
        "matched_groups": [],
    }


@app.get("/api/v1/permission_groups")
def api_permission_groups(scope_kind: str = "", scope_id: str = "") -> Response:
    rows = [
        g for g in md.PERMISSION_GROUPS
        if g.deleted_at is None
        and (not scope_kind or g.scope_kind == scope_kind)
        and (not scope_id or g.scope_id == scope_id)
    ]
    return ok(rows)


@app.get("/api/v1/permission_groups/{gid}")
def api_permission_group(gid: str) -> Response:
    g = _find_group(gid)
    if g is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(g)


@app.get("/api/v1/permission_groups/{gid}/members")
def api_permission_group_members(gid: str) -> Response:
    g = _find_group(gid)
    if g is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    member_ids = _group_member_ids(g)
    return ok({
        "group_id": gid,
        "is_derived": g.is_derived,
        "members": [
            {"user_id": uid, "derived": g.is_derived}
            for uid in member_ids
        ],
    })


@app.get("/api/v1/permission_rules")
def api_permission_rules(scope_kind: str = "", scope_id: str = "",
                         action_key: str = "") -> Response:
    rows = [
        r for r in md.PERMISSION_RULES
        if r.revoked_at is None
        and (not scope_kind or r.scope_kind == scope_kind)
        and (not scope_id or r.scope_id == scope_id)
        and (not action_key or r.action_key == action_key)
    ]
    return ok(rows)


@app.get("/api/v1/permissions/action_catalog")
def api_action_catalog() -> Response:
    return ok(md.ACTION_CATALOG)


@app.get("/api/v1/permissions/resolved")
def api_permissions_resolved(
    user_id: str,
    action_key: str,
    scope_kind: str,
    scope_id: str,
) -> Response:
    return ok(_resolve_action(user_id, action_key, scope_kind, scope_id))


@app.get("/api/v1/work_engagements")
def api_work_engagements(user_id: str = "", workspace_id: str = "") -> Response:
    rows = [
        w for w in md.WORK_ENGAGEMENTS
        if (not user_id or w.user_id == user_id)
        and (not workspace_id or w.workspace_id == workspace_id)
    ]
    return ok(rows)


@app.get("/api/v1/work_engagements/{weid}")
def api_work_engagement(weid: str) -> Response:
    we = md.work_engagement_by_id(weid)
    if we is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(we)


@app.get("/api/v1/work_roles")
def api_work_roles(workspace_id: str = "") -> Response:
    rows = md.WORK_ROLES
    if workspace_id:
        rows = [r for r in rows if r.workspace_id == workspace_id]
    return ok(rows)


@app.get("/api/v1/user_work_roles")
def api_user_work_roles(user_id: str = "", workspace_id: str = "") -> Response:
    rows = md.USER_WORK_ROLES
    if user_id:
        rows = [r for r in rows if r.user_id == user_id]
    if workspace_id:
        rows = [r for r in rows if r.workspace_id == workspace_id]
    return ok(rows)


@app.get("/api/v1/workspaces")
def api_workspaces() -> Response:
    return ok(md.WORKSPACES)


@app.get("/api/v1/property_workspaces")
def api_property_workspaces(property_id: str = "", workspace_id: str = "") -> Response:
    rows = md.PROPERTY_WORKSPACES
    if property_id:
        rows = [r for r in rows if r.property_id == property_id]
    if workspace_id:
        rows = [r for r in rows if r.workspace_id == workspace_id]
    return ok(rows)


@app.get("/api/v1/organizations")
def api_organizations(workspace_id: str = "") -> Response:
    rows = md.ORGANIZATIONS
    if workspace_id:
        rows = [o for o in rows if o.workspace_id == workspace_id]
    return ok(rows)


# `/api/v1/managers` — legacy alias. In v1 there is no `manager`
# entity; the UI asks for it only in a handful of legacy spots. We
# return users who hold an `owner` or `manager` grant somewhere.
@app.get("/api/v1/managers")
def api_managers() -> Response:
    manager_ids = {
        g.user_id for g in md.ROLE_GRANTS
        if g.grant_role in ("owner", "manager") and g.revoked_at is None
    }
    return ok([u for u in md.USERS if u.id in manager_ids])


@app.get("/api/v1/tasks")
def api_tasks() -> Response:
    return ok(md.TASKS)


@app.get("/api/v1/tasks/{tid}")
def api_task(tid: str) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok({
        "task": task,
        "property": md.property_by_id(task.property_id),
        "instructions": md.instructions_for_task(task),
        "comments": md.comments_for_task(tid),
    })


@app.get("/api/v1/today")
def api_today(request: Request) -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    tasks = sorted(md.tasks_for_employee(emp.id), key=lambda t: t.scheduled_start)
    today_tasks = [t for t in tasks if t.scheduled_start.date() == md.TODAY]
    now_task = next((t for t in today_tasks if t.status in {"pending", "in_progress"}), None)
    upcoming = [t for t in today_tasks if t is not now_task and t.status in {"pending", "in_progress"}]
    completed = [t for t in today_tasks if t.status == "completed"]
    _ = request  # quiet linters
    return ok({"now_task": now_task, "upcoming": upcoming, "completed": completed,
               "properties": md.PROPERTIES})


@app.get("/api/v1/week")
def api_week() -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return ok({
        "tasks": sorted(md.tasks_for_employee(emp.id), key=lambda t: t.scheduled_start),
        "properties": md.PROPERTIES,
    })


@app.get("/api/v1/dashboard")
def api_dashboard() -> Response:
    on_shift = [e for e in md.EMPLOYEES if e.clocked_in_at]
    today_tasks = [t for t in md.TASKS if t.scheduled_start.date() == md.TODAY]
    by_status = {
        "completed":   [t for t in today_tasks if t.status == "completed"],
        "in_progress": [t for t in today_tasks if t.status == "in_progress"],
        "pending":     [t for t in today_tasks if t.status == "pending"],
    }
    return ok({
        "on_shift": on_shift,
        "by_status": by_status,
        "pending_approvals": md.APPROVALS,
        "pending_expenses": [x for x in md.EXPENSES if x.status == "submitted"],
        "pending_leaves": [lv for lv in md.LEAVES if lv.approved_at is None],
        "open_issues": [i for i in md.ISSUES if i.status != "resolved"],
        "stays_today": [s for s in md.STAYS if s.check_in <= md.TODAY <= s.check_out],
        "properties": md.PROPERTIES,
        "employees": md.EMPLOYEES,
    })


@app.get("/api/v1/expenses")
def api_expenses(mine: bool = False) -> Response:
    if mine:
        return ok(md.expenses_for_employee(md.DEFAULT_EMPLOYEE_ID))
    return ok(md.EXPENSES)


@app.get("/api/v1/issues")
def api_issues() -> Response:
    return ok(md.ISSUES)


@app.get("/api/v1/stays")
def api_stays() -> Response:
    return ok({
        "stays": sorted(md.STAYS, key=lambda s: s.check_in),
        "closures": md.CLOSURES,
        "leaves": [lv for lv in md.LEAVES if lv.approved_at is not None],
    })


@app.get("/api/v1/property_closures")
def api_property_closures(property_id: str) -> Response:
    return ok({
        "property": md.property_by_id(property_id),
        "closures": md.closures_for_property(property_id),
        "stays": md.stays_for_property(property_id),
    })


@app.get("/api/v1/task_templates")
def api_templates() -> Response:
    return ok(md.TEMPLATES)


@app.get("/api/v1/schedules")
def api_schedules() -> Response:
    return ok({
        "schedules": md.SCHEDULES,
        "templates_by_id": {t.id: t for t in md.TEMPLATES},
    })


@app.get("/api/v1/instructions")
def api_instructions() -> Response:
    return ok(md.INSTRUCTIONS)


@app.get("/api/v1/instructions/{iid}")
def api_instruction(iid: str) -> Response:
    instr = next((i for i in md.INSTRUCTIONS if i.id == iid), None)
    if instr is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(instr)


@app.get("/api/v1/inventory")
def api_inventory() -> Response:
    return ok(md.INVENTORY)


@app.get("/api/v1/payslips")
def api_payslips() -> Response:
    current = [p for p in md.PAYSLIPS if p.period_starts.month == 4]
    previous = [p for p in md.PAYSLIPS if p.period_starts.month == 3]
    return ok({"current": current, "previous": previous})


@app.get("/api/v1/shifts")
def api_shifts() -> Response:
    return ok(md.SHIFTS)


@app.get("/api/v1/pay_rules")
def api_pay_rules() -> Response:
    return ok(md.PAY_RULES)


@app.get("/api/v1/pay_periods")
def api_pay_periods() -> Response:
    return ok(md.PAY_PERIODS)


@app.get("/api/v1/lifecycle_rules")
def api_lifecycle_rules(property_id: str = "") -> Response:
    if property_id:
        return ok(md.lifecycle_rules_for_property(property_id))
    return ok(md.LIFECYCLE_RULES)


@app.get("/api/v1/leaves")
def api_leaves() -> Response:
    return ok({
        "pending": [lv for lv in md.LEAVES if lv.approved_at is None],
        "approved": [lv for lv in md.LEAVES if lv.approved_at is not None],
    })


@app.get("/api/v1/approvals")
def api_approvals() -> Response:
    return ok(md.APPROVALS)


@app.get("/api/v1/audit")
def api_audit() -> Response:
    return ok(md.AUDIT)


@app.get("/api/v1/webhooks")
def api_webhooks() -> Response:
    return ok(md.WEBHOOKS)


@app.get("/api/v1/llm/assignments")
def api_llm_assignments() -> Response:
    total_spent = sum(a.spent_24h_usd for a in md.LLM_ASSIGNMENTS)
    total_budget = sum(a.daily_budget_usd for a in md.LLM_ASSIGNMENTS)
    total_calls = sum(a.calls_24h for a in md.LLM_ASSIGNMENTS)
    return ok({
        "assignments": md.LLM_ASSIGNMENTS,
        "total_spent": total_spent,
        "total_budget": total_budget,
        "total_calls": total_calls,
    })


@app.get("/api/v1/llm/calls")
def api_llm_calls() -> Response:
    return ok(md.LLM_CALLS)


@app.get("/api/v1/settings")
def api_settings() -> Response:
    return ok({
        "meta": md.WORKSPACE_META,
        "defaults": md.WORKSPACE_SETTINGS,
        "policy": md.WORKSPACE_POLICY,
    })


@app.get("/api/v1/settings/catalog")
def api_settings_catalog() -> Response:
    return ok(md.SETTINGS_CATALOG)


@app.get("/api/v1/settings/resolved")
def api_settings_resolved(entity_kind: str = "", entity_id: str = "") -> Response:
    prop_override: dict[str, Any] | None = None
    emp_override: dict[str, Any] | None = None
    task_override: dict[str, Any] | None = None
    if entity_kind == "property":
        prop = md.property_by_id(entity_id)
        prop_override = prop.settings_override
    elif entity_kind == "employee":
        emp = md.employee_by_id(entity_id)
        emp_override = emp.settings_override
        # Also pick the first property for context.
        if emp.properties:
            try:
                prop = md.property_by_id(emp.properties[0])
                prop_override = prop.settings_override
            except StopIteration:
                pass
    elif entity_kind == "task":
        task = md.task_by_id(entity_id)
        if task:
            task_override = task.settings_override
            try:
                prop = md.property_by_id(task.property_id)
                prop_override = prop.settings_override
            except StopIteration:
                pass
            try:
                emp = md.employee_by_id(task.assignee_id)
                emp_override = emp.settings_override
            except StopIteration:
                pass
    resolved = md.resolve_settings(
        md.WORKSPACE_SETTINGS,
        property_override=prop_override,
        employee_override=emp_override,
        task_override=task_override,
    )
    return ok({"entity_kind": entity_kind, "entity_id": entity_id, "settings": resolved})


@app.get("/api/v1/properties/{pid}/settings")
def api_property_settings(pid: str) -> Response:
    prop = md.property_by_id(pid)
    resolved = md.resolve_settings(md.WORKSPACE_SETTINGS, property_override=prop.settings_override)
    return ok({"overrides": prop.settings_override, "resolved": resolved})


@app.get("/api/v1/employees/{eid}/settings")
def api_employee_settings(eid: str) -> Response:
    emp = md.employee_by_id(eid)
    prop_override: dict[str, Any] | None = None
    if emp.properties:
        try:
            prop = md.property_by_id(emp.properties[0])
            prop_override = prop.settings_override
        except StopIteration:
            pass
    resolved = md.resolve_settings(
        md.WORKSPACE_SETTINGS,
        property_override=prop_override,
        employee_override=emp.settings_override,
    )
    return ok({"overrides": emp.settings_override, "resolved": resolved})


# ── Assets & documents ───────────────────────────────────────────────

@app.get("/api/v1/asset_types")
def api_asset_types() -> Response:
    return ok(md.ASSET_TYPES)


@app.get("/api/v1/assets")
def api_assets(property_id: str = "", category: str = "", condition: str = "") -> Response:
    result = list(md.ASSETS)
    if property_id:
        result = [a for a in result if a.property_id == property_id]
    if category:
        type_ids = {t.id for t in md.ASSET_TYPES if t.category == category}
        result = [a for a in result if a.asset_type_id in type_ids]
    if condition:
        result = [a for a in result if a.condition == condition]
    return ok(result)


@app.get("/api/v1/assets/{aid}")
def api_asset(aid: str) -> Response:
    asset = md.asset_by_id(aid)
    if asset is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    asset_type = md.asset_type_by_id(asset.asset_type_id) if asset.asset_type_id else None
    actions = md.actions_for_asset(aid)
    docs = md.documents_for_asset(aid)
    linked_tasks = [t for t in md.TASKS if t.asset_id == aid]
    return ok({
        "asset": asset,
        "asset_type": asset_type,
        "property": md.property_by_id(asset.property_id),
        "actions": actions,
        "documents": docs,
        "linked_tasks": linked_tasks,
    })


@app.get("/api/v1/documents")
def api_documents(property_id: str = "", asset_id: str = "", kind: str = "") -> Response:
    result = list(md.ASSET_DOCUMENTS)
    if property_id:
        result = [d for d in result if d.property_id == property_id]
    if asset_id:
        result = [d for d in result if d.asset_id == asset_id]
    if kind:
        result = [d for d in result if d.kind == kind]
    return ok(result)


@app.post("/api/v1/assets/{aid}/actions/{action_id}/complete")
def api_asset_action_complete(aid: str, action_id: str) -> Response:
    action = next((a for a in md.ASSET_ACTIONS if a.id == action_id and a.asset_id == aid), None)
    if action is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    action.last_performed_at = md.TODAY
    hub.publish("asset_action.performed", {"asset_id": aid, "action": action})
    return ok(action)


@app.get("/api/v1/agent/employee/log")
def api_agent_employee_log() -> Response:
    return ok(md.EMPLOYEE_CHAT_LOG)


@app.get("/api/v1/agent/manager/log")
def api_agent_manager_log() -> Response:
    return ok(md.MANAGER_AGENT_LOG)


@app.get("/api/v1/agent/manager/actions")
def api_agent_manager_actions() -> Response:
    return ok(md.MANAGER_AGENT_ACTIONS)


@app.get("/api/v1/guest")
def api_guest() -> Response:
    stay = md.stay_by_id(md.GUEST_STAY_ID)
    turnover_task = next((t for t in md.TASKS if t.turnover_bundle_id == "tb-apt-3b-18"), None)
    guest_checklist = [c for c in (turnover_task.checklist if turnover_task else []) if c.get("guest_visible")]
    guest_assets: list[md.Asset] = []
    if stay:
        guest_assets = [a for a in md.assets_for_property(stay.property_id) if a.guest_visible]
    return ok({
        "stay": stay,
        "property": md.property_by_id(stay.property_id) if stay else None,
        "guest_checklist": guest_checklist,
        "guest_assets": guest_assets,
    })


@app.get("/api/v1/history")
def api_history(tab: str = "tasks") -> Response:
    if tab not in {"tasks", "chats", "expenses", "leaves"}:
        tab = "tasks"
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return ok({
        "tab": tab,
        "tasks": [t for t in md.tasks_for_employee(emp.id) if t.status in {"completed", "skipped"}],
        "expenses": [x for x in md.expenses_for_employee(emp.id) if x.status in {"approved", "reimbursed", "rejected"}],
        "leaves": [lv for lv in md.leaves_for_employee(emp.id) if lv.approved_at is not None and lv.ends_on < md.TODAY],
        "chats": md.HISTORY.get("chats", []),
    })


# ══════════════════════════════════════════════════════════════════════
# JSON API — writes
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/shifts/toggle")
def api_shifts_toggle() -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    emp.clocked_in_at = None if emp.clocked_in_at else md.NOW
    return ok(emp)


@app.post("/api/v1/tasks/{tid}/check/{idx}")
def api_task_check(tid: str, idx: int) -> Response:
    task = md.task_by_id(tid)
    if task is None or idx < 0 or idx >= len(task.checklist):
        return JSONResponse({"detail": "not found"}, status_code=404)
    task.checklist[idx]["done"] = not task.checklist[idx].get("done", False)
    hub.publish("task.updated", {"task": task})
    return ok(task)


@app.post("/api/v1/tasks/{tid}/complete")
def api_task_complete(tid: str) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    task.status = "completed"
    hub.publish("task.completed", {"task": task})
    return ok(task)


@app.post("/api/v1/tasks/{tid}/skip")
def api_task_skip(tid: str, payload: dict[str, Any] = Body(default_factory=dict)) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    task.status = "skipped"
    reason = payload.get("reason")
    hub.publish("task.skipped", {"task": task, "reason": reason})
    return ok(task)


_scan_counter = 0

_SCAN_SCENARIOS: list[dict[str, Any]] = [
    {
        "vendor":            {"value": "Carrefour Market", "confidence": 0.97},
        "purchased_at":      {"value": "2026-04-15T14:32:00", "confidence": 0.95},
        "currency":          {"value": "EUR", "confidence": 0.99},
        "total_amount_cents": {"value": 2340, "confidence": 0.96},
        "category":          {"value": "supplies", "confidence": 0.92},
        "note_md":           {"value": "Cleaning products — 2x bleach, sponge pack, bin bags", "confidence": 0.91},
        "agent_question":    None,
    },
    {
        "vendor":            {"value": "", "confidence": 0.35},
        "purchased_at":      {"value": "2026-04-14T09:00:00", "confidence": 0.72},
        "currency":          {"value": "EUR", "confidence": 0.98},
        "total_amount_cents": {"value": 4500, "confidence": 0.88},
        "category":          {"value": "other", "confidence": 0.40},
        "note_md":           {"value": "Bank transfer — details unclear", "confidence": 0.55},
        "agent_question":    "Who was this sent to, and what was it for?",
    },
    {
        "vendor":            {"value": "Brico Depot", "confidence": 0.78},
        "purchased_at":      {"value": "2026-04-13T16:45:00", "confidence": 0.65},
        "currency":          {"value": "EUR", "confidence": 0.99},
        "total_amount_cents": {"value": 8950, "confidence": 0.68},
        "category":          {"value": "maintenance", "confidence": 0.82},
        "note_md":           {"value": "Assorted hardware — partially illegible", "confidence": 0.62},
        "agent_question":    "Does the total of \u20ac89.50 look right? The receipt is faded at the bottom.",
    },
]


@app.post("/api/v1/expenses/scan")
def api_expenses_scan() -> Response:
    global _scan_counter
    scenario = _SCAN_SCENARIOS[_scan_counter % len(_SCAN_SCENARIOS)]
    _scan_counter += 1
    return ok(scenario)


@app.post("/api/v1/expenses")
def api_expenses_create(payload: dict[str, Any] = Body(...)) -> Response:
    try:
        cents = int(round(float(payload.get("amount", 0)) * 100))
    except (TypeError, ValueError):
        cents = 0
    cat = payload.get("category")
    ocr = payload.get("ocr_confidence")
    default_emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    x = md.Expense(
        id=f"x-{len(md.EXPENSES) + 1}",
        employee_id=md.DEFAULT_EMPLOYEE_ID,
        amount_cents=cents,
        currency=str(payload.get("currency") or "EUR"),
        merchant=str(payload.get("merchant") or "Unknown"),
        submitted_at=datetime.now(),
        status="submitted",
        note=str(payload.get("note") or ""),
        ocr_confidence=float(ocr) if ocr is not None else None,
        category=str(cat) if cat else None,
        # v1 canonical pointers — per §09 expense claims key off
        # work_engagement_id, not user_id directly.
        user_id=default_emp.user_id,
        work_engagement_id=default_emp.work_engagement_id,
    )
    md.EXPENSES.insert(0, x)
    return ok(x, status_code=201)


@app.post("/api/v1/expenses/{xid}/{decision}")
def api_expenses_decide(xid: str, decision: str) -> Response:
    # decision → new status → matching §10 webhook event name
    mapping = {
        "approve":   ("approved",   "expense.approved"),
        "reject":    ("rejected",   "expense.rejected"),
        "reimburse": ("reimbursed", "expense.reimbursed"),
    }
    pair = mapping.get(decision)
    if pair is None:
        return JSONResponse({"detail": "bad decision"}, status_code=400)
    new_status, event = pair
    for x in md.EXPENSES:
        if x.id == xid:
            x.status = new_status  # type: ignore[assignment]
            hub.publish(event, {"id": xid, "status": new_status})
            return ok(x)
    return JSONResponse({"detail": "not found"}, status_code=404)


@app.post("/api/v1/issues")
def api_issues_create(payload: dict[str, Any] = Body(...)) -> Response:
    issue = md.Issue(
        id=f"iss-{len(md.ISSUES) + 1}",
        reported_by=md.DEFAULT_EMPLOYEE_ID,
        property_id=str(payload.get("property_id") or md.PROPERTIES[0].id),
        area=str(payload.get("area") or "—"),
        severity=str(payload.get("severity") or "normal"),  # type: ignore[arg-type]
        category=str(payload.get("category") or "other"),   # type: ignore[arg-type]
        title=str(payload.get("title") or "Untitled"),
        body=str(payload.get("body") or ""),
        reported_at=datetime.now(),
        status="open",
    )
    md.ISSUES.insert(0, issue)
    return ok(issue, status_code=201)


@app.post("/api/v1/leaves/{lid}/{decision}")
def api_leaves_decide(lid: str, decision: str) -> Response:
    for lv in md.LEAVES:
        if lv.id == lid:
            if decision == "approve":
                lv.approved_at = datetime.now()
                return ok(lv)
            if decision == "reject":
                md.LEAVES.remove(lv)
                return ok({"ok": True, "id": lid})
            return JSONResponse({"detail": "bad decision"}, status_code=400)
    return JSONResponse({"detail": "not found"}, status_code=404)


@app.post("/api/v1/approvals/{aid}/{decision}")
def api_approvals_decide(aid: str, decision: str) -> Response:
    if decision not in {"approve", "reject"}:
        return JSONResponse({"detail": "bad decision"}, status_code=400)
    md.APPROVALS[:] = [a for a in md.APPROVALS if a.id != aid]
    hub.publish("approval.decided", {"id": aid, "decision": decision})
    return ok({"ok": True, "id": aid, "decision": decision})


@app.post("/api/v1/agent/employee/message")
def api_agent_employee_message(payload: dict[str, Any] = Body(...)) -> Response:
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.EMPLOYEE_CHAT_LOG.append(msg)
    hub.publish("agent.message.appended", {"scope": "employee", "message": msg})
    return ok(msg)


@app.post("/api/v1/agent/manager/message")
def api_agent_manager_message(payload: dict[str, Any] = Body(...)) -> Response:
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.MANAGER_AGENT_LOG.append(msg)
    hub.publish("agent.message.appended", {"scope": "manager", "message": msg})
    return ok(msg)


@app.post("/api/v1/agent/manager/action/{aid}/{decision}")
def api_agent_manager_action(aid: str, decision: str) -> Response:
    action = next((a for a in md.MANAGER_AGENT_ACTIONS if a.id == aid), None)
    if action is None or decision not in {"approve", "deny"}:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    md.MANAGER_AGENT_ACTIONS[:] = [a for a in md.MANAGER_AGENT_ACTIONS if a.id != aid]
    verb = "Approved" if decision == "approve" else "Denied"
    user_msg = md.AgentMessage(at=datetime.now(), kind="user", body=f"{verb}: {action.title}")
    md.MANAGER_AGENT_LOG.append(user_msg)
    hub.publish("agent.message.appended", {"scope": "manager", "message": user_msg})
    if decision == "approve":
        agent_msg = md.AgentMessage(
            at=datetime.now(), kind="agent",
            body=f"Done — {action.title.lower()} is in the audit log.",
        )
        md.MANAGER_AGENT_LOG.append(agent_msg)
        hub.publish("agent.message.appended", {"scope": "manager", "message": agent_msg})
    return ok({"ok": True, "id": aid, "decision": decision})


@app.post("/api/v1/chat/action/{idx}/{decision}")
def api_chat_action_decide(idx: int, decision: str) -> Response:
    if idx < 0 or idx >= len(md.EMPLOYEE_CHAT_LOG) or decision not in {"approve", "details"}:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    msg = md.EMPLOYEE_CHAT_LOG[idx]
    if msg.kind != "action":
        return JSONResponse({"detail": "not an action"}, status_code=400)
    if decision == "approve":
        md.EMPLOYEE_CHAT_LOG[idx] = md.AgentMessage(
            at=msg.at, kind="agent", body=f"{msg.body} — approved.",
        )
    else:
        md.EMPLOYEE_CHAT_LOG.append(md.AgentMessage(
            at=datetime.now(), kind="agent",
            body="Here are the details — receipt attached, merchant Carrefour, €12.40.",
        ))
    return ok(md.EMPLOYEE_CHAT_LOG)


# ══════════════════════════════════════════════════════════════════════
# SSE
# ══════════════════════════════════════════════════════════════════════

def _sse_format(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


async def _tick_loop() -> None:
    while True:
        await asyncio.sleep(25)
        hub.publish("tick", {"now": datetime.now()})


_tick_task: asyncio.Task[None] | None = None


@app.on_event("startup")
async def _on_startup() -> None:
    global _tick_task
    loop = asyncio.get_running_loop()
    _tick_task = loop.create_task(_tick_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _tick_task is not None:
        _tick_task.cancel()


@app.get("/events")
async def events_stream(request: Request) -> StreamingResponse:
    q = hub.subscribe()

    async def stream() -> AsyncIterator[bytes]:
        # Initial handshake so EventSource considers the stream open.
        yield _sse_format("tick", json.dumps({"now": datetime.now().isoformat()}))
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    # Keep-alive comment; most proxies drop idle SSE at 60s.
                    yield b": keep-alive\n\n"
                    continue
                yield _sse_format(event, data)
        finally:
            hub.unsubscribe(q)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


# ══════════════════════════════════════════════════════════════════════
# SPA fallback
# ══════════════════════════════════════════════════════════════════════

# Mount built assets (JS, CSS, fonts) under Vite's default /assets path.
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")


_SPA_PASSTHROUGH: Iterable[str] = (
    "/api",
    "/events",
    "/switch",
    "/theme",
    "/agent/sidebar",
    "/healthz",
    "/readyz",
    "/metrics",
)


@app.get("/{full_path:path}")
def spa_fallback(full_path: str) -> Response:
    """Serve the SPA's index.html for any non-API GET.

    FastAPI matches specific routes first, so `/api/v1/...`, `/events`,
    and cookie endpoints never reach here. We still guard a few prefix
    checks in case of path weirdness.
    """
    path = "/" + full_path
    for prefix in _SPA_PASSTHROUGH:
        if path.startswith(prefix):
            return JSONResponse({"detail": "not found"}, status_code=404)

    # Top-level static files (favicon, grain.svg, manifest) copied by
    # Vite directly under dist/.
    candidate = WEB_DIST / full_path
    if full_path and candidate.is_file() and WEB_DIST in candidate.resolve().parents:
        return FileResponse(candidate)

    index = WEB_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    # Until the SPA has been built (e.g. in dev without a build), return
    # a stub so curl/healthcheck can distinguish.
    return PlainTextResponse(
        "SPA bundle not built. Run `npm --prefix mocks/web run build` or "
        "use the `dev` compose profile with Vite on :5173.",
        status_code=503,
    )
