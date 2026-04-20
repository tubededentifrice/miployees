"""Tests for the ``/w/<slug>/...`` tenancy middleware.

Every test runs against an in-process FastAPI ``TestClient`` so no
socket is bound — the ASGI app is driven directly.

See docs/specs/01-architecture.md §"Workspace addressing" and
§"WorkspaceContext".
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.tenancy.context import WorkspaceContext
from app.tenancy.current import _current_ctx, get_current
from app.tenancy.middleware import (
    CORRELATION_ID_HEADER,
    SKIP_PATHS,
    TEST_ACTOR_ID_HEADER,
    TEST_WORKSPACE_ID_HEADER,
    WorkspaceContextMiddleware,
)

# ULID is 26 chars of Crockford base32: 0-9 and A-Z minus I, L, O, U.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


# ---------------------------------------------------------------------------
# Test-app scaffolding
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_current_ctx() -> Iterator[None]:
    """Guarantee no WorkspaceContext bleeds between tests.

    The middleware resets the ContextVar in its ``finally`` block, but
    if a test installs a context outside the middleware (or a bug in
    the middleware drops the reset) we want the next test to start
    clean.
    """
    token = _current_ctx.set(None)
    try:
        yield
    finally:
        _current_ctx.reset(token)


def _build_app(*, captured: list[WorkspaceContext | None] | None = None) -> FastAPI:
    """Construct a minimal FastAPI app with the middleware installed.

    Adds routes covering every branch exercised by the tests:

    * ``/w/{slug}/api/v1/ping`` — scoped; captures ``get_current()``.
    * ``/w/{slug}/api/v1/boom`` — scoped; raises so we can assert
      ContextVar cleanup runs on exceptions.
    * ``/healthz``, ``/signup``, ``/signup/start`` — skip paths.
    * ``/w``, ``/w/{slug}`` — bare ``/w`` SPA catch-alls.
    """
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/w/{slug}/api/v1/ping")
    def scoped_ping(slug: str) -> dict[str, object]:
        ctx = get_current()
        if captured is not None:
            captured.append(ctx)
        if ctx is None:
            return {"bound": False, "slug_from_path": slug}
        return {
            "bound": True,
            "workspace_id": ctx.workspace_id,
            "workspace_slug": ctx.workspace_slug,
            "actor_id": ctx.actor_id,
            "actor_kind": ctx.actor_kind,
            "actor_grant_role": ctx.actor_grant_role,
            "actor_was_owner_member": ctx.actor_was_owner_member,
            "audit_correlation_id": ctx.audit_correlation_id,
        }

    @app.get("/w/{slug}/api/v1/boom")
    def scoped_boom(slug: str) -> dict[str, str]:
        raise ValueError("kaboom")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.get("/signup")
    def signup_get() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.post("/signup/start")
    def signup_start() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    # Bare-host SPA catch-all shapes. These don't actually have real
    # handlers in production (the SPA serves them), but we register
    # stubs to verify the middleware hands them through un-scoped and
    # 200.
    @app.get("/w")
    def bare_w() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.get("/w/{slug}")
    def bare_w_slug(slug: str) -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None, "slug": slug}

    return app


def _client(app: FastAPI | None = None) -> TestClient:
    # ``raise_server_exceptions=False`` lets us observe the 500 shape
    # produced by Starlette's error handler when a downstream raises,
    # instead of having the exception bubble out of ``client.get``.
    return TestClient(app or _build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_scoped_request_binds_workspace_context() -> None:
    captured: list[WorkspaceContext | None] = []
    app = _build_app(captured=captured)

    with _client(app) as client:
        response = client.get(
            "/w/villa-sud/api/v1/ping",
            headers={
                TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A",
                TEST_ACTOR_ID_HEADER: "01US000000000000000000000B",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["bound"] is True
    assert body["workspace_id"] == "01WS000000000000000000000A"
    assert body["workspace_slug"] == "villa-sud"
    assert body["actor_id"] == "01US000000000000000000000B"
    assert body["actor_kind"] == "user"
    assert body["actor_grant_role"] == "manager"
    assert body["actor_was_owner_member"] is False

    # The correlation id on the response MUST equal the one the handler
    # saw on the ctx — single source of truth per request.
    correlation_id = response.headers[CORRELATION_ID_HEADER]
    assert correlation_id == body["audit_correlation_id"]
    # No incoming X-Request-Id ⇒ minted ULID.
    assert _ULID_RE.match(correlation_id)

    # Handler saw a live ctx; after the request returns the ContextVar
    # is restored to None (autouse fixture guarantees pre-test state).
    assert len(captured) == 1
    assert captured[0] is not None
    assert get_current() is None


def test_actor_id_defaults_to_fresh_ulid_when_header_missing() -> None:
    app = _build_app()
    with _client(app) as client:
        response = client.get(
            "/w/villa-sud/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    assert response.status_code == 200
    actor_id = response.json()["actor_id"]
    assert _ULID_RE.match(actor_id)


# ---------------------------------------------------------------------------
# Rejection paths — every branch is 404 (never 403)
# ---------------------------------------------------------------------------


def test_invalid_slug_pattern_returns_404() -> None:
    with _client() as client:
        response = client.get(
            "/w/UPPER/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    assert response.status_code == 404
    # Correlation id still echoed — ops must be able to correlate the
    # rejection to the request in logs.
    assert CORRELATION_ID_HEADER in response.headers


def test_reserved_slug_returns_404() -> None:
    with _client() as client:
        response = client.get(
            "/w/admin/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    assert response.status_code == 404


def test_consecutive_hyphen_slug_returns_404() -> None:
    # Regex-valid but rejected by ``validate_slug`` via the
    # explicit consecutive-hyphen rule.
    with _client() as client:
        response = client.get(
            "/w/foo--bar/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    assert response.status_code == 404


def test_url_encoded_bad_slug_returns_404() -> None:
    # ``%20`` decodes to a space — not valid in the slug regex.
    with _client() as client:
        response = client.get(
            "/w/%20badslug/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    assert response.status_code == 404


def test_empty_slug_returns_404() -> None:
    # ``/w//api/v1/ping`` — the slug segment is empty. Not a skip path
    # (bare-``/w`` is only ``/w`` or ``/w/``), not parseable.
    with _client() as client:
        response = client.get(
            "/w//api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    assert response.status_code == 404


def test_missing_phase0_workspace_header_returns_404() -> None:
    with _client() as client:
        response = client.get("/w/villa-sud/api/v1/ping")
    assert response.status_code == 404
    # Still echoes a correlation id — skip/non-skip parity.
    assert CORRELATION_ID_HEADER in response.headers


# ---------------------------------------------------------------------------
# Skip paths pass through un-scoped
# ---------------------------------------------------------------------------


def test_healthz_is_skip_path() -> None:
    with _client() as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "bound": False}
    # Correlation id header present on skip-path responses too.
    assert CORRELATION_ID_HEADER in response.headers
    assert _ULID_RE.match(response.headers[CORRELATION_ID_HEADER])


def test_signup_get_is_skip_path() -> None:
    with _client() as client:
        response = client.get("/signup")
    assert response.status_code == 200
    assert response.json()["bound"] is False


def test_signup_post_child_is_skip_path() -> None:
    # ``/signup/start`` must match via the child-segment rule; the
    # middleware cannot require every POST target to be enumerated.
    with _client() as client:
        response = client.post("/signup/start")
    assert response.status_code == 200
    assert response.json()["bound"] is False


def test_bare_w_is_skip_path() -> None:
    with _client() as client:
        response = client.get("/w")
    assert response.status_code == 200
    assert response.json()["bound"] is False


def test_bare_w_slug_is_skip_path() -> None:
    # ``/w/villa-sud`` with no trailing segment is handled by the SPA
    # catch-all, not the scoped-route parser.
    with _client() as client:
        response = client.get("/w/villa-sud")
    assert response.status_code == 200
    assert response.json()["bound"] is False
    assert response.json()["slug"] == "villa-sud"


def test_spec_bare_host_routes_are_skip_paths() -> None:
    """Every bare-host surface called out by specs §01/§12/§14 is listed.

    This is a guard-rail test: if a future spec change adds or renames a
    bare-host route, the mismatch between the spec's expectation and
    ``SKIP_PATHS`` surfaces here rather than as a surprise 404 at
    runtime.
    """
    # §01 "Workspace addressing" canonical bare-host list.
    for path in (
        "/healthz",
        "/readyz",
        "/version",
        "/signup",
        "/login",
        "/recover",
        "/select-workspace",
        "/api/openapi.json",
        "/docs",
        "/redoc",
    ):
        assert path in SKIP_PATHS, f"§01 bare-host route {path} missing from SKIP_PATHS"
    # §12 "Base URL" bare-host API prefix + §14 "Admin" shell.
    assert "/api/v1" in SKIP_PATHS
    assert "/admin" in SKIP_PATHS
    # §03 "Self-serve signup" / §14 "Public" bare-host auth surface.
    assert "/auth/magic" in SKIP_PATHS
    assert "/auth/passkey" in SKIP_PATHS
    # §14 "Public" email-change magic-link landing.
    assert "/me/email/verify" in SKIP_PATHS


def test_auth_passkey_child_is_skip_path() -> None:
    """``/auth/passkey/signup/register/start`` passes through un-scoped.

    The passkey signup router (``app/api/v1/auth/passkey.py``) is the
    sibling of ``/auth/magic`` and must have the same skip-through
    behaviour: reach the router, never 404 at the middleware.
    """
    app = _build_app()

    @app.post("/auth/passkey/signup/register/start")
    def passkey_child() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    with _client(app) as client:
        response = client.post("/auth/passkey/signup/register/start")
    assert response.status_code == 200
    assert response.json()["bound"] is False


def test_admin_child_is_skip_path() -> None:
    """``/admin/dashboard`` is bare-host per §14; middleware skips it."""
    app = _build_app()

    @app.get("/admin/dashboard")
    def admin_dashboard() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    with _client(app) as client:
        response = client.get("/admin/dashboard")
    assert response.status_code == 200
    assert response.json()["bound"] is False


def test_bare_w_slug_trailing_slash_is_skip_path() -> None:
    # ``/w/villa-sud/`` — trailing slash with no child segment, still
    # a SPA catch-all. The test-app routes ``/w/{slug}`` catches this
    # after FastAPI's trailing-slash handling; we just need the
    # middleware to not 404 it.
    with _client() as client:
        response = client.get("/w/villa-sud/", follow_redirects=True)
    # Either a direct 200 or a redirect-then-200 is acceptable: what
    # the middleware MUST NOT do is reject the request as an invalid
    # scoped hit.
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Correlation id propagation
# ---------------------------------------------------------------------------


def test_correlation_id_echoed_from_request() -> None:
    incoming = "01RQ000000000000000000000C"
    with _client() as client:
        response = client.get(
            "/w/villa-sud/api/v1/ping",
            headers={
                TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A",
                CORRELATION_ID_HEADER: incoming,
            },
        )
    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER] == incoming
    # Also copied onto the bound ctx.
    assert response.json()["audit_correlation_id"] == incoming


def test_correlation_id_echoed_on_skip_path() -> None:
    incoming = "01RQ000000000000000000000D"
    with _client() as client:
        response = client.get("/healthz", headers={CORRELATION_ID_HEADER: incoming})
    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER] == incoming


def test_correlation_id_minted_when_missing() -> None:
    with _client() as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    minted = response.headers[CORRELATION_ID_HEADER]
    assert _ULID_RE.match(minted)


# ---------------------------------------------------------------------------
# ContextVar cleanup
# ---------------------------------------------------------------------------


def test_context_does_not_leak_across_requests() -> None:
    captured: list[WorkspaceContext | None] = []
    app = _build_app(captured=captured)
    with _client(app) as client:
        first = client.get(
            "/w/villa-sud/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
        second = client.get(
            "/w/villa-sud/api/v1/ping",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000B"},
        )
    assert first.status_code == 200
    assert second.status_code == 200
    # Both requests saw a live (distinct) context — no leak, no reuse.
    assert len(captured) == 2
    assert captured[0] is not None
    assert captured[1] is not None
    assert captured[0] is not captured[1]
    assert captured[0].workspace_id == "01WS000000000000000000000A"
    assert captured[1].workspace_id == "01WS000000000000000000000B"
    # And the ContextVar is back to None outside the requests.
    assert get_current() is None


def test_context_cleanup_on_handler_exception() -> None:
    app = _build_app()
    with _client(app) as client:
        response = client.get(
            "/w/villa-sud/api/v1/boom",
            headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
        )
    # Starlette's default error handler surfaces 500.
    assert response.status_code == 500
    # Crucially, the ContextVar was still reset. If ``reset_current``
    # had been skipped, ``get_current()`` here would return the ctx
    # from the failed request.
    assert get_current() is None
