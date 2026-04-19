"""crewday — UI preview mocks (JSON API + SPA fallback).

Presentational only. Mutations are in-memory. A `role` cookie picks
employee vs manager; `/switch/<role>` toggles. `theme` cookie picks
light / dark / system; `/theme/set/<value>` stores it and
`/theme/toggle` cycles light→dark→system.

This module exposes:

- `/api/v1/*` — read/write JSON endpoints used by the Vite/React SPA
  under `mocks/web/`. Bodies are JSON, responses are JSON-serialised
  dataclasses. No Jinja templates anywhere.
- `/events` — Server-Sent Events stream emitting deterministic mock
  events so the SPA can prove its SSE + invalidation wiring.
- `/switch/<role>`, `/theme/toggle`, `/theme/set/<value>`,
  `/agent/sidebar/<state>` —
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
import random
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal

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

app = FastAPI(title="crewday mocks", docs_url=None, redoc_url=None, openapi_url=None)


ROLE_COOKIE = "crewday_role"
THEME_COOKIE = "crewday_theme"
AGENT_COLLAPSED_COOKIE = "crewday_agent_collapsed"
WORKSPACE_COOKIE = "crewday_workspace"
VALID_ROLES = {"employee", "manager", "client", "admin"}
VALID_THEMES = {"light", "dark", "system"}


def current_role(request: Request) -> str:
    r = request.cookies.get(ROLE_COOKIE)
    return r if r in VALID_ROLES else "employee"


def current_theme(request: Request) -> str:
    t = request.cookies.get(THEME_COOKIE)
    return t if t in VALID_THEMES else "system"


def current_workspace_id(request: Request) -> str:
    """Active workspace from cookie; falls back to the role-default.

    Workers default to Bernard, the client persona defaults to CleanCo
    (where Vincent's `client` grant lives, §22), managers stay on
    Bernard. The cookie is set by `POST /workspaces/switch/{wsid}`.
    """
    cookie_val = request.cookies.get(WORKSPACE_COOKIE)
    if cookie_val and md.workspace_by_id(cookie_val) is not None:
        return cookie_val
    role = current_role(request)
    if role == "client":
        return "ws-cleanco"
    return md.DEFAULT_WORKSPACE_ID


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
    if isinstance(obj, (md.Employee, md.User)):
        # `avatar_url` is derived from `avatar_file_id` on read (§12).
        out = {k: _encode(v) for k, v in asdict(obj).items()}
        out["avatar_url"] = md.avatar_url_for_file_id(obj.avatar_file_id)
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


# ── Agent turn simulator ──────────────────────────────────────────────
#
# §11 "Agent turn lifecycle" brackets every user→agent message with an
# `agent.turn.started` / `agent.turn.finished` SSE pair so every
# connected tab can render a typing indicator (§14 "Agent turn
# indicator"). The mock has no real model to call; this helper stands
# in by sleeping a randomised 1.5–3.5 s and appending a rotated canned
# reply so the indicator is visible end-to-end in the preview.
# Production replaces the sleep with the real LLM turn and the canned
# text with the model's output; the SSE contract is unchanged.

_MOCK_AGENT_REPLIES = (
    "Got it — pulling this together now.",
    "On it. I'll let you know what I find.",
    "Thanks — I'll take a look and get back to you.",
    "Noted. Running the checks now.",
    "Understood. I'll share the result here shortly.",
)


def _agent_turn_scope(scope: str, task_id: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"scope": scope}
    if task_id is not None:
        payload["task_id"] = task_id
    return payload


async def _simulate_agent_turn(
    scope: Literal["employee", "manager", "admin", "task"],
    log: list[md.AgentMessage],
    *,
    task_id: str | None = None,
) -> None:
    """Publish the turn lifecycle + append one canned agent reply.

    Called as a fire-and-forget `asyncio.create_task(...)` from the
    user-message handlers. Pairs its own `started` / `finished` even
    on error (the `finally` block) so the web surfaces never end up
    with a stuck typing indicator.
    """
    start_payload = _agent_turn_scope(scope, task_id)
    start_payload["started_at"] = datetime.now().isoformat()
    hub.publish("agent.turn.started", start_payload)

    outcome = "replied"
    try:
        await asyncio.sleep(random.uniform(1.5, 3.5))
        reply = md.AgentMessage(
            at=datetime.now(),
            kind="agent",
            body=random.choice(_MOCK_AGENT_REPLIES),
        )
        log.append(reply)
        appended_payload = _agent_turn_scope(scope, task_id)
        appended_payload["message"] = reply
        hub.publish("agent.message.appended", appended_payload)
    except Exception:
        outcome = "error"
        raise
    finally:
        finish_payload = _agent_turn_scope(scope, task_id)
        finish_payload["finished_at"] = datetime.now().isoformat()
        finish_payload["outcome"] = outcome
        hub.publish("agent.turn.finished", finish_payload)


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
        "# HELP crewday_tasks_completed_total Total tasks completed\n"
        "# TYPE crewday_tasks_completed_total counter\n"
        'crewday_tasks_completed_total{property="Villa Sud"} 1\n'
        'crewday_tasks_pending{property="Villa Sud"} 4\n'
        "crewday_bookings_active 1\n"
    )


# ── Preference endpoints (server-authoritative cookies) ───────────────

@app.get("/switch/{role}")
def switch_role(role: str) -> Response:
    if role not in VALID_ROLES:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie(ROLE_COOKIE, role, max_age=60 * 60 * 24 * 30, samesite="lax")
    # Switching role pivots the active workspace to a sensible default
    # so the next page load lands in a workspace the persona can see.
    if role == "client":
        resp.set_cookie(WORKSPACE_COOKIE, "ws-cleanco", max_age=60 * 60 * 24 * 30, samesite="lax")
    elif role == "manager":
        resp.set_cookie(WORKSPACE_COOKIE, md.DEFAULT_WORKSPACE_ID, max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/workspaces/switch/{wsid}")
@app.get("/workspaces/switch/{wsid}")
def switch_workspace(wsid: str) -> Response:
    if md.workspace_by_id(wsid) is None:
        return JSONResponse({"ok": False, "error": "unknown_workspace"}, status_code=404)
    resp = JSONResponse({"ok": True, "workspace_id": wsid})
    resp.set_cookie(WORKSPACE_COOKIE, wsid, max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/theme/toggle")
@app.get("/theme/toggle")
def theme_toggle(request: Request) -> Response:
    cur = current_theme(request)
    new_theme = "dark" if cur == "light" else "system" if cur == "dark" else "light"
    resp = JSONResponse({"ok": True, "theme": new_theme})
    resp.set_cookie(THEME_COOKIE, new_theme, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/theme/set/{value}")
@app.get("/theme/set/{value}")
def theme_set(value: str) -> Response:
    if value not in VALID_THEMES:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True, "theme": value})
    resp.set_cookie(THEME_COOKIE, value, max_age=60 * 60 * 24 * 365, samesite="lax")
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

def current_user_id(request: Request) -> str:
    """Resolve the signed-in user's ULID from the role cookie (§03, §11).

    The `client` persona maps to Vincent Dupont — the v1 example client
    user (§22) — so demo state immediately reflects a real client grant.
    The `admin` persona maps to Élodie (the bootstrap operator + owner
    of the deployment; §05 "Deployment scope").
    """
    role = current_role(request)
    if role == "manager" or role == "admin":
        return md.DEFAULT_MANAGER_USER_ID
    if role == "client":
        return md.DEFAULT_CLIENT_USER_ID
    return md.DEFAULT_EMPLOYEE_USER_ID


@app.get("/api/v1/me")
def api_me(request: Request) -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    me_user = md.user_by_id(current_user_id(request))
    wsid = current_workspace_id(request)
    available = md.workspaces_for_user(current_user_id(request))
    binding_org_ids = sorted({
        g.binding_org_id for g in md.role_grants_for_user(current_user_id(request))
        if g.scope_kind == "workspace" and g.scope_id == wsid
        and g.grant_role == "client" and g.binding_org_id
    })
    # §05 — deployment-admin gating. In production this is a
    # membership lookup on role_grants (scope_kind='deployment').
    # The mock treats the bootstrap operator (Élodie) as admin + owner
    # and the deputy (Marc) as admin-only.
    admin_team = {m.user_id: m for m in md.DEPLOYMENT_ADMINS}
    uid = current_user_id(request)
    admin_row = admin_team.get(uid)
    return ok({
        "role": current_role(request),
        "theme": current_theme(request),
        "agent_sidebar_collapsed": request.cookies.get(AGENT_COLLAPSED_COOKIE) == "1",
        "employee": emp,
        "manager_name": md.DEFAULT_MANAGER_NAME,
        "today": md.TODAY,
        "now": md.NOW,
        "user_id": me_user.id if me_user else None,
        "agent_approval_mode": me_user.agent_approval_mode if me_user else "strict",
        "current_workspace_id": wsid,
        "available_workspaces": available,
        "client_binding_org_ids": binding_org_ids,
        "is_deployment_admin": admin_row is not None,
        "is_deployment_owner": bool(admin_row and admin_row.is_owner),
    })


@app.get("/api/v1/me/agent_approval_mode")
def api_me_agent_approval_mode(request: Request) -> Response:
    user = md.user_by_id(current_user_id(request))
    if user is None:
        return ok({"error": "not_found"}, status_code=404)
    return ok({"mode": user.agent_approval_mode})


@app.put("/api/v1/me/agent_approval_mode")
async def api_me_agent_approval_mode_set(request: Request) -> Response:
    user = md.user_by_id(current_user_id(request))
    if user is None:
        return ok({"error": "not_found"}, status_code=404)
    body = await request.json()
    new_mode = (body or {}).get("mode")
    if new_mode not in {"bypass", "auto", "strict"}:
        return ok({"error": "invalid_mode", "allowed": ["bypass", "auto", "strict"]}, status_code=400)
    old_mode = user.agent_approval_mode
    user.agent_approval_mode = new_mode
    hub.publish(
        "auth.agent_mode_changed",
        {"user_id": user.id, "old_mode": old_mode, "new_mode": new_mode},
    )
    return ok({"mode": user.agent_approval_mode})


# ── Avatar (§12 POST/DELETE /me/avatar, GET /files/{id}/blob) ────────

AVATAR_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic"}
AVATAR_MAX_BYTES = 10 * 1024 * 1024  # §15 images default


@app.post("/api/v1/me/avatar")
async def api_me_avatar_set(request: Request) -> Response:
    """Accept a cropped image from the /me editor (§14) and store it.

    The production server re-encodes to 512×512 WebP with EXIF stripped
    (§15). The mock trusts the client-side crop, keeps the submitted
    bytes as-is, and surfaces them via `/api/v1/files/{id}/blob`.
    """
    user = md.user_by_id(current_user_id(request))
    if user is None:
        return ok({"error": "not_found"}, status_code=404)

    form = await request.form()
    image = form.get("image")
    if image is None or not hasattr(image, "read"):
        return ok({"error": "image_required"}, status_code=422)

    mime = (getattr(image, "content_type", None) or "").lower()
    if mime not in AVATAR_MIME_TYPES:
        return ok(
            {"error": "unsupported_mime", "allowed": sorted(AVATAR_MIME_TYPES)},
            status_code=415,
        )

    data = await image.read()
    if len(data) > AVATAR_MAX_BYTES:
        return ok({"error": "too_large", "max_bytes": AVATAR_MAX_BYTES}, status_code=413)

    before_file_id = md.set_user_avatar(user.id, data, mime)
    hub.publish(
        "user.avatar_changed",
        {
            "user_id": user.id,
            "before_file_id": before_file_id,
            "after_file_id": user.avatar_file_id,
        },
    )
    return ok({"user": user, "avatar_url": md.avatar_url_for_file_id(user.avatar_file_id)})


@app.delete("/api/v1/me/avatar")
def api_me_avatar_clear(request: Request) -> Response:
    user = md.user_by_id(current_user_id(request))
    if user is None:
        return ok({"error": "not_found"}, status_code=404)
    before_file_id = md.clear_user_avatar(user.id)
    hub.publish(
        "user.avatar_changed",
        {"user_id": user.id, "before_file_id": before_file_id, "after_file_id": None},
    )
    return ok({"user": user, "avatar_url": None})


@app.get("/api/v1/files/{file_id}/blob")
def api_file_blob(file_id: str) -> Response:
    """Serve stored file bytes (mock scope: avatars only)."""
    entry = md.AVATAR_BYTES.get(file_id)
    if entry is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    data, mime = entry
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=60"},
    )


# ── Agent preferences (§11) ──────────────────────────────────────────

import re as _re  # noqa: E402  -- local to the feature

AGENT_PREF_SOFT_CAP_TOKENS = 4000
AGENT_PREF_HARD_CAP_TOKENS = 16000
_PREF_SECRET_REGEXES = [(_re.compile(p), label) for p, label in md.AGENT_PREFERENCE_SECRET_PATTERNS]


def _count_tokens(body: str) -> int:
    """Mock tokeniser: ~= 1 token per 3 chars (close enough for UI counter)."""
    return (len(body) + 2) // 3


def _scan_pref_for_secrets(body: str) -> dict[str, Any] | None:
    for regex, label in _PREF_SECRET_REGEXES:
        m = regex.search(body)
        if m:
            return {
                "error": "preference_contains_secret",
                "pattern": label,
                "span": [m.start(), m.end()],
                "excerpt": body[max(0, m.start() - 12):m.end() + 12],
            }
    return None


def _pref_writable(request: Request, scope_kind: str, scope_id: str) -> bool:
    """Mock action-catalog resolver for `agent_prefs.edit_*`.

    The real API consults `permission_rule` + the §05 catalog; here we
    fake it: managers pass workspace/property edit; any user may edit
    their own row.
    """
    role = current_role(request)
    uid = current_user_id(request)
    if scope_kind == "user":
        return scope_id == uid
    if scope_kind in ("workspace", "property"):
        return role == "manager"
    return False


def _pref_envelope(scope_kind: str, scope_id: str, request: Request) -> dict[str, Any]:
    row = md.AGENT_PREFERENCES.get((scope_kind, scope_id))
    writable = _pref_writable(request, scope_kind, scope_id)
    if row is None:
        return {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "body_md": "",
            "token_count": 0,
            "updated_by_user_id": None,
            "updated_at": None,
            "writable": writable,
            "soft_cap": AGENT_PREF_SOFT_CAP_TOKENS,
            "hard_cap": AGENT_PREF_HARD_CAP_TOKENS,
        }
    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "body_md": row["body_md"],
        "token_count": row["token_count"],
        "updated_by_user_id": row["updated_by_user_id"],
        "updated_at": row["updated_at"],
        "writable": writable,
        "soft_cap": AGENT_PREF_SOFT_CAP_TOKENS,
        "hard_cap": AGENT_PREF_HARD_CAP_TOKENS,
    }


@app.get("/api/v1/agent_preferences/workspace")
def api_agent_prefs_workspace(request: Request) -> Response:
    return ok(_pref_envelope("workspace", md.DEFAULT_WORKSPACE_ID, request))


@app.put("/api/v1/agent_preferences/workspace")
async def api_agent_prefs_workspace_set(request: Request) -> Response:
    return await _save_pref(request, "workspace", md.DEFAULT_WORKSPACE_ID)


@app.get("/api/v1/agent_preferences/property/{pid}")
def api_agent_prefs_property(pid: str, request: Request) -> Response:
    md.property_by_id(pid)  # 404s via StopIteration if missing
    return ok(_pref_envelope("property", pid, request))


@app.put("/api/v1/agent_preferences/property/{pid}")
async def api_agent_prefs_property_set(pid: str, request: Request) -> Response:
    md.property_by_id(pid)
    return await _save_pref(request, "property", pid)


@app.get("/api/v1/agent_preferences/me")
def api_agent_prefs_me(request: Request) -> Response:
    uid = current_user_id(request)
    return ok(_pref_envelope("user", uid, request))


@app.put("/api/v1/agent_preferences/me")
async def api_agent_prefs_me_set(request: Request) -> Response:
    uid = current_user_id(request)
    return await _save_pref(request, "user", uid)


@app.get("/api/v1/agent_preferences/revisions/{scope_kind}/{scope_id}")
def api_agent_prefs_revisions(scope_kind: str, scope_id: str, request: Request) -> Response:
    if scope_kind == "user" and scope_id != current_user_id(request):
        return ok({"error": "not_found"}, status_code=404)
    revs = md.AGENT_PREFERENCE_REVISIONS.get((scope_kind, scope_id), [])
    return ok({"scope_kind": scope_kind, "scope_id": scope_id, "revisions": revs})


async def _save_pref(request: Request, scope_kind: str, scope_id: str) -> Response:
    if not _pref_writable(request, scope_kind, scope_id):
        return ok({"error": "forbidden", "required_action": f"agent_prefs.edit_{scope_kind}"}, status_code=403)
    body = await request.json()
    new_body = (body or {}).get("body_md", "")
    if not isinstance(new_body, str):
        return ok({"error": "invalid_body"}, status_code=400)
    tokens = _count_tokens(new_body)
    if tokens > AGENT_PREF_HARD_CAP_TOKENS:
        return ok({"error": "preference_too_large", "token_count": tokens,
                   "hard_cap": AGENT_PREF_HARD_CAP_TOKENS}, status_code=422)
    secret = _scan_pref_for_secrets(new_body)
    if secret:
        return ok(secret, status_code=422)
    note = (body or {}).get("save_note")
    uid = current_user_id(request)
    now_iso = md.NOW.isoformat() + "Z"
    existing = md.AGENT_PREFERENCES.get((scope_kind, scope_id))
    md.AGENT_PREFERENCES[(scope_kind, scope_id)] = {
        "body_md": new_body,
        "token_count": tokens,
        "updated_by_user_id": uid,
        "updated_at": now_iso,
    }
    revs = md.AGENT_PREFERENCE_REVISIONS.setdefault((scope_kind, scope_id), [])
    next_rev = (revs[-1]["revision_number"] + 1) if revs else 1
    revs.append({
        "revision_number": next_rev,
        "body_md": new_body,
        "saved_by_user_id": uid,
        "saved_at": now_iso,
        "save_note": note,
    })
    hub.publish(
        "agent_preference.updated",
        {"scope_kind": scope_kind, "scope_id": scope_id,
         "revision_number": next_rev, "token_count": tokens,
         "was_empty": existing is None or not existing.get("body_md")},
    )
    return ok(_pref_envelope(scope_kind, scope_id, request))


@app.get("/api/v1/properties")
def api_properties(request: Request, workspace_id: str = "") -> Response:
    """List properties visible from the active (or requested) workspace.

    Multi-belonging: a property is "visible" from a workspace iff a
    `property_workspace` row exists linking the two (§02). The
    `?workspace_id=` query param overrides the cookie context for
    cross-workspace queries (e.g. an agency dashboard listing a
    client's portfolio).
    """
    wsid = workspace_id or current_workspace_id(request)
    if wsid:
        return ok(md.properties_for_workspace(wsid))
    return ok(md.PROPERTIES)


@app.get("/api/v1/properties/{pid}")
def api_property(pid: str, request: Request) -> Response:
    prop = md.property_by_id(pid)
    if prop is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    memberships = md.workspaces_for_property(pid)
    client_org = md.organization_by_id(prop.client_org_id) if prop.client_org_id else None
    workspaces = [md.workspace_by_id(m.workspace_id) for m in memberships]
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
        # §02 + §22 — multi-belonging surface.
        "memberships": memberships,
        "membership_workspaces": [w for w in workspaces if w is not None],
        "client_org": client_org,
        "owner_user": md.user_by_id(prop.owner_user_id) if prop.owner_user_id else None,
        "active_workspace_id": current_workspace_id(request),
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
        "subject_bookings": md.bookings_for_employee(eid),
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
def api_organizations(request: Request, workspace_id: str = "") -> Response:
    wsid = workspace_id or current_workspace_id(request)
    rows = [o for o in md.ORGANIZATIONS if not wsid or o.workspace_id == wsid]
    return ok(rows)


@app.get("/api/v1/organizations/{oid}")
def api_organization(oid: str) -> Response:
    org = md.organization_by_id(oid)
    if org is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    properties_billed = [p for p in md.PROPERTIES if p.client_org_id == oid]
    rates = [r for r in md.CLIENT_RATES if r.client_org_id == oid]
    user_rates = [r for r in md.CLIENT_USER_RATES if r.client_org_id == oid]
    billings = [b for b in md.BOOKING_BILLINGS if b.client_org_id == oid]
    invoices_to = [i for i in md.VENDOR_INVOICES
                   if i.vendor_organization_id == oid
                   or (i.work_order_id and md.work_order_by_id(i.work_order_id)
                       and md.work_order_by_id(i.work_order_id).client_org_id == oid)]
    invoices_from = [i for i in md.VENDOR_INVOICES if i.vendor_organization_id == oid]
    portal_user = md.user_by_id(org.portal_user_id) if org.portal_user_id else None
    return ok({
        "organization": org,
        "properties_billed": properties_billed,
        "client_rates": rates,
        "client_user_rates": user_rates,
        "recent_booking_billings": billings[-10:],
        "vendor_invoices_billed_to": invoices_to,
        "vendor_invoices_billed_from": invoices_from,
        "portal_user": portal_user,
    })


@app.get("/api/v1/role_grants")
def api_role_grants(
    request: Request,
    user_id: str = "",
    workspace_id: str = "",
    binding_org_id: str = "",
    grant_role: str = "",
) -> Response:
    rows = md.ROLE_GRANTS
    if user_id:
        rows = [r for r in rows if r.user_id == user_id]
    if workspace_id:
        rows = [r for r in rows if r.scope_kind == "workspace" and r.scope_id == workspace_id]
    if binding_org_id:
        rows = [r for r in rows if r.binding_org_id == binding_org_id]
    if grant_role:
        rows = [r for r in rows if r.grant_role == grant_role]
    rows = [r for r in rows if r.revoked_at is None]
    return ok(rows)


@app.get("/api/v1/client_rates")
def api_client_rates(client_org_id: str = "") -> Response:
    rows = md.CLIENT_RATES
    if client_org_id:
        rows = [r for r in rows if r.client_org_id == client_org_id]
    return ok(rows)


@app.get("/api/v1/client_user_rates")
def api_client_user_rates(client_org_id: str = "") -> Response:
    rows = md.CLIENT_USER_RATES
    if client_org_id:
        rows = [r for r in rows if r.client_org_id == client_org_id]
    return ok(rows)


@app.get("/api/v1/booking_billings")
def api_booking_billings(
    client_org_id: str = "",
    user_id: str = "",
    work_engagement_id: str = "",
) -> Response:
    rows = md.BOOKING_BILLINGS
    if client_org_id:
        rows = [r for r in rows if r.client_org_id == client_org_id]
    if user_id:
        rows = [r for r in rows if r.user_id == user_id]
    if work_engagement_id:
        rows = [r for r in rows if r.work_engagement_id == work_engagement_id]
    return ok(rows)


@app.get("/api/v1/work_orders")
def api_work_orders(
    request: Request,
    workspace_id: str = "",
    property_id: str = "",
    client_org_id: str = "",
) -> Response:
    wsid = workspace_id or current_workspace_id(request)
    rows = list(md.WORK_ORDERS)
    if wsid:
        # A work_order is "in" a workspace iff its property is linked
        # to that workspace via property_workspace (§02 multi-belonging).
        ws_props = {pw.property_id for pw in md.PROPERTY_WORKSPACES if pw.workspace_id == wsid}
        rows = [r for r in rows if r.property_id in ws_props]
    if property_id:
        rows = [r for r in rows if r.property_id == property_id]
    if client_org_id:
        rows = [r for r in rows if r.client_org_id == client_org_id]
    return ok(rows)


@app.get("/api/v1/work_orders/{woid}")
def api_work_order(woid: str) -> Response:
    wo = md.work_order_by_id(woid)
    if wo is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    quotes = [q for q in md.QUOTES if q.work_order_id == woid]
    invoices = [i for i in md.VENDOR_INVOICES if i.work_order_id == woid]
    return ok({
        "work_order": wo,
        "property": md.property_by_id(wo.property_id),
        "client_org": md.organization_by_id(wo.client_org_id) if wo.client_org_id else None,
        "quotes": quotes,
        "vendor_invoices": invoices,
    })


@app.get("/api/v1/quotes")
def api_quotes(work_order_id: str = "") -> Response:
    rows = md.QUOTES
    if work_order_id:
        rows = [q for q in rows if q.work_order_id == work_order_id]
    return ok(rows)


@app.get("/api/v1/vendor_invoices")
def api_vendor_invoices(
    request: Request,
    workspace_id: str = "",
    property_id: str = "",
    client_org_id: str = "",
    vendor_organization_id: str = "",
    vendor_user_id: str = "",
) -> Response:
    wsid = workspace_id or current_workspace_id(request)
    rows = list(md.VENDOR_INVOICES)
    if wsid:
        ws_props = {pw.property_id for pw in md.PROPERTY_WORKSPACES if pw.workspace_id == wsid}
        ws_work_orders = {w.id for w in md.WORK_ORDERS if w.property_id in ws_props}
        rows = [
            r for r in rows
            if (r.property_id and r.property_id in ws_props)
            or (r.work_order_id and r.work_order_id in ws_work_orders)
        ]
    if property_id:
        rows = [r for r in rows if r.property_id == property_id
                or (r.work_order_id
                    and md.work_order_by_id(r.work_order_id) is not None
                    and md.work_order_by_id(r.work_order_id).property_id == property_id)]
    if client_org_id:
        rows = [r for r in rows
                if (r.work_order_id and md.work_order_by_id(r.work_order_id) is not None
                    and md.work_order_by_id(r.work_order_id).client_org_id == client_org_id)
                or (r.property_id and md.property_by_id(r.property_id) is not None
                    and md.property_by_id(r.property_id).client_org_id == client_org_id)]
    if vendor_organization_id:
        rows = [r for r in rows if r.vendor_organization_id == vendor_organization_id]
    if vendor_user_id:
        rows = [r for r in rows if r.vendor_user_id == vendor_user_id]
    return ok(rows)


# ── Multi-belonging mutations (stubbed — §22 share / revoke) ─────────
# These are not the canonical wire shape; they exist so the mock UI
# can demonstrate the "client invites agency" / "client switches
# agency" flow. The production routes live under
# `POST /properties/{id}/share` (§04 + §22) and gate through the
# always-approval set on transfer.

@app.post("/api/v1/property_workspaces/share")
async def api_property_share(request: Request) -> Response:
    body = await request.json()
    pid = str(body.get("property_id") or "")
    wsid = str(body.get("workspace_id") or "")
    role = str(body.get("membership_role") or "managed_workspace")
    if role not in {"managed_workspace", "observer_workspace"}:
        return JSONResponse({"detail": "invalid_membership_role"}, status_code=422)
    if md.property_by_id(pid) is None or md.workspace_by_id(wsid) is None:
        return JSONResponse({"detail": "unknown_property_or_workspace"}, status_code=404)
    existing = next((pw for pw in md.PROPERTY_WORKSPACES
                     if pw.property_id == pid and pw.workspace_id == wsid), None)
    if existing is not None:
        existing.membership_role = role  # idempotent re-share
        row = existing
    else:
        row = md.PropertyWorkspace(
            property_id=pid, workspace_id=wsid, membership_role=role,
            added_by_user_id=current_user_id(request),
        )
        md.PROPERTY_WORKSPACES.append(row)
    hub.publish("property_workspace.shared", {"property_id": pid, "workspace_id": wsid, "membership_role": role})
    return ok(row)


@app.post("/api/v1/property_workspaces/revoke")
async def api_property_revoke(request: Request) -> Response:
    body = await request.json()
    pid = str(body.get("property_id") or "")
    wsid = str(body.get("workspace_id") or "")
    rows = [pw for pw in md.PROPERTY_WORKSPACES
            if pw.property_id == pid and pw.workspace_id == wsid]
    if not rows:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    row = rows[0]
    if row.membership_role == "owner_workspace":
        return JSONResponse({"detail": "cannot_revoke_owner_workspace"}, status_code=409)
    md.PROPERTY_WORKSPACES.remove(row)
    hub.publish("property_workspace.revoked", {"property_id": pid, "workspace_id": wsid})
    return ok({"ok": True})


# §22 property_workspace_invite — two-sided invite/accept flow. The
# owner workspace creates an invite; the target workspace's owners
# accept via the token URL. Only on acceptance is the
# `property_workspace` junction row materialised.

@app.get("/api/v1/property_workspace_invites")
def api_property_workspace_invites(
    request: Request,
    workspace_id: str = "",
    property_id: str = "",
    state: str = "",
    direction: str = "out",
) -> Response:
    """List invites from / to the current workspace.

    `direction = out` (default) returns invites this workspace
    originated. `direction = in` returns invites addressed to this
    workspace (pending decisions). `direction = any` returns both.
    """
    wsid = workspace_id or current_workspace_id(request)
    rows = list(md.PROPERTY_WORKSPACE_INVITES)
    if wsid and direction == "out":
        rows = [r for r in rows if r.from_workspace_id == wsid]
    elif wsid and direction == "in":
        rows = [r for r in rows if r.to_workspace_id == wsid]
    elif wsid and direction == "any":
        rows = [r for r in rows
                if r.from_workspace_id == wsid or r.to_workspace_id == wsid]
    if property_id:
        rows = [r for r in rows if r.property_id == property_id]
    if state:
        rows = [r for r in rows if r.state == state]
    return ok(rows)


@app.post("/api/v1/property_workspace_invites")
async def api_property_workspace_invite_create(request: Request) -> Response:
    body = await request.json()
    pid = str(body.get("property_id") or "")
    from_wsid = str(body.get("from_workspace_id") or current_workspace_id(request) or "")
    to_wsid = body.get("to_workspace_id")
    role = str(body.get("proposed_membership_role") or "managed_workspace")
    share_guest_identity = bool(body.get("share_guest_identity") or False)
    if role not in {"managed_workspace", "observer_workspace"}:
        return JSONResponse({"detail": "invalid_membership_role"}, status_code=422)
    if md.property_by_id(pid) is None:
        return JSONResponse({"detail": "unknown_property"}, status_code=404)
    owner_link = next((pw for pw in md.PROPERTY_WORKSPACES
                       if pw.property_id == pid
                       and pw.workspace_id == from_wsid
                       and pw.membership_role == "owner_workspace"), None)
    if owner_link is None:
        return JSONResponse({"detail": "not_owner_workspace"}, status_code=403)
    import secrets as _secrets
    invite = md.PropertyWorkspaceInvite(
        id=f"pwi-{_secrets.token_hex(6)}",
        token=f"pwi_{_secrets.token_urlsafe(22)}",
        from_workspace_id=from_wsid,
        property_id=pid,
        to_workspace_id=(str(to_wsid) if to_wsid else None),
        proposed_membership_role=role,
        initial_share_settings={"share_guest_identity": share_guest_identity},
        state="pending",
        created_by_user_id=current_user_id(request) or "u-elodie",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    md.PROPERTY_WORKSPACE_INVITES.append(invite)
    hub.publish("property_workspace_invite.created",
                {"invite_id": invite.id, "property_id": pid,
                 "from_workspace_id": from_wsid, "to_workspace_id": to_wsid})
    return ok(invite)


def _invite_by_token_or_id(ident: str):
    for inv in md.PROPERTY_WORKSPACE_INVITES:
        if inv.id == ident or inv.token == ident:
            return inv
    return None


@app.get("/api/v1/property_workspace_invites/{ident}")
def api_property_workspace_invite_get(ident: str) -> Response:
    inv = _invite_by_token_or_id(ident)
    if inv is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    prop = md.property_by_id(inv.property_id)
    from_ws = md.workspace_by_id(inv.from_workspace_id)
    to_ws = md.workspace_by_id(inv.to_workspace_id) if inv.to_workspace_id else None
    return ok({
        "invite": inv,
        "property": prop,
        "from_workspace": from_ws,
        "to_workspace": to_ws,
    })


@app.post("/api/v1/property_workspace_invites/{ident}/accept")
async def api_property_workspace_invite_accept(ident: str, request: Request) -> Response:
    inv = _invite_by_token_or_id(ident)
    if inv is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    if inv.state != "pending":
        return JSONResponse({"detail": f"invite_{inv.state}"}, status_code=409)
    body = await request.json() if request.headers.get("content-length") else {}
    accepting_wsid = str(body.get("accepting_workspace_id") or current_workspace_id(request) or "")
    if inv.to_workspace_id and inv.to_workspace_id != accepting_wsid:
        return JSONResponse({"detail": "workspace_not_addressed"}, status_code=403)
    if md.workspace_by_id(accepting_wsid) is None:
        return JSONResponse({"detail": "unknown_accepting_workspace"}, status_code=404)
    # Collide with any existing row.
    dup = next((pw for pw in md.PROPERTY_WORKSPACES
                if pw.property_id == inv.property_id and pw.workspace_id == accepting_wsid), None)
    if dup is not None:
        return JSONResponse({"detail": "already_linked"}, status_code=409)
    inv.state = "accepted"
    inv.decided_at = datetime.now(timezone.utc)
    inv.decided_by_user_id = current_user_id(request) or "u-vincent"
    row = md.PropertyWorkspace(
        property_id=inv.property_id,
        workspace_id=accepting_wsid,
        membership_role=inv.proposed_membership_role,
        share_guest_identity=bool(inv.initial_share_settings.get("share_guest_identity", False)),
        invite_id=inv.id,
        added_by_user_id=current_user_id(request),
        added_via="invite_accept",
    )
    md.PROPERTY_WORKSPACES.append(row)
    hub.publish("property_workspace_invite.accepted",
                {"invite_id": inv.id, "property_id": inv.property_id,
                 "workspace_id": accepting_wsid})
    hub.publish("property_workspace.shared",
                {"property_id": inv.property_id, "workspace_id": accepting_wsid,
                 "membership_role": inv.proposed_membership_role})
    return ok({"invite": inv, "property_workspace": row})


@app.post("/api/v1/property_workspace_invites/{ident}/reject")
async def api_property_workspace_invite_reject(ident: str, request: Request) -> Response:
    inv = _invite_by_token_or_id(ident)
    if inv is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    if inv.state != "pending":
        return JSONResponse({"detail": f"invite_{inv.state}"}, status_code=409)
    body = await request.json() if request.headers.get("content-length") else {}
    inv.state = "rejected"
    inv.decided_at = datetime.now(timezone.utc)
    inv.decided_by_user_id = current_user_id(request)
    inv.decision_note_md = body.get("note_md")
    hub.publish("property_workspace_invite.rejected",
                {"invite_id": inv.id, "property_id": inv.property_id})
    return ok(inv)


@app.post("/api/v1/property_workspace_invites/{ident}/revoke")
async def api_property_workspace_invite_revoke(ident: str, request: Request) -> Response:
    inv = _invite_by_token_or_id(ident)
    if inv is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    if inv.state != "pending":
        return JSONResponse({"detail": f"invite_{inv.state}"}, status_code=409)
    inv.state = "revoked"
    inv.decided_at = datetime.now(timezone.utc)
    inv.decided_by_user_id = current_user_id(request)
    hub.publish("property_workspace_invite.revoked",
                {"invite_id": inv.id, "property_id": inv.property_id})
    return ok(inv)


# §22 vendor_invoice proof-of-payment upload. The client (or the
# billing workspace's owner/manager) appends file ids to
# `proof_of_payment_file_ids`. The mock accepts ids without actually
# storing files — the real backend would run this through the §02
# `file` table.

@app.post("/api/v1/vendor_invoices/{vid}/proof")
async def api_vendor_invoice_upload_proof(vid: str, request: Request) -> Response:
    inv = next((v for v in md.VENDOR_INVOICES if v.id == vid), None)
    if inv is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    body = await request.json() if request.headers.get("content-length") else {}
    file_ids = body.get("file_ids") or [f"file-proof-{vid}-{len(inv.proof_of_payment_file_ids) + 1}"]
    for fid in file_ids:
        if fid not in inv.proof_of_payment_file_ids:
            inv.proof_of_payment_file_ids.append(str(fid))
    hub.publish("vendor_invoice.proof_uploaded",
                {"vendor_invoice_id": inv.id, "file_ids": file_ids})
    return ok(inv)


@app.delete("/api/v1/vendor_invoices/{vid}/proof/{fid}")
def api_vendor_invoice_remove_proof(vid: str, fid: str) -> Response:
    inv = next((v for v in md.VENDOR_INVOICES if v.id == vid), None)
    if inv is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    if fid not in inv.proof_of_payment_file_ids:
        return JSONResponse({"detail": "file_not_attached"}, status_code=404)
    inv.proof_of_payment_file_ids.remove(fid)
    hub.publish("vendor_invoice.proof_removed",
                {"vendor_invoice_id": inv.id, "file_id": fid})
    return ok(inv)


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
def api_tasks(request: Request) -> Response:
    uid = current_user_id(request)
    return ok([t for t in md.TASKS if md.visible_to(t, uid)])


@app.get("/api/v1/tasks/{tid}")
def api_task(tid: str, request: Request) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not md.visible_to(task, current_user_id(request)):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok({
        "task": task,
        "property": md.property_by_id(task.property_id) if task.property_id else None,
        "instructions": md.instructions_for_task(task),
        "comments": md.comments_for_task(tid),
    })


@app.get("/api/v1/today")
def api_today(request: Request) -> Response:
    uid = current_user_id(request)
    tasks = sorted(md.tasks_for_user(uid), key=lambda t: t.scheduled_start)
    today_tasks = [
        t for t in tasks
        if t.scheduled_start.date() == md.TODAY and md.visible_to(t, uid)
    ]
    now_task = next((t for t in today_tasks if t.status in {"pending", "in_progress"}), None)
    upcoming = [t for t in today_tasks if t is not now_task and t.status in {"pending", "in_progress"}]
    completed = [t for t in today_tasks if t.status == "completed"]
    return ok({"now_task": now_task, "upcoming": upcoming, "completed": completed,
               "properties": md.PROPERTIES})


@app.get("/api/v1/week")
def api_week(request: Request) -> Response:
    uid = current_user_id(request)
    return ok({
        "tasks": sorted(
            [t for t in md.tasks_for_user(uid) if md.visible_to(t, uid)],
            key=lambda t: t.scheduled_start,
        ),
        "properties": md.PROPERTIES,
    })


@app.get("/api/v1/dashboard")
def api_dashboard(request: Request) -> Response:
    uid = current_user_id(request)
    # "Active right now" = workers with a `scheduled` booking whose
    # window contains md.NOW. With the booking model there is no
    # clock-in event; presence is derived from the schedule.
    now = md.NOW
    active_emp_ids = {
        b.employee_id for b in md.BOOKINGS
        if b.status == "scheduled"
        and b.scheduled_start <= now <= b.scheduled_end
    }
    on_booking = [e for e in md.EMPLOYEES if e.id in active_emp_ids]
    today_tasks = [
        t for t in md.TASKS
        if t.scheduled_start.date() == md.TODAY and md.visible_to(t, uid)
    ]
    by_status = {
        "completed":   [t for t in today_tasks if t.status == "completed"],
        "in_progress": [t for t in today_tasks if t.status == "in_progress"],
        "pending":     [t for t in today_tasks if t.status == "pending"],
    }
    return ok({
        "on_booking": on_booking,
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
def api_expenses(request: Request, mine: bool = False) -> Response:
    if mine:
        return ok(md.expenses_for_user(current_user_id(request)))
    return ok(md.EXPENSES)


@app.get("/api/v1/expenses/pending_reimbursement")
def api_expenses_pending_reimbursement(
    request: Request, user_id: str | None = None
) -> Response:
    """Approved-but-not-yet-reimbursed totals grouped by ``owed_currency``.

    Per §09 "Amount owed to the employee" this is the authoritative
    "what do we owe this employee right now?" endpoint.

    - ``user_id=me`` → current worker (what the employee widget calls).
    - ``user_id=<uid>`` → that employee's pending totals.
    - no ``user_id`` → workspace-wide aggregate; the response includes a
      ``by_user`` breakdown for the manager Pay page.
    """

    if user_id == "me":
        uid: str | None = current_user_id(request)
    else:
        uid = user_id or None

    scope = md.expenses_for_user(uid) if uid else md.EXPENSES
    pending = [x for x in scope if x.status == "approved"]
    totals: dict[str, int] = {}
    per_user: dict[str, dict[str, int]] = {}
    for x in pending:
        ccy = x.owed_currency or x.currency
        cents = x.owed_amount_cents if x.owed_amount_cents is not None else x.amount_cents
        totals[ccy] = totals.get(ccy, 0) + cents
        if uid is None:
            key = x.user_id or x.employee_id
            per_user.setdefault(key, {})
            per_user[key][ccy] = per_user[key].get(ccy, 0) + cents

    payload: dict[str, object] = {
        "user_id": uid,
        "claims": pending,
        "totals_by_currency": [
            {"currency": ccy, "amount_cents": cents}
            for ccy, cents in sorted(totals.items())
        ],
    }
    if uid is None:
        payload["by_user"] = [
            {
                "user_id": uid_key,
                "employee_id": next(
                    (
                        x.employee_id
                        for x in pending
                        if (x.user_id or x.employee_id) == uid_key
                    ),
                    uid_key,
                ),
                "totals_by_currency": [
                    {"currency": ccy, "amount_cents": cents}
                    for ccy, cents in sorted(per_user[uid_key].items())
                ],
            }
            for uid_key in sorted(per_user.keys())
        ]
    return ok(payload)


@app.get("/api/v1/exchange_rates")
def api_exchange_rates(
    as_of: str | None = None,
    quote: str | None = None,
    source: str | None = None,
) -> Response:
    """§09 "Exchange rates service" — list FX rates in the demo
    workspace. Filters are optional; ``as_of`` narrows to a single
    date (YYYY-MM-DD), ``quote`` to a currency code, ``source`` to
    ``ecb | manual | stale_carryover``.
    """

    rows = list(md.EXCHANGE_RATES)
    if as_of:
        try:
            target = date.fromisoformat(as_of)
        except ValueError:
            return JSONResponse({"detail": "bad as_of"}, status_code=422)
        rows = [r for r in rows if r.as_of_date == target]
    if quote:
        rows = [r for r in rows if r.quote.upper() == quote.upper()]
    if source:
        rows = [r for r in rows if r.source == source]
    return ok(rows)


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


# ── Scheduler: rulesets + calendar feed (§06, §12, §14) ──────────────

@app.get("/api/v1/schedule_rulesets")
def api_schedule_rulesets(request: Request) -> Response:
    """List rulesets + slots + assignments for the active workspace."""
    ws_id = current_workspace_id(request)
    rulesets = [r for r in md.SCHEDULE_RULESETS if r.workspace_id == ws_id]
    ruleset_ids = {r.id for r in rulesets}
    slots = [s for s in md.SCHEDULE_RULESET_SLOTS if s.schedule_ruleset_id in ruleset_ids]
    assignments = md.assignments_for_workspace(ws_id)
    return ok({
        "rulesets": rulesets,
        "slots": slots,
        "assignments": [
            {
                "id": a.id,
                "user_work_role_id": a.user_work_role_id,
                "property_id": a.property_id,
                "schedule_ruleset_id": a.schedule_ruleset_id,
                "user_id": md.user_id_for_uwr(a.user_work_role_id),
                "work_role_id": md.work_role_id_for_uwr(a.user_work_role_id),
            }
            for a in assignments
        ],
    })


def _client_bound_property_ids(uid: str, ws_id: str) -> set[str]:
    """Properties visible to a client user on the given workspace per §22.

    A client grant with `binding_org_id` sees every property whose
    `client_org_id` matches. A property-scoped client grant sees only
    that one property.
    """
    pids: set[str] = set()
    for g in md.ROLE_GRANTS:
        if (
            g.user_id != uid
            or g.grant_role != "client"
            or g.revoked_at is not None
        ):
            continue
        if g.scope_kind == "workspace" and g.scope_id == ws_id and g.binding_org_id:
            for p in md.PROPERTIES:
                if getattr(p, "client_org_id", None) == g.binding_org_id:
                    pids.add(p.id)
        elif g.scope_kind == "property":
            pids.add(g.scope_id)
    return pids


def _scheduler_user_view(uid: str, role: str) -> dict[str, Any]:
    """Serialise a user for the scheduler feed.

    Client callers see `first_name` + `work_role` only per §15 "Client
    rota visibility"; manager/worker callers get the regular user shape
    (first + last name, no phone / email — the scheduler never needs
    them).
    """
    u = md.user_by_id(uid)
    if u is None:
        return {"id": uid, "first_name": "", "display_name": ""}
    full = u.display_name or ""
    first = full.split(" ", 1)[0] if full else ""
    if role == "client":
        return {"id": uid, "first_name": first}
    return {"id": uid, "first_name": first, "display_name": full}


@app.get("/api/v1/scheduler/calendar")
def api_scheduler_calendar(request: Request,
                           from_: str | None = None,
                           to: str | None = None) -> Response:
    """Calendar feed: rota slots + tasks + stay bundles for a date range.

    `from`/`to` are ISO dates; defaults are [today, today+14d]. Scoping
    is by caller role (§14, §15): manager/owner get the full workspace
    feed, worker gets self-only, client gets their bound properties and
    first-name-only user rows.
    """
    role = current_role(request)
    ws_id = current_workspace_id(request)
    today = date.today()
    from_d = date.fromisoformat(from_) if from_ else today
    to_d = date.fromisoformat(to) if to else today + timedelta(days=14)

    rulesets = [r for r in md.SCHEDULE_RULESETS if r.workspace_id == ws_id]
    ruleset_ids = {r.id for r in rulesets}
    slots = [s for s in md.SCHEDULE_RULESET_SLOTS if s.schedule_ruleset_id in ruleset_ids]
    assignments = md.assignments_for_workspace(ws_id)

    # Scope by role.
    if role == "employee":
        uid = md.DEFAULT_EMPLOYEE_USER_ID
        uwr_ids = {r.id for r in md.USER_WORK_ROLES if r.user_id == uid and r.workspace_id == ws_id}
        assignments = [a for a in assignments if a.user_work_role_id in uwr_ids]
    elif role == "client":
        uid = md.DEFAULT_CLIENT_USER_ID
        visible_pids = _client_bound_property_ids(uid, ws_id)
        assignments = [a for a in assignments if a.property_id in visible_pids]
    # manager + owner: no narrowing.

    # Tasks overlapping the window, narrowed the same way as assignments.
    pids_in_scope = {a.property_id for a in assignments}
    user_ids_in_scope = {md.user_id_for_uwr(a.user_work_role_id) for a in assignments}
    user_ids_in_scope.discard(None)

    def in_window(t: md.Task) -> bool:
        d = t.scheduled_start.date()
        if d < from_d or d > to_d:
            return False
        if role == "manager" or role == "owner":
            return getattr(t, "workspace_id", ws_id) == ws_id
        if role == "employee":
            return t.assigned_user_id == md.DEFAULT_EMPLOYEE_USER_ID
        if role == "client":
            return t.property_id in pids_in_scope
        return False

    tasks = [t for t in md.TASKS if in_window(t)]
    task_view = [
        {
            "id": t.id,
            "title": t.title,
            "property_id": t.property_id,
            "user_id": t.assigned_user_id,
            "scheduled_start": t.scheduled_start,
            "estimated_minutes": t.estimated_minutes,
            "priority": t.priority,
            "status": t.status,
        }
        for t in tasks
    ]

    # Surface relevant users for rendering (de-duplicated).
    uids = {md.user_id_for_uwr(a.user_work_role_id) for a in assignments}
    uids.update(t.assigned_user_id for t in tasks if t.assigned_user_id)
    uids.discard(None)
    uids.discard("")
    users = [_scheduler_user_view(u, role) for u in sorted(u for u in uids if u)]

    return ok({
        "window": {"from": from_d.isoformat(), "to": to_d.isoformat()},
        "rulesets": rulesets,
        "slots": slots,
        "assignments": [
            {
                "id": a.id,
                "user_id": md.user_id_for_uwr(a.user_work_role_id),
                "work_role_id": md.work_role_id_for_uwr(a.user_work_role_id),
                "property_id": a.property_id,
                "schedule_ruleset_id": a.schedule_ruleset_id,
            }
            for a in assignments
        ],
        "tasks": task_view,
        "users": users,
        "properties": [
            {"id": p.id, "name": p.name, "timezone": getattr(p, "timezone", "Europe/Paris")}
            for p in md.PROPERTIES
            if p.id in {a.property_id for a in assignments} | {t.property_id for t in tasks}
        ],
    })


# ── /schedule self-service surface (§14 "Schedule view") ─────────────

_WEEKLY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekly_slots_for_user(uid: str) -> list[dict[str, Any]]:
    """Serialise the signed-in user's weekly pattern as ISO-weekday slots.

    The seed stores weekly availability on Employee for historical
    reasons; §06 will migrate it to `user_weekly_availability`. The
    response shape here is the target shape so the frontend is
    forward-compatible.
    """
    emp = next((e for e in md.EMPLOYEES if e.user_id == uid), None)
    if emp is None:
        return []
    out: list[dict[str, Any]] = []
    for i, key in enumerate(_WEEKLY_KEYS):
        hours = emp.weekly_availability.get(key)
        if hours:
            out.append({"weekday": i, "starts_local": hours[0], "ends_local": hours[1]})
        else:
            out.append({"weekday": i, "starts_local": None, "ends_local": None})
    return out


def _time_to_minutes(hhmm: str) -> int:
    parts = hhmm.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _override_reduces_hours(
    weekly: list[dict[str, Any]],
    weekday: int,
    available: bool,
    starts_local: str | None,
    ends_local: str | None,
) -> bool:
    """§06 "Approval logic (hybrid model)" — does this override narrow availability?

    Mirrors the table exactly: off → working = auto; working → off = approval;
    reduce hours = approval; extend hours = auto; off → off = auto.
    """
    pattern = next((s for s in weekly if s["weekday"] == weekday), None)
    pat_off = not pattern or pattern["starts_local"] is None
    if not available:
        return not pat_off  # removing a working day → approval
    # Available = True.
    if pat_off:
        return False  # extra day — auto-approved
    # Both have hours — compare widths.
    pat_start = _time_to_minutes(pattern["starts_local"])
    pat_end = _time_to_minutes(pattern["ends_local"])
    if starts_local is None or ends_local is None:
        return False  # inherits pattern hours, same width
    new_start = _time_to_minutes(starts_local)
    new_end = _time_to_minutes(ends_local)
    # Narrower on either edge → approval.
    return new_start > pat_start or new_end < pat_end


@app.get("/api/v1/me/schedule")
def api_me_schedule(
    request: Request,
    from_: str | None = None,
    to: str | None = None,
) -> Response:
    """Self-only calendar feed for `/schedule` (§14).

    Returns rota slots + assigned tasks + approved leaves + overrides
    covering the window. Pending leaves and overrides are returned too,
    flagged by `approved_at IS NULL`, so the UI can render their state
    without having to re-derive it.
    """
    uid = current_user_id(request)
    ws_id = current_workspace_id(request)
    today = date.today()
    from_d = date.fromisoformat(from_) if from_ else today
    to_d = date.fromisoformat(to) if to else today + timedelta(days=13)

    # Rota — same join as /scheduler/calendar but narrowed to self.
    uwr_ids = {r.id for r in md.USER_WORK_ROLES if r.user_id == uid and r.workspace_id == ws_id}
    assignments = [
        a for a in md.assignments_for_workspace(ws_id) if a.user_work_role_id in uwr_ids
    ]
    ruleset_ids = {a.schedule_ruleset_id for a in assignments if a.schedule_ruleset_id}
    rulesets = [r for r in md.SCHEDULE_RULESETS if r.id in ruleset_ids]
    slots = [s for s in md.SCHEDULE_RULESET_SLOTS if s.schedule_ruleset_id in ruleset_ids]
    pids = {a.property_id for a in assignments}

    # Tasks assigned to this user in the window.
    tasks = [
        t for t in md.TASKS
        if t.assigned_user_id == uid
        and from_d <= t.scheduled_start.date() <= to_d
    ]
    task_view = [
        {
            "id": t.id,
            "title": t.title,
            "property_id": t.property_id,
            "user_id": t.assigned_user_id,
            "scheduled_start": t.scheduled_start,
            "estimated_minutes": t.estimated_minutes,
            "priority": t.priority,
            "status": t.status,
        }
        for t in tasks
    ]
    pids = pids | {t.property_id for t in tasks}

    # Leaves + overrides covering the window (any approval state).
    emp = next((e for e in md.EMPLOYEES if e.user_id == uid), None)
    emp_id = emp.id if emp else None
    leaves = [
        lv for lv in md.LEAVES
        if (lv.user_id == uid or lv.employee_id == emp_id)
        and lv.starts_on <= to_d and lv.ends_on >= from_d
    ]
    overrides = [
        ao for ao in md.AVAILABILITY_OVERRIDES
        if ao.user_id == uid and from_d <= ao.date <= to_d
    ]

    return ok({
        "window": {"from": from_d.isoformat(), "to": to_d.isoformat()},
        "user_id": uid,
        "weekly_availability": _weekly_slots_for_user(uid),
        "rulesets": rulesets,
        "slots": slots,
        "assignments": [
            {
                "id": a.id,
                "user_id": md.user_id_for_uwr(a.user_work_role_id),
                "work_role_id": md.work_role_id_for_uwr(a.user_work_role_id),
                "property_id": a.property_id,
                "schedule_ruleset_id": a.schedule_ruleset_id,
            }
            for a in assignments
        ],
        "tasks": task_view,
        "properties": [
            {"id": p.id, "name": p.name, "timezone": getattr(p, "timezone", "Europe/Paris")}
            for p in md.PROPERTIES if p.id in pids
        ],
        "leaves": leaves,
        "overrides": overrides,
    })


@app.post("/api/v1/me/leaves")
async def api_me_leaves_create(request: Request) -> Response:
    """Self-service leave request — always lands pending (§06)."""
    body = await request.json()
    uid = current_user_id(request)
    emp = next((e for e in md.EMPLOYEES if e.user_id == uid), None)
    try:
        starts = date.fromisoformat(body["starts_on"])
        ends = date.fromisoformat(body["ends_on"])
    except (KeyError, ValueError):
        return ok({"error": "invalid_dates"}, status_code=422)
    if ends < starts:
        return ok({"error": "ends_before_starts"}, status_code=422)
    category = body.get("category", "personal")
    if category not in {"vacation", "sick", "personal", "bereavement", "other"}:
        return ok({"error": "invalid_category"}, status_code=422)
    leave = md.Leave(
        id=f"lv-{len(md.LEAVES) + 1}-{uid[-4:]}",
        employee_id=emp.id if emp else "",
        user_id=uid,
        starts_on=starts,
        ends_on=ends,
        category=category,
        note=body.get("note_md", "") or "",
        approved_at=None,
        decided_by_user_id=None,
    )
    md.LEAVES.append(leave)
    hub.publish("user_leave.upserted", {"id": leave.id, "user_id": uid})
    return ok(leave, status_code=201)


@app.get("/api/v1/me/availability_overrides")
def api_me_overrides_list(request: Request) -> Response:
    """Self-only override list for `/me` and `/schedule` (§14).

    Returns every `user_availability_override` owned by the signed-in
    user, any approval state, sorted by date descending. No window —
    `/me` lists them all; `/schedule` narrows to its viewport from the
    richer `/me/schedule` feed.
    """
    uid = current_user_id(request)
    rows = [ao for ao in md.AVAILABILITY_OVERRIDES if ao.user_id == uid]
    rows.sort(key=lambda r: r.date, reverse=True)
    return ok({"overrides": rows})


@app.post("/api/v1/me/availability_overrides")
async def api_me_overrides_create(request: Request) -> Response:
    """Self-service override. Approval required iff it narrows availability (§06)."""
    body = await request.json()
    uid = current_user_id(request)
    ws_id = current_workspace_id(request)
    try:
        d = date.fromisoformat(body["date"])
    except (KeyError, ValueError):
        return ok({"error": "invalid_date"}, status_code=422)
    available = bool(body.get("available", True))
    starts_local = body.get("starts_local")
    ends_local = body.get("ends_local")
    if available and (starts_local is None) != (ends_local is None):
        return ok({"error": "hours_must_be_paired"}, status_code=422)
    if not available:
        starts_local = None
        ends_local = None
    weekly = _weekly_slots_for_user(uid)
    weekday = d.weekday()
    approval_required = _override_reduces_hours(
        weekly, weekday, available, starts_local, ends_local,
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ao = md.AvailabilityOverride(
        id=f"ao-{uid[-6:]}-{d.isoformat()}",
        user_id=uid,
        workspace_id=ws_id,
        date=d,
        available=available,
        starts_local=starts_local,
        ends_local=ends_local,
        reason=(body.get("reason") or None),
        approval_required=approval_required,
        approved_at=None if approval_required else now,
        approved_by=None if approval_required else uid,
        created_at=now,
    )
    # Upsert: one override per (user, date).
    md.AVAILABILITY_OVERRIDES = [
        existing for existing in md.AVAILABILITY_OVERRIDES
        if not (existing.user_id == uid and existing.date == d)
    ]
    md.AVAILABILITY_OVERRIDES.append(ao)
    hub.publish(
        "user_availability_override.upserted",
        {"id": ao.id, "user_id": uid, "date": d.isoformat(),
         "approval_required": approval_required},
    )
    return ok(ao, status_code=201)


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


@app.get("/api/v1/bookings")
def api_bookings(
    user_id: str = "",
    property_id: str = "",
    status: str = "",
    pending_amend: bool = False,
) -> Response:
    rows = list(md.BOOKINGS)
    if user_id:
        rows = [b for b in rows if b.user_id == user_id]
    if property_id:
        rows = [b for b in rows if b.property_id == property_id]
    if status:
        wanted = set(status.split(","))
        rows = [b for b in rows if b.status in wanted]
    if pending_amend:
        rows = [b for b in rows if b.pending_amend_minutes is not None]
    return ok(rows)


@app.get("/api/v1/bookings/{bid}")
def api_booking(bid: str) -> Response:
    bk = next((b for b in md.BOOKINGS if b.id == bid), None)
    if bk is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(bk)


@app.post("/api/v1/bookings/{bid}/amend")
def api_booking_amend(bid: str, payload: dict) -> Response:
    """Mock amend — applies the patch in-memory.

    Real server: enforces `bookings.amend_self` for owner, threshold
    auto-approve for self-amends, manager queue for larger overruns.
    The mock applies whatever is sent and marks `adjusted = True`.
    """
    bk = next((b for b in md.BOOKINGS if b.id == bid), None)
    if bk is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    reason = payload.get("reason", "")
    touched_time = any(
        k in payload for k in ("scheduled_start", "scheduled_end",
                                "actual_minutes", "break_seconds")
    )
    if touched_time and not reason:
        return JSONResponse({"detail": "amend_reason_required"}, status_code=422)
    for k in ("scheduled_start", "scheduled_end"):
        if k in payload and payload[k]:
            setattr(bk, k, datetime.fromisoformat(payload[k]))
    for k in ("actual_minutes", "actual_minutes_paid", "break_seconds", "kind"):
        if k in payload:
            setattr(bk, k, payload[k])
    if touched_time:
        bk.adjusted = True
        bk.adjustment_reason = reason
        if bk.status == "completed":
            bk.status = "adjusted"
    hub.publish("booking.amended", {"booking": bk})
    return ok(bk)


@app.post("/api/v1/bookings/{bid}/decline")
def api_booking_decline(bid: str, payload: dict | None = None) -> Response:
    bk = next((b for b in md.BOOKINGS if b.id == bid), None)
    if bk is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    bk.declined_at = md.NOW
    bk.declined_reason = (payload or {}).get("reason")
    bk.status = "pending_approval"
    bk.work_engagement_id = ""
    hub.publish("booking.declined", {"booking": bk})
    return ok(bk)


@app.post("/api/v1/bookings/{bid}/cancel")
def api_booking_cancel(bid: str, payload: dict) -> Response:
    bk = next((b for b in md.BOOKINGS if b.id == bid), None)
    if bk is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    by = payload.get("by", "client")
    if by == "client":
        bk.status = "cancelled_by_client"
    elif by == "agency":
        bk.status = "cancelled_by_agency"
    else:
        return JSONResponse({"detail": "invalid_by"}, status_code=422)
    bk.notes_md = (bk.notes_md + " " + payload.get("reason", "")).strip()
    hub.publish("booking.cancelled", {"booking": bk})
    return ok(bk)


@app.post("/api/v1/bookings/{bid}/approve")
def api_booking_approve(bid: str) -> Response:
    bk = next((b for b in md.BOOKINGS if b.id == bid), None)
    if bk is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    if bk.pending_amend_minutes is not None:
        bk.actual_minutes_paid = bk.pending_amend_minutes
        bk.actual_minutes = bk.pending_amend_minutes
        bk.adjusted = True
        bk.adjustment_reason = bk.pending_amend_reason
        bk.pending_amend_minutes = None
        bk.pending_amend_reason = None
        if bk.status == "completed":
            bk.status = "adjusted"
    elif bk.status == "pending_approval":
        bk.status = "scheduled" if bk.scheduled_end > md.NOW else "completed"
    hub.publish("booking.approved", {"booking": bk})
    return ok(bk)


@app.post("/api/v1/bookings/{bid}/reject")
def api_booking_reject(bid: str, payload: dict | None = None) -> Response:
    bk = next((b for b in md.BOOKINGS if b.id == bid), None)
    if bk is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    bk.notes_md = (bk.notes_md + " [rejected: "
                   + (payload or {}).get("reason", "no reason given") + "]").strip()
    if bk.status == "pending_approval":
        # mock: simply soft-delete by removing from the list
        md.BOOKINGS.remove(bk)
    else:
        # for amend rejection, just clear pending_*
        bk.pending_amend_minutes = None
        bk.pending_amend_reason = None
    hub.publish("booking.rejected", {"booking": bk})
    return ok(bk)


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


# ── API tokens (§03) ──────────────────────────────────────────────
# The mock treats create / revoke / rotate as presentational-only
# (in-memory mutation). Personal access tokens are split off to the
# /me/tokens path below so the manager /tokens page never shows them.

def _token_by_id(tid: str) -> md.ApiToken | None:
    return next((t for t in md.API_TOKENS if t.id == tid), None)


def _curl_example(token: md.ApiToken, plaintext: str) -> str:
    """Pick the first scope, suggest an endpoint the user would hit next."""
    scope_to_path = {
        "tasks:read": "/api/v1/tasks",
        "tasks:write": "/api/v1/tasks",
        "payroll:read": "/api/v1/payroll/periods",
        "expenses:read": "/api/v1/expenses",
        "stays:read": "/api/v1/stays",
        "me.tasks:read": "/api/v1/me/tasks",
        "me.bookings:read": "/api/v1/me/bookings",
        "me.expenses:read": "/api/v1/me/expenses",
        "me.expenses:write": "/api/v1/me/expenses",
        "me.profile:read": "/api/v1/me",
    }
    path = scope_to_path.get(token.scopes[0] if token.scopes else "", "/api/v1/me") \
        if token.kind != "delegated" else "/api/v1/me"
    host = "https://dev.crew.day"
    return f"curl -sS -H 'Authorization: Bearer {plaintext}' {host}{path}"


@app.get("/api/v1/auth/tokens")
def api_tokens_list(request: Request) -> Response:
    """List workspace tokens (scoped + delegated). PATs excluded."""
    _ = current_user_id(request)  # manager-only in prod; mock is permissive
    rows = [t for t in md.API_TOKENS if t.kind in ("scoped", "delegated")]
    return ok(rows)


@app.post("/api/v1/auth/tokens")
def api_tokens_create(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    uid = current_user_id(request)
    user = md.user_by_id(uid)
    display = user.display_name if user else "(unknown)"
    name = str(payload.get("name") or "unnamed-token").strip()[:80]
    scopes = list(payload.get("scopes") or [])
    delegate = bool(payload.get("delegate"))
    kind: Literal["scoped", "delegated", "personal"] = "delegated" if delegate else "scoped"
    if any(s.startswith("me.") for s in scopes):
        return JSONResponse({"detail": "me_scope_conflict",
                             "message": "Workspace tokens cannot request me:* scopes. "
                                        "Create a personal access token from /me instead."},
                            status_code=422)
    if kind == "scoped" and not scopes:
        return JSONResponse({"detail": "scopes_required"}, status_code=422)
    live = [t for t in md.API_TOKENS if t.kind in ("scoped", "delegated") and t.revoked_at is None]
    if len(live) >= 50:
        return JSONResponse({"detail": "too_many_workspace_tokens"}, status_code=422)
    expires_at = payload.get("expires_at")
    default_days = 30 if kind == "delegated" else 90
    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).replace(tzinfo=None) \
        if expires_at else datetime.now() + timedelta(days=default_days)
    tok = md.ApiToken(
        id=f"tok-{len(md.API_TOKENS) + 1}",
        name=name,
        kind=kind,
        prefix=f"mip_{'0' * 10}{len(md.API_TOKENS) + 1:02d}",
        scopes=[] if kind == "delegated" else scopes,
        created_by_user_id=uid,
        created_by_display=display,
        created_at=datetime.now(),
        expires_at=expires_dt,
        last_used_at=None,
        last_used_ip=None,
        last_used_path=None,
        revoked_at=None,
        note=payload.get("note"),
        ip_allowlist=list(payload.get("ip_allowlist") or []),
    )
    md.API_TOKENS.append(tok)
    md.API_TOKEN_AUDIT.setdefault(tok.id, [])
    plaintext = f"{tok.prefix}_{'x' * 52}"  # opaque 256-bit mock secret
    hub.publish("api_token.created", {"id": tok.id, "kind": tok.kind})
    return ok({"token": tok, "plaintext": plaintext,
               "curl_example": _curl_example(tok, plaintext)}, status_code=201)


@app.post("/api/v1/auth/tokens/{tid}/revoke")
def api_tokens_revoke(tid: str) -> Response:
    tok = _token_by_id(tid)
    if tok is None or tok.kind == "personal":
        return JSONResponse({"detail": "not_found"}, status_code=404)
    if tok.revoked_at is None:
        tok.revoked_at = datetime.now()
        hub.publish("api_token.revoked", {"id": tok.id})
    return ok(tok)


@app.post("/api/v1/auth/tokens/{tid}/rotate")
def api_tokens_rotate(tid: str) -> Response:
    tok = _token_by_id(tid)
    if tok is None or tok.kind == "personal" or tok.revoked_at is not None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    plaintext = f"{tok.prefix}_{'y' * 52}"
    hub.publish("api_token.rotated", {"id": tok.id})
    return ok({"token": tok, "plaintext": plaintext,
               "curl_example": _curl_example(tok, plaintext)})


@app.get("/api/v1/auth/tokens/{tid}/audit")
def api_tokens_audit(tid: str) -> Response:
    tok = _token_by_id(tid)
    if tok is None or tok.kind == "personal":
        return JSONResponse({"detail": "not_found"}, status_code=404)
    return ok(md.API_TOKEN_AUDIT.get(tid, []))


# Personal access tokens — /me surface, `me:*` scopes only.
@app.get("/api/v1/me/tokens")
def api_me_tokens_list(request: Request) -> Response:
    uid = current_user_id(request)
    rows = [t for t in md.API_TOKENS
            if t.kind == "personal" and t.created_by_user_id == uid]
    return ok(rows)


@app.post("/api/v1/me/tokens")
def api_me_tokens_create(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    uid = current_user_id(request)
    user = md.user_by_id(uid)
    display = user.display_name if user else "(unknown)"
    name = str(payload.get("name") or "personal-token").strip()[:80]
    scopes = list(payload.get("scopes") or [])
    if not scopes:
        return JSONResponse({"detail": "scopes_required"}, status_code=422)
    if not all(s.startswith("me.") for s in scopes):
        return JSONResponse({"detail": "me_scope_conflict",
                             "message": "Personal access tokens only accept me:* scopes."},
                            status_code=422)
    own = [t for t in md.API_TOKENS
           if t.kind == "personal" and t.created_by_user_id == uid and t.revoked_at is None]
    if len(own) >= 5:
        return JSONResponse({"detail": "too_many_personal_tokens"}, status_code=422)
    expires_at = payload.get("expires_at")
    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).replace(tzinfo=None) \
        if expires_at else datetime.now() + timedelta(days=90)
    tok = md.ApiToken(
        id=f"tok-pat-{uid}-{len(md.API_TOKENS) + 1}",
        name=name,
        kind="personal",
        prefix=f"mip_{'0' * 10}{len(md.API_TOKENS) + 1:02d}",
        scopes=scopes,
        created_by_user_id=uid,
        created_by_display=display,
        created_at=datetime.now(),
        expires_at=expires_dt,
        last_used_at=None,
        last_used_ip=None,
        last_used_path=None,
        revoked_at=None,
        note=payload.get("note"),
        ip_allowlist=[],
    )
    md.API_TOKENS.append(tok)
    md.API_TOKEN_AUDIT.setdefault(tok.id, [])
    plaintext = f"{tok.prefix}_{'z' * 52}"
    hub.publish("api_token.created", {"id": tok.id, "kind": "personal"})
    return ok({"token": tok, "plaintext": plaintext,
               "curl_example": _curl_example(tok, plaintext)}, status_code=201)


@app.post("/api/v1/me/tokens/{tid}/revoke")
def api_me_tokens_revoke(tid: str, request: Request) -> Response:
    uid = current_user_id(request)
    tok = _token_by_id(tid)
    if tok is None or tok.kind != "personal" or tok.created_by_user_id != uid:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    if tok.revoked_at is None:
        tok.revoked_at = datetime.now()
        hub.publish("api_token.revoked", {"id": tok.id})
    return ok(tok)


@app.get("/api/v1/me/tokens/{tid}/audit")
def api_me_tokens_audit(tid: str, request: Request) -> Response:
    uid = current_user_id(request)
    tok = _token_by_id(tid)
    if tok is None or tok.kind != "personal" or tok.created_by_user_id != uid:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    return ok(md.API_TOKEN_AUDIT.get(tid, []))


# §11 — LLM assignments and the per-call feed are deployment-scoped
# (moved to /admin/api/v1/llm/*). Workspace managers keep only the
# percent-only usage tile on /settings via /api/v1/workspace/usage.


@app.get("/api/v1/workspace/usage")
def api_workspace_usage() -> Response:
    return ok(md.WORKSPACE_USAGE)


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


@app.get("/api/v1/documents/{did}/extraction")
def api_document_extraction(did: str) -> Response:
    doc = md.document_by_id(did)
    if doc is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    extraction = md.extraction_for_document(did)
    body_preview = ""
    page_count = 0
    token_count = 0
    extractor = None
    last_error = None
    has_secret_marker = False
    if extraction is not None:
        body_preview = (extraction.body_text or "")[:4000]
        page_count = len(extraction.pages or [])
        token_count = extraction.token_count
        extractor = extraction.extractor
        last_error = extraction.last_error
        has_secret_marker = extraction.has_secret_marker
    return ok({
        "document_id": doc.id,
        "status": doc.extraction_status,
        "extractor": extractor,
        "body_preview": body_preview,
        "page_count": page_count,
        "token_count": token_count,
        "has_secret_marker": has_secret_marker,
        "last_error": last_error,
        "extracted_at": doc.extracted_at,
    })


@app.get("/api/v1/documents/{did}/extraction/pages/{n}")
def api_document_extraction_page(did: str, n: int) -> Response:
    doc = md.document_by_id(did)
    if doc is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    extraction = md.extraction_for_document(did)
    if extraction is None or doc.extraction_status != "succeeded":
        return JSONResponse(
            {"detail": "extraction not available", "status": doc.extraction_status},
            status_code=409,
        )
    pages = extraction.pages or []
    if n < 1 or n > max(1, len(pages)):
        return JSONResponse({"detail": "page out of range"}, status_code=404)
    page_meta = pages[n - 1] if pages else {"page": n, "char_start": 0, "char_end": len(extraction.body_text)}
    body = extraction.body_text[page_meta["char_start"]:page_meta["char_end"]]
    return ok({
        "page": n,
        "char_start": page_meta["char_start"],
        "char_end": page_meta["char_end"],
        "body": body,
        "more_pages": n < len(pages),
    })


@app.post("/api/v1/documents/{did}/extraction/retry")
def api_document_extraction_retry(did: str) -> Response:
    doc = md.document_by_id(did)
    if doc is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    doc.extraction_status = "pending"
    hub.publish("asset_document.extraction_retried", {"document_id": doc.id})
    return ok({"document_id": doc.id, "status": doc.extraction_status})


# ── Knowledge base (kb) — agent search_kb / read_doc surfaces ────────

@app.get("/api/v1/kb/search")
def api_kb_search(
    q: str = "",
    kind: str = "",
    property_id: str = "",
    asset_id: str = "",
    document_kind: str = "",
    limit: int = 10,
) -> Response:
    results = md.search_kb(
        q,
        kind=kind,
        property_id=property_id,
        asset_id=asset_id,
        document_kind=document_kind,
        limit=limit,
    )
    return ok({"results": results, "total": len(results)})


@app.get("/api/v1/kb/doc/{kind}/{id_}")
def api_kb_read(kind: str, id_: str, page: int = 1) -> Response:
    if kind == "instruction":
        instr = next((i for i in md.INSTRUCTIONS if i.id == id_), None)
        if instr is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return ok({
            "kind": "instruction",
            "id": instr.id,
            "title": instr.title,
            "body": instr.body_md,
            "page": 1,
            "page_count": 1,
            "more_pages": False,
            "source_ref": {"scope": instr.scope, "property_id": instr.property_id, "area": instr.area},
        })
    if kind == "document":
        doc = md.document_by_id(id_)
        if doc is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        if doc.extraction_status != "succeeded":
            return ok({
                "kind": "document",
                "id": doc.id,
                "extraction_status": doc.extraction_status,
                "hint": (
                    "Extraction is still running — try again in a minute."
                    if doc.extraction_status in ("pending", "extracting")
                    else "I haven't been able to read this file."
                ),
            })
        extraction = md.extraction_for_document(id_)
        assert extraction is not None
        pages = extraction.pages or [{"page": 1, "char_start": 0, "char_end": len(extraction.body_text)}]
        if page < 1 or page > len(pages):
            return JSONResponse({"detail": "page out of range"}, status_code=404)
        page_meta = pages[page - 1]
        body = extraction.body_text[page_meta["char_start"]:page_meta["char_end"]]
        return ok({
            "kind": "document",
            "id": doc.id,
            "title": doc.title,
            "body": body,
            "page": page,
            "page_count": len(pages),
            "more_pages": page < len(pages),
            "source_ref": {"property_id": doc.property_id, "asset_id": doc.asset_id, "document_kind": doc.kind},
        })
    return JSONResponse({"detail": f"unknown kind: {kind}"}, status_code=400)


@app.get("/api/v1/kb/system_docs")
def api_kb_system_docs(role: str = "") -> Response:
    docs = md.AGENT_DOCS
    if role:
        docs = [d for d in docs if role in d.roles]
    return ok([
        {
            "slug": d.slug,
            "title": d.title,
            "summary": d.summary,
            "roles": d.roles,
            "updated_at": d.updated_at,
        }
        for d in docs
    ])


@app.get("/api/v1/kb/system_docs/{slug}")
def api_kb_system_doc(slug: str) -> Response:
    doc = md.agent_doc_by_slug(slug)
    if doc is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(doc)


# ── Admin: agent_doc overrides (§02 agent_doc, §11) ────────────────────

@app.get("/admin/api/v1/agent_docs")
def admin_agent_docs() -> Response:
    return ok([
        {
            "slug": d.slug,
            "title": d.title,
            "summary": d.summary,
            "roles": d.roles,
            "capabilities": d.capabilities,
            "version": d.version,
            "is_customised": d.is_customised,
            "default_hash": d.default_hash,
            "updated_at": d.updated_at,
        }
        for d in md.AGENT_DOCS
    ])


@app.get("/admin/api/v1/agent_docs/{slug}")
def admin_agent_doc(slug: str) -> Response:
    doc = md.agent_doc_by_slug(slug)
    if doc is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(doc)


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


@app.get("/api/v1/tasks/{tid}/chat/log")
def api_task_chat_log(tid: str) -> Response:
    """§06 task-scoped agent thread; same `AgentMessage[]` shape as /chat."""
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(md.TASK_CHAT_LOGS.setdefault(tid, []))


@app.post("/api/v1/tasks/{tid}/chat/message")
async def api_task_chat_message(tid: str, payload: dict[str, Any] = Body(...)) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    log = md.TASK_CHAT_LOGS.setdefault(tid, [])
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    log.append(msg)
    hub.publish(
        "agent.message.appended",
        {"scope": "task", "task_id": tid, "message": msg},
    )
    asyncio.create_task(_simulate_agent_turn("task", log, task_id=tid))
    return ok(msg)


@app.post("/api/v1/tasks/{tid}/chat/action/{idx}/{decision}")
def api_task_chat_action(tid: str, idx: int, decision: str) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    log = md.TASK_CHAT_LOGS.setdefault(tid, [])
    if idx < 0 or idx >= len(log) or decision not in {"approve", "details"}:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    msg = log[idx]
    if msg.kind != "action":
        return JSONResponse({"detail": "not an action"}, status_code=400)
    if decision == "approve":
        log[idx] = md.AgentMessage(at=msg.at, kind="agent", body=f"{msg.body} — approved.")
    else:
        log.append(md.AgentMessage(
            at=datetime.now(), kind="agent",
            body="Here are the details — nothing else to add from my side.",
        ))
    return ok(log)


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
def api_history(request: Request, tab: str = "tasks") -> Response:
    if tab not in {"tasks", "chats", "expenses", "leaves"}:
        tab = "tasks"
    uid = current_user_id(request)
    emp = md.employee_by_user_id(uid)
    emp_id = emp.id if emp else ""
    return ok({
        "tab": tab,
        "tasks": [
            t for t in md.tasks_for_user(uid)
            if t.status in {"completed", "skipped"} and md.visible_to(t, uid)
        ],
        "expenses": [
            x for x in md.expenses_for_user(uid)
            if x.status in {"approved", "reimbursed", "rejected"}
        ],
        "leaves": [
            lv for lv in md.leaves_for_employee(emp_id)
            if lv.approved_at is not None and lv.ends_on < md.TODAY
        ] if emp_id else [],
        "chats": md.HISTORY.get("chats", []),
    })


# ══════════════════════════════════════════════════════════════════════
# JSON API — writes
# ══════════════════════════════════════════════════════════════════════

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


@app.post("/api/v1/tasks")
def api_tasks_create(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Quick-add a task for the current user. §06 — self-assigned,
    `is_personal = true` by default. Workers land here too now that §05
    `tasks.create` allows `all_workers` (personal-only in spirit; the
    UI enforces the opt-out toggle)."""
    uid = current_user_id(request)
    role = current_role(request)
    emp_id = md.DEFAULT_EMPLOYEE_ID if role == "employee" else ""

    title = str(payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"detail": "title required"}, status_code=400)

    is_personal = bool(payload.get("is_personal", True))
    property_id = str(payload.get("property_id") or "")
    area = str(payload.get("area") or "")

    scheduled_raw = payload.get("scheduled_start")
    if scheduled_raw:
        try:
            raw = str(scheduled_raw).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return JSONResponse({"detail": "bad scheduled_start"}, status_code=400)
        # Seed TASKS are tz-naive; keep this normalised so the list-wide
        # sort in /today doesn't mix aware + naive datetimes.
        scheduled = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    else:
        scheduled = datetime.combine(md.TODAY, time(9, 0))

    task = md.Task(
        id=f"t-u-{len(md.TASKS) + 1}",
        title=title,
        property_id=property_id,
        area=area,
        assignee_id=emp_id,
        scheduled_start=scheduled,
        estimated_minutes=int(payload.get("estimated_minutes") or 30),
        priority="normal",
        status="pending",
        assigned_user_id=uid,
        created_by=uid,
        is_personal=is_personal,
        workspace_id=md.DEFAULT_WORKSPACE_ID,
    )
    md.TASKS.append(task)
    hub.publish("task.updated", {"task": task})
    return ok(task, status_code=201)


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


@app.post("/api/v1/quotes/{qid}/{decision}")
def api_quote_decide(qid: str, decision: str, request: Request) -> Response:
    """Client- or owner-side decision on a quote (§22).

    Acceptance is unconditionally approval-gated in production; this
    mock accepts the click directly so the UI can show the resulting
    state. Accepting also flips the parent work_order to `accepted`
    and writes `accepted_quote_id`, mirroring the spec's transaction.
    """
    if decision not in {"accept", "reject"}:
        return JSONResponse({"detail": "bad decision"}, status_code=400)
    quote = next((q for q in md.QUOTES if q.id == qid), None)
    if quote is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    quote.status = "accepted" if decision == "accept" else "rejected"
    quote.decided_by_user_id = current_user_id(request)
    quote.decided_at = md.NOW
    if decision == "accept":
        wo = md.work_order_by_id(quote.work_order_id)
        if wo is not None:
            wo.accepted_quote_id = quote.id
            wo.state = "accepted"
            for sibling in md.QUOTES:
                if sibling.work_order_id == wo.id and sibling.id != quote.id and sibling.status == "submitted":
                    sibling.status = "superseded"
    hub.publish("quote." + ("accepted" if decision == "accept" else "rejected"),
                {"id": quote.id, "work_order_id": quote.work_order_id})
    return ok(quote)


@app.post("/api/v1/agent/employee/message")
async def api_agent_employee_message(payload: dict[str, Any] = Body(...)) -> Response:
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.EMPLOYEE_CHAT_LOG.append(msg)
    hub.publish("agent.message.appended", {"scope": "employee", "message": msg})
    asyncio.create_task(_simulate_agent_turn("employee", md.EMPLOYEE_CHAT_LOG))
    return ok(msg)


@app.post("/api/v1/agent/manager/message")
async def api_agent_manager_message(payload: dict[str, Any] = Body(...)) -> Response:
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.MANAGER_AGENT_LOG.append(msg)
    hub.publish("agent.message.appended", {"scope": "manager", "message": msg})
    asyncio.create_task(_simulate_agent_turn("manager", md.MANAGER_AGENT_LOG))
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


@app.get("/api/v1/chat/channels")
def api_chat_channels() -> Response:
    """§23 — every binding the current mock user can see.

    In the preview both roles see every binding for simplicity; the
    production split (self on /me, workspace-wide on /chat-channels)
    lives in §14.
    """
    return ok([
        {
            "id": b.id,
            "user_id": b.user_id,
            "user_display_name": (
                md.user_by_id(b.user_id).display_name
                if md.user_by_id(b.user_id) else b.user_id
            ),
            "channel_kind": b.channel_kind,
            "address": b.address,
            "display_label": b.display_label,
            "state": b.state,
            "verified_at": b.verified_at,
            "last_message_at": b.last_message_at,
            "revoked_at": b.revoked_at,
            "revoke_reason": b.revoke_reason,
        }
        for b in md.CHAT_CHANNEL_BINDINGS
    ])


@app.get("/api/v1/chat/channels/providers")
def api_chat_channels_providers() -> Response:
    """§23 — provider config display stubs for /settings."""
    return ok(md.CHAT_GATEWAY_PROVIDERS)


@app.post("/api/v1/chat/channels/link/start")
def api_chat_channels_link_start(payload: dict[str, Any] = Body(...)) -> Response:
    """§23 link ceremony, step 1: send the code.

    Mock implementation — accepts the request, flips (or inserts) a
    `pending` binding, pretends to send a WhatsApp template message.
    """
    channel_kind = str(payload.get("channel_kind") or "").strip()
    address = str(payload.get("address") or "").strip()
    user_id = str(payload.get("user_id") or md.DEFAULT_EMPLOYEE_ID).strip()
    if channel_kind not in {"offapp_whatsapp", "offapp_telegram"}:
        return JSONResponse({"detail": "bad channel_kind"}, status_code=400)
    if not address:
        return JSONResponse({"detail": "address required"}, status_code=400)
    existing = next(
        (
            b for b in md.CHAT_CHANNEL_BINDINGS
            if b.user_id == user_id
            and b.channel_kind == channel_kind
            and b.state != "revoked"
        ),
        None,
    )
    if existing and existing.state == "active":
        return JSONResponse(
            {"detail": "already linked", "binding_id": existing.id},
            status_code=409,
        )
    if existing:
        existing.address = address
    else:
        existing = md.ChatChannelBinding(
            id=f"ccb-{user_id}-{channel_kind[-2:]}",
            user_id=user_id,
            channel_kind=channel_kind,  # type: ignore[arg-type]
            address=address,
            display_label="Personal phone",
            state="pending",
        )
        md.CHAT_CHANNEL_BINDINGS.append(existing)
    hub.publish(
        "chat_channel_binding.created",
        {"binding_id": existing.id, "channel_kind": channel_kind},
    )
    return ok({
        "binding_id": existing.id,
        "state": existing.state,
        "hint": "code sent over the target channel — enter it below",
    })


@app.post("/api/v1/chat/channels/link/verify")
def api_chat_channels_link_verify(payload: dict[str, Any] = Body(...)) -> Response:
    """§23 link ceremony, step 2: verify the 6-digit code."""
    binding_id = str(payload.get("binding_id") or "").strip()
    code = str(payload.get("code") or "").strip()
    binding = next(
        (b for b in md.CHAT_CHANNEL_BINDINGS if b.id == binding_id),
        None,
    )
    if binding is None:
        return JSONResponse({"detail": "unknown binding"}, status_code=404)
    if binding.state != "pending":
        return JSONResponse(
            {"detail": f"binding is {binding.state}"},
            status_code=409,
        )
    if code != "424242":  # mock code — every pending binding accepts this
        return JSONResponse({"detail": "wrong code"}, status_code=400)
    binding.state = "active"
    binding.verified_at = datetime.now()
    hub.publish(
        "chat_channel_binding.verified",
        {"binding_id": binding.id, "channel_kind": binding.channel_kind},
    )
    return ok({"binding_id": binding.id, "state": binding.state})


@app.post("/api/v1/chat/channels/{bid}/unlink")
def api_chat_channels_unlink(bid: str) -> Response:
    """§23 — user-initiated revocation."""
    binding = next((b for b in md.CHAT_CHANNEL_BINDINGS if b.id == bid), None)
    if binding is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    if binding.state == "revoked":
        return ok({"binding_id": binding.id, "state": binding.state})
    binding.state = "revoked"
    binding.revoked_at = datetime.now()
    binding.revoke_reason = "user"
    hub.publish(
        "chat_channel_binding.revoked",
        {"binding_id": binding.id, "channel_kind": binding.channel_kind},
    )
    return ok({"binding_id": binding.id, "state": binding.state})


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

# ══════════════════════════════════════════════════════════════════════
# /admin/api/v1 — deployment-scoped routes (§05, §11, §12, §14).
#
# Non-admin callers are supposed to receive 404 on every route here
# (same tenant-enumeration posture as the workspace prefix). In the
# mock every visitor is the bootstrap operator Élodie, so the guard
# is soft — we still surface the intent on the wire so the React
# shell can test the "ask your operator" state by switching role.
# ══════════════════════════════════════════════════════════════════════

def _require_deployment_admin(request: Request) -> Response | None:
    uid = current_user_id(request)
    if not any(m.user_id == uid for m in md.DEPLOYMENT_ADMINS):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return None


@app.get("/admin/api/v1/me")
def api_admin_me(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    uid = current_user_id(request)
    row = next((m for m in md.DEPLOYMENT_ADMINS if m.user_id == uid), None)
    return ok({
        "user_id": row.user_id,
        "display_name": row.display_name,
        "email": row.email,
        "is_owner": row.is_owner,
        "capabilities": {
            "deployment.view": True,
            "deployment.llm.view": True,
            "deployment.llm.edit": True,
            "deployment.usage.view": True,
            "deployment.workspaces.view": True,
            "deployment.workspaces.trust": True,
            "deployment.workspaces.archive": row.is_owner,
            "deployment.budget.edit": True,
            "deployment.signup.edit": True,
            "deployment.settings.edit": row.is_owner,
            "deployment.audit.view": True,
            "groups.manage_owners_membership": row.is_owner,
        },
    })


# §11 — /admin/llm "graph" endpoints. Three-column view (providers →
# models → assignments) plus the prompt-library slide-over and the weekly
# pricing sync trigger. All deployment-scope, gated by
# deployment.llm.{view,edit}.


@app.get("/admin/api/v1/llm/graph")
def api_admin_llm_graph(request: Request) -> Response:
    """Single-call snapshot for the three-column admin graph.

    Returns providers, models, provider-models, the capability catalogue,
    inheritance edges, assignments (grouped later on the client), and the
    summary totals that fill the stat cards. One round trip keeps the
    hover-highlight interactions responsive.
    """

    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard

    # Denormalised counts for card chips
    pm_by_provider: dict[str, int] = {}
    pm_by_model: dict[str, int] = {}
    for pm in md.LLM_PROVIDER_MODELS:
        pm_by_provider[pm.provider_id] = pm_by_provider.get(pm.provider_id, 0) + 1
        pm_by_model[pm.model_id] = pm_by_model.get(pm.model_id, 0) + 1

    providers = [
        {
            **p.__dict__,
            "provider_model_count": pm_by_provider.get(p.id, 0),
        }
        for p in md.LLM_PROVIDERS
    ]
    models = [
        {
            **m.__dict__,
            "provider_model_count": pm_by_model.get(m.id, 0),
        }
        for m in md.LLM_MODELS
    ]
    assignments = [a.__dict__ for a in md.LLM_ASSIGNMENTS_GRAPH]

    # Validation: which assignments serve a provider_model whose model
    # lacks one or more required capability tags? The graph renders those
    # edges red.
    by_pm = {pm.id: pm for pm in md.LLM_PROVIDER_MODELS}
    by_model = {m.id: m for m in md.LLM_MODELS}
    assignment_issues: list[dict[str, Any]] = []
    for a in md.LLM_ASSIGNMENTS_GRAPH:
        pm = by_pm.get(a.provider_model_id)
        if pm is None:
            continue
        model = by_model.get(pm.model_id)
        if model is None:
            continue
        missing = [c for c in a.required_capabilities if c not in model.capabilities]
        if missing:
            assignment_issues.append({
                "assignment_id": a.id,
                "capability": a.capability,
                "missing_capabilities": missing,
            })

    total_spent = sum(a.spend_usd_30d for a in md.LLM_ASSIGNMENTS_GRAPH)
    total_calls = sum(a.calls_30d for a in md.LLM_ASSIGNMENTS_GRAPH)

    return ok({
        "providers": providers,
        "models": models,
        "provider_models": [pm.__dict__ for pm in md.LLM_PROVIDER_MODELS],
        "capabilities": md.LLM_CAPABILITY_CATALOGUE,
        "inheritance": [i.__dict__ for i in md.LLM_CAPABILITY_INHERITANCE],
        "assignments": assignments,
        "assignment_issues": assignment_issues,
        "totals": {
            "spend_usd_30d": round(total_spent, 2),
            "calls_30d": total_calls,
            "provider_count": len(md.LLM_PROVIDERS),
            "model_count": len(md.LLM_MODELS),
            "capability_count": len(md.LLM_CAPABILITY_CATALOGUE),
            "unassigned_capabilities": [
                c["key"]
                for c in md.LLM_CAPABILITY_CATALOGUE
                if not any(a.capability == c["key"] and a.is_enabled for a in md.LLM_ASSIGNMENTS_GRAPH)
                # chat.admin inherits — still counts as resolved.
                and not any(inh.capability == c["key"] for inh in md.LLM_CAPABILITY_INHERITANCE)
            ],
        },
    })


@app.get("/admin/api/v1/llm/prompts")
def api_admin_llm_prompts(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.LLM_PROMPT_TEMPLATES)


@app.post("/admin/api/v1/llm/sync-pricing")
def api_admin_llm_sync_pricing(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    # Mock-only: report the would-be deltas without mutating state.
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    deltas = [
        {"provider_model_id": pm.id, "api_model_id": pm.api_model_id,
         "input_before": pm.input_cost_per_million, "input_after": pm.input_cost_per_million,
         "output_before": pm.output_cost_per_million, "output_after": pm.output_cost_per_million,
         "status": "pinned" if pm.price_source_override == "none" else "unchanged"}
        for pm in md.LLM_PROVIDER_MODELS
    ]
    return ok({"started_at": now_iso, "deltas": deltas, "updated": 0, "skipped": len(deltas), "errors": 0})


@app.get("/admin/api/v1/llm/calls")
def api_admin_llm_calls(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.LLM_CALLS)


@app.get("/admin/api/v1/chat/providers")
def api_admin_chat_providers(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_CHAT_PROVIDERS)


@app.get("/admin/api/v1/chat/overrides")
def api_admin_chat_overrides(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_CHAT_OVERRIDES)


@app.get("/admin/api/v1/usage/summary")
def api_admin_usage_summary(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    per_cap = [
        {
            "capability": a.capability,
            "spend_usd_30d": round(a.spent_24h_usd * 30, 2),
            "calls_30d": a.calls_24h * 30,
        }
        for a in md.LLM_ASSIGNMENTS
        if a.enabled and a.calls_24h > 0
    ]
    spend = sum(w.spent_usd_30d for w in md.DEPLOYMENT_WORKSPACES if not w.archived_at)
    calls = sum(c["calls_30d"] for c in per_cap)
    active = [w for w in md.DEPLOYMENT_WORKSPACES if not w.archived_at]
    return ok({
        "window_label": "Rolling 30 days",
        "deployment_spend_usd_30d": round(spend, 2),
        "deployment_call_count_30d": calls,
        "workspace_count": len(active),
        "paused_workspaces": sum(1 for w in active if w.paused),
        "per_capability": per_cap,
    })


@app.get("/admin/api/v1/usage/workspaces")
def api_admin_usage_workspaces(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok([w for w in md.DEPLOYMENT_WORKSPACES if not w.archived_at])


@app.put("/admin/api/v1/usage/workspaces/{wsid}/cap")
async def api_admin_usage_workspaces_set_cap(wsid: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    ws = next((w for w in md.DEPLOYMENT_WORKSPACES if w.id == wsid), None)
    if ws is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    body = await request.json()
    cap = float((body or {}).get("cap_usd_30d", 0))
    if cap < 0 or cap > 10_000:
        return JSONResponse({"detail": "cap out of range"}, status_code=422)
    ws.cap_usd_30d = cap
    ws.usage_percent = int(min(100, (ws.spent_usd_30d / cap) * 100)) if cap else 0
    ws.paused = cap > 0 and ws.spent_usd_30d >= cap
    hub.publish("admin.workspace_cap_updated", {"id": ws.id, "cap_usd_30d": cap})
    return ok(ws)


@app.get("/admin/api/v1/workspaces")
def api_admin_workspaces(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_WORKSPACES)


@app.get("/admin/api/v1/workspaces/{wsid}")
def api_admin_workspace(wsid: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    ws = next((w for w in md.DEPLOYMENT_WORKSPACES if w.id == wsid), None)
    if ws is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(ws)


@app.post("/admin/api/v1/workspaces/{wsid}/trust")
def api_admin_workspace_trust(wsid: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    ws = next((w for w in md.DEPLOYMENT_WORKSPACES if w.id == wsid), None)
    if ws is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    ws.verification_state = "trusted"
    hub.publish("admin.workspace_trusted", {"id": ws.id})
    return ok(ws)


@app.post("/admin/api/v1/workspaces/{wsid}/archive")
def api_admin_workspace_archive(wsid: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    uid = current_user_id(request)
    row = next((m for m in md.DEPLOYMENT_ADMINS if m.user_id == uid), None)
    if not row or not row.is_owner:
        return JSONResponse({"detail": "owners-only"}, status_code=403)
    ws = next((w for w in md.DEPLOYMENT_WORKSPACES if w.id == wsid), None)
    if ws is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    ws.archived_at = md.TODAY
    hub.publish("admin.workspace_archived", {"id": ws.id})
    return ok(ws)


@app.get("/admin/api/v1/signup/settings")
def api_admin_signup_settings(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_SIGNUP_SETTINGS)


@app.put("/admin/api/v1/signup/settings")
async def api_admin_signup_settings_update(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    body = await request.json()
    s = md.DEPLOYMENT_SIGNUP_SETTINGS
    for key in (
        "enabled",
        "throttle_per_ip_hour",
        "throttle_per_email_lifetime",
        "pre_verified_upload_mb_cap",
        "pre_verified_llm_percent_cap",
    ):
        if key in body:
            setattr(s, key, body[key])
    s.updated_at = datetime.now().isoformat(timespec="seconds") + "Z"
    s.updated_by = "Élodie Bernard"
    hub.publish("admin.signup_settings_updated", {"enabled": s.enabled})
    return ok(s)


@app.get("/admin/api/v1/settings")
def api_admin_settings(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_SETTINGS)


@app.put("/admin/api/v1/settings/{key}")
async def api_admin_settings_update(key: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    row = next((s for s in md.DEPLOYMENT_SETTINGS if s.key == key), None)
    if row is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    uid = current_user_id(request)
    me = next((m for m in md.DEPLOYMENT_ADMINS if m.user_id == uid), None)
    if row.root_only and not (me and me.is_owner):
        return JSONResponse({"detail": "owners-only"}, status_code=403)
    body = await request.json()
    row.value = body.get("value", row.value)
    row.updated_at = datetime.now().isoformat(timespec="seconds") + "Z"
    row.updated_by = me.display_name if me else "(unknown)"
    hub.publish("admin.setting_updated", {"key": key})
    return ok(row)


@app.get("/admin/api/v1/admins")
def api_admin_admins(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_ADMINS)


@app.post("/admin/api/v1/admins")
async def api_admin_admins_grant(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    body = await request.json()
    email = (body or {}).get("email", "").strip()
    if not email:
        return JSONResponse({"detail": "email required"}, status_code=422)
    as_owner = bool((body or {}).get("as_owner", False))
    if as_owner:
        uid = current_user_id(request)
        me = next((m for m in md.DEPLOYMENT_ADMINS if m.user_id == uid), None)
        if not me or not me.is_owner:
            return JSONResponse(
                {"detail": "groups.manage_owners_membership required"},
                status_code=403,
            )
    new_id = f"da-{len(md.DEPLOYMENT_ADMINS) + 1}"
    row = md.AdminTeamMember(
        id=new_id,
        user_id=f"u-pending-{len(md.DEPLOYMENT_ADMINS) + 1}",
        display_name=email.split("@")[0].title(),
        email=email,
        is_owner=as_owner,
        granted_at=md.TODAY,
        granted_by="Élodie Bernard",
    )
    md.DEPLOYMENT_ADMINS.append(row)
    hub.publish("admin.granted", {"user_id": row.user_id})
    return ok(row)


@app.post("/admin/api/v1/admins/{daid}/revoke")
def api_admin_admins_revoke(daid: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    row = next((m for m in md.DEPLOYMENT_ADMINS if m.id == daid), None)
    if row is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    owners = [m for m in md.DEPLOYMENT_ADMINS if m.is_owner]
    if row.is_owner and len(owners) <= 1:
        return JSONResponse(
            {"detail": "cannot revoke last owner (≥1 owner invariant)"},
            status_code=422,
        )
    md.DEPLOYMENT_ADMINS[:] = [m for m in md.DEPLOYMENT_ADMINS if m.id != daid]
    hub.publish("admin.revoked", {"user_id": row.user_id})
    return ok({"ok": True, "id": daid})


@app.get("/admin/api/v1/audit")
def api_admin_audit(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.DEPLOYMENT_AUDIT)


# ── Admin-side embedded chat agent (§11 "Admin-side agent") ──────────


@app.get("/admin/api/v1/agent/log")
def api_admin_agent_log(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.ADMIN_AGENT_LOG)


@app.get("/admin/api/v1/agent/actions")
def api_admin_agent_actions(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    return ok(md.ADMIN_AGENT_ACTIONS)


@app.post("/admin/api/v1/agent/message")
async def api_admin_agent_message(request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    payload = await request.json()
    body = str((payload or {}).get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    page = request.headers.get("X-Agent-Page", "")
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.ADMIN_AGENT_LOG.append(msg)
    hub.publish(
        "agent.message.appended",
        {"scope": "admin", "message": msg, "page": page},
    )
    asyncio.create_task(_simulate_agent_turn("admin", md.ADMIN_AGENT_LOG))
    return ok({"message": msg, "page": page})


@app.post("/admin/api/v1/agent/action/{aid}/{decision}")
def api_admin_agent_action(aid: str, decision: str, request: Request) -> Response:
    guard = _require_deployment_admin(request)
    if guard is not None:
        return guard
    action = next((a for a in md.ADMIN_AGENT_ACTIONS if a.id == aid), None)
    if action is None or decision not in {"approve", "deny"}:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    md.ADMIN_AGENT_ACTIONS[:] = [a for a in md.ADMIN_AGENT_ACTIONS if a.id != aid]
    verb = "Approved" if decision == "approve" else "Denied"
    md.ADMIN_AGENT_LOG.append(
        md.AgentMessage(at=datetime.now(), kind="user", body=f"{verb}: {action.title}")
    )
    if decision == "approve":
        md.ADMIN_AGENT_LOG.append(
            md.AgentMessage(
                at=datetime.now(), kind="agent",
                body=f"Done — {action.title.lower()} is in the deployment audit log.",
            )
        )
    return ok({"ok": True, "id": aid, "decision": decision})


# Mount built assets (JS, CSS, fonts) under Vite's default /assets path.
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")


_SPA_PASSTHROUGH: Iterable[str] = (
    "/api",
    "/admin/api",
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
