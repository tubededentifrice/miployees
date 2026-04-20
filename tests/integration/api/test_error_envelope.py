"""Integration snapshots for the problem+json envelope.

Exercises :func:`app.api.factory.create_app` end-to-end so the
envelope shape is asserted on responses produced by the composed
application — middleware, exception handlers, and catch-all included.
One response per canonical ``type`` URI is pinned here so a
regression in any arm fails the right test.

Canonical ``type`` URIs under test (spec §12 "Errors"):

* ``validation``
* ``not_found``
* ``conflict``
* ``unauthorized``
* ``forbidden``
* ``rate_limited``
* ``upstream_unavailable``
* ``idempotency_conflict``
* ``approval_required``
* ``internal`` (fallback for unknown :class:`DomainError` subclass)

See ``docs/specs/12-rest-api.md`` §"Errors" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.errors import CANONICAL_TYPE_BASE, CONTENT_TYPE_PROBLEM_JSON
from app.api.factory import create_app
from app.config import Settings
from app.domain.errors import (
    ApprovalRequired,
    Conflict,
    DomainError,
    Forbidden,
    IdempotencyConflict,
    NotFound,
    RateLimited,
    Unauthorized,
    UpstreamUnavailable,
    Validation,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — composed app + TestClient
# ---------------------------------------------------------------------------


def _pinned_settings(
    db_url: str,
    *,
    profile: Literal["prod", "dev"] = "prod",
) -> Settings:
    """Settings bound to the integration-harness DB URL.

    Mirrors the shape used in
    :mod:`tests.integration.api.test_security_headers` so the two
    integration tests share a common baseline.
    """
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-error-envelope-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile=profile,
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
        cors_allow_origins=[],
    )


class _UnregisteredError(DomainError):
    """Subclass that is NOT in the status map → 500 ``internal``."""

    title = "Unregistered mystery"
    type_name = "mystery"  # deliberately off-catalog


def _build_probe_router() -> APIRouter:
    """Build the synthetic probe router that raises one error per type.

    Bundled as a standalone router so we can insert its routes ahead
    of the SPA catch-all (``/{full_path:path}``) that :func:`create_app`
    registers last. FastAPI appends routes when
    ``include_router`` is called, so a late call would still sit
    after the catch-all; we instead inject the collected ``Route``
    objects into ``app.router.routes`` before the catch-all index.
    """
    r = APIRouter()

    @r.get("/api/_probe/validation", include_in_schema=False)
    def probe_validation() -> None:
        raise Validation(
            "property_id must be provided",
            errors=[
                {
                    "loc": ["body", "property_id"],
                    "msg": "field required",
                    "type": "missing",
                }
            ],
        )

    @r.get("/api/_probe/not_found", include_in_schema=False)
    def probe_not_found() -> None:
        raise NotFound("task not found")

    @r.get("/api/_probe/conflict", include_in_schema=False)
    def probe_conflict() -> None:
        raise Conflict("etag mismatch")

    @r.get("/api/_probe/unauthorized", include_in_schema=False)
    def probe_unauthorized() -> None:
        raise Unauthorized("bearer token missing")

    @r.get("/api/_probe/forbidden", include_in_schema=False)
    def probe_forbidden() -> None:
        raise Forbidden("insufficient permissions")

    @r.get("/api/_probe/rate_limited", include_in_schema=False)
    def probe_rate_limited() -> None:
        raise RateLimited(
            "slow down",
            extra={"retry_after_seconds": 30},
        )

    @r.get("/api/_probe/upstream_unavailable", include_in_schema=False)
    def probe_upstream_unavailable() -> None:
        raise UpstreamUnavailable(
            "LLM timed out",
            extra={"upstream": "openrouter"},
        )

    @r.get("/api/_probe/idempotency_conflict", include_in_schema=False)
    def probe_idempotency_conflict() -> None:
        raise IdempotencyConflict(
            "idempotency key reused with a different body",
            extra={"idempotency_key": "abc-123"},
        )

    @r.get("/api/_probe/approval_required", include_in_schema=False)
    def probe_approval_required() -> None:
        raise ApprovalRequired(
            "01HXAPPRID",
            detail="agent action pending approval",
            expires_at="2026-04-21T12:00:00Z",
            extra={"card_summary": "Approve expense $5"},
        )

    @r.get("/api/_probe/internal", include_in_schema=False)
    def probe_internal() -> None:
        raise _UnregisteredError("surprise")

    return r


def _compose_app(db_url: str) -> FastAPI:
    """Compose the real app and inject throwaway routes per type.

    We add a synthetic ``/api/_probe/<kind>`` route set on the already-
    composed app so the full middleware chain — tenancy, security
    headers, exception handlers — sees each request. The routes go
    under ``/api/`` so the tenancy middleware's SKIP_PATHS treats
    them as bare-host API surface (no slug binding needed), and they
    are inserted BEFORE the SPA catch-all so the ``/{full_path:path}``
    matcher does not 404 them first.
    """
    app = create_app(settings=_pinned_settings(db_url))
    probe_router = _build_probe_router()

    # ``create_app`` registers the SPA catch-all last (its
    # ``/{full_path:path}`` matcher would swallow anything mounted
    # after it). We find its index and splice the probe routes in
    # right before it so the envelope seam runs for every probe.
    routes = app.router.routes
    catch_all_index = next(
        (
            idx
            for idx, route in enumerate(routes)
            if getattr(route, "path", None) == "/{full_path:path}"
        ),
        len(routes),
    )
    for offset, probe_route in enumerate(probe_router.routes):
        routes.insert(catch_all_index + offset, probe_route)
    return app


@pytest.fixture
def composed_client(db_url: str) -> Iterator[TestClient]:
    """TestClient over the full :func:`create_app` with test routes."""
    app = _compose_app(db_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _type_uri(short: str) -> str:
    return f"{CANONICAL_TYPE_BASE}{short}"


# ---------------------------------------------------------------------------
# Snapshot per canonical type
# ---------------------------------------------------------------------------


class TestCanonicalTypeSnapshots:
    """One request per canonical type; envelope shape pinned."""

    def test_validation(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/validation")
        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
        body = resp.json()
        assert body["type"] == _type_uri("validation")
        assert body["title"] == "Validation error"
        assert body["status"] == 422
        assert body["detail"] == "property_id must be provided"
        assert body["instance"] == "/api/_probe/validation"
        assert body["errors"] == [
            {
                "loc": ["body", "property_id"],
                "msg": "field required",
                "type": "missing",
            }
        ]

    def test_not_found(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/not_found")
        assert resp.status_code == 404
        body = resp.json()
        assert body == {
            "type": _type_uri("not_found"),
            "title": "Not found",
            "status": 404,
            "detail": "task not found",
            "instance": "/api/_probe/not_found",
        }

    def test_conflict(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/conflict")
        assert resp.status_code == 409
        body = resp.json()
        assert body["type"] == _type_uri("conflict")
        assert body["title"] == "Conflict"
        assert body["status"] == 409
        assert body["detail"] == "etag mismatch"
        assert body["instance"] == "/api/_probe/conflict"

    def test_unauthorized(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/unauthorized")
        assert resp.status_code == 401
        body = resp.json()
        assert body["type"] == _type_uri("unauthorized")
        assert body["title"] == "Unauthorized"
        assert body["detail"] == "bearer token missing"
        assert body["instance"] == "/api/_probe/unauthorized"

    def test_forbidden(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/forbidden")
        assert resp.status_code == 403
        body = resp.json()
        assert body["type"] == _type_uri("forbidden")
        assert body["title"] == "Forbidden"
        assert body["detail"] == "insufficient permissions"

    def test_rate_limited(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/rate_limited")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "30"
        body = resp.json()
        assert body["type"] == _type_uri("rate_limited")
        assert body["title"] == "Rate limited"
        assert body["status"] == 429
        assert body["detail"] == "slow down"
        assert body["retry_after_seconds"] == 30

    def test_upstream_unavailable(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/upstream_unavailable")
        assert resp.status_code == 502
        body = resp.json()
        assert body["type"] == _type_uri("upstream_unavailable")
        assert body["title"] == "Upstream unavailable"
        assert body["status"] == 502
        assert body["upstream"] == "openrouter"

    def test_idempotency_conflict(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/idempotency_conflict")
        assert resp.status_code == 409
        body = resp.json()
        assert body["type"] == _type_uri("idempotency_conflict")
        assert body["title"] == "Idempotency conflict"
        assert body["detail"] == ("idempotency key reused with a different body")
        assert body["idempotency_key"] == "abc-123"

    def test_approval_required(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/approval_required")
        assert resp.status_code == 409
        body = resp.json()
        assert body["type"] == _type_uri("approval_required")
        assert body["title"] == "Approval required"
        assert body["status"] == 409
        assert body["approval_request_id"] == "01HXAPPRID"
        assert body["expires_at"] == "2026-04-21T12:00:00Z"
        assert body["detail"] == "agent action pending approval"
        assert body["card_summary"] == "Approve expense $5"

    def test_internal_fallback(self, composed_client: TestClient) -> None:
        """Unknown DomainError subclass → 500 ``internal``."""
        resp = composed_client.get("/api/_probe/internal")
        assert resp.status_code == 500
        body = resp.json()
        assert body["type"] == _type_uri("internal")
        assert body["title"] == "Internal server error"
        assert body["status"] == 500
        assert body["instance"] == "/api/_probe/internal"


# ---------------------------------------------------------------------------
# Cross-cutting: correlation id propagation + content type on every type
# ---------------------------------------------------------------------------


_PROBE_PATHS: tuple[str, ...] = (
    "/api/_probe/validation",
    "/api/_probe/not_found",
    "/api/_probe/conflict",
    "/api/_probe/unauthorized",
    "/api/_probe/forbidden",
    "/api/_probe/rate_limited",
    "/api/_probe/upstream_unavailable",
    "/api/_probe/idempotency_conflict",
    "/api/_probe/approval_required",
    "/api/_probe/internal",
)


class TestCrossCuttingEnvelopeInvariants:
    """Every error response — regardless of type — obeys the envelope
    contract: problem+json content-type, instance path, and the
    inbound correlation id echoes on the way out.
    """

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_content_type_on_every_probe(
        self, composed_client: TestClient, path: str
    ) -> None:
        resp = composed_client.get(path)
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_correlation_id_echoed_on_every_probe(
        self, composed_client: TestClient, path: str
    ) -> None:
        """Inbound ``X-Correlation-Id`` survives the error-envelope path."""
        resp = composed_client.get(path, headers={"X-Correlation-Id": "01HXOBSERVABLE"})
        assert resp.headers.get("X-Correlation-Id") == "01HXOBSERVABLE"

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_instance_equals_request_path(
        self, composed_client: TestClient, path: str
    ) -> None:
        body = composed_client.get(path).json()
        assert body["instance"] == path

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_type_is_full_canonical_uri(
        self, composed_client: TestClient, path: str
    ) -> None:
        """Every rendered envelope carries a full URI under ``type``."""
        body = composed_client.get(path).json()
        assert body["type"].startswith(CANONICAL_TYPE_BASE)
