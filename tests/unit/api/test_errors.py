"""Unit tests for :mod:`app.api.errors` — the RFC 7807 problem+json seam.

Covers each arm of the exception dispatcher in isolation using a
throwaway FastAPI app that mounts ``add_exception_handlers`` on top
of two synthetic routes per scenario. We deliberately avoid the full
``create_app`` factory here so the tests stay fast and focus only on
the error-envelope contract — wider integration lives in
``tests/integration/api/test_error_envelope.py``.

Per spec §12 "Errors":

* Every response is ``Content-Type: application/problem+json``.
* ``type`` is the canonical URI ``https://crewday.dev/errors/<name>``.
* ``instance`` echoes the request path.
* ``X-Correlation-Id`` / ``X-Request-Id`` echo back when present.
* :class:`DomainError` subclass → known status map; unknown → 500
  ``internal``.
* :class:`pydantic.ValidationError` /
  :class:`fastapi.exceptions.RequestValidationError` → 422 with
  ``errors[]``.

See ``docs/specs/12-rest-api.md`` §"Errors" and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.api.errors import (
    CANONICAL_TYPE_BASE,
    CONTENT_TYPE_PROBLEM_JSON,
    add_exception_handlers,
)
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_raising(exc: Exception) -> FastAPI:
    """Build a minimal FastAPI app whose ``/boom`` route raises ``exc``."""
    app = FastAPI()

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise exc

    add_exception_handlers(app)
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _type_uri(short: str) -> str:
    return f"{CANONICAL_TYPE_BASE}{short}"


# ---------------------------------------------------------------------------
# DomainError subclass → status + type URI + title
# ---------------------------------------------------------------------------


class TestDomainErrorMapping:
    """Each DomainError subclass lands on the spec-pinned HTTP status,
    canonical ``type`` URI, and canonical ``title``.
    """

    @pytest.mark.parametrize(
        ("exc_cls", "expected_status", "expected_type", "expected_title"),
        [
            (Validation, 422, "validation", "Validation error"),
            (NotFound, 404, "not_found", "Not found"),
            (Conflict, 409, "conflict", "Conflict"),
            (
                IdempotencyConflict,
                409,
                "idempotency_conflict",
                "Idempotency conflict",
            ),
            (Unauthorized, 401, "unauthorized", "Unauthorized"),
            (Forbidden, 403, "forbidden", "Forbidden"),
            (RateLimited, 429, "rate_limited", "Rate limited"),
            (
                UpstreamUnavailable,
                502,
                "upstream_unavailable",
                "Upstream unavailable",
            ),
        ],
    )
    def test_subclass_mapping(
        self,
        exc_cls: type[DomainError],
        expected_status: int,
        expected_type: str,
        expected_title: str,
    ) -> None:
        client = _client(_app_raising(exc_cls("boom detail")))
        resp = client.get("/boom")
        assert resp.status_code == expected_status
        body = resp.json()
        assert body["type"] == _type_uri(expected_type)
        assert body["title"] == expected_title
        assert body["status"] == expected_status
        assert body["detail"] == "boom detail"
        assert body["instance"] == "/boom"
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)

    def test_detail_defaults_to_none(self) -> None:
        """Omitting ``detail`` omits the key entirely."""
        client = _client(_app_raising(NotFound()))
        body = client.get("/boom").json()
        assert "detail" not in body

    def test_errors_array_flows_through(self) -> None:
        """Service-raised ``errors`` propagate into ``errors[]``."""
        client = _client(
            _app_raising(
                Validation(
                    "bad",
                    errors=[
                        {
                            "loc": ["body", "property_id"],
                            "msg": "field required",
                            "type": "missing",
                        }
                    ],
                )
            )
        )
        body = client.get("/boom").json()
        assert body["errors"] == [
            {
                "loc": ["body", "property_id"],
                "msg": "field required",
                "type": "missing",
            }
        ]


class TestUnknownDomainErrorFallsBackToInternal:
    """A subclass the handler does not know about → 500 ``internal``.

    Mirrors the acceptance criterion "Unknown DomainError subclass
    falls back to 500 ``internal``" from cd-waq3.
    """

    def test_unknown_subclass_returns_500_internal(self) -> None:
        class MysteryError(DomainError):
            """Not registered in ``_DOMAIN_STATUS_MAP``."""

            title = "Mystery"
            type_name = "mystery"  # deliberately not a canonical name

        client = _client(_app_raising(MysteryError("out of band")))
        resp = client.get("/boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body["type"] == _type_uri("internal")
        assert body["title"] == "Internal server error"
        assert body["status"] == 500


class TestApprovalRequired:
    """``ApprovalRequired`` renders ``approval_request_id`` + optional
    ``expires_at`` inside the envelope body.
    """

    def test_body_includes_approval_request_id(self) -> None:
        client = _client(
            _app_raising(ApprovalRequired("01HXAPPRID", detail="awaiting approval"))
        )
        resp = client.get("/boom")
        assert resp.status_code == 409
        body = resp.json()
        assert body["type"] == _type_uri("approval_required")
        assert body["approval_request_id"] == "01HXAPPRID"
        assert "expires_at" not in body

    def test_body_includes_expires_at_when_provided(self) -> None:
        client = _client(
            _app_raising(
                ApprovalRequired(
                    "01HXAPPRID",
                    expires_at="2026-04-21T12:00:00Z",
                )
            )
        )
        body = client.get("/boom").json()
        assert body["approval_request_id"] == "01HXAPPRID"
        assert body["expires_at"] == "2026-04-21T12:00:00Z"

    def test_extra_cannot_overwrite_approval_keys(self) -> None:
        """``extra`` is advisory — reserved keys win."""
        client = _client(
            _app_raising(
                ApprovalRequired(
                    "01HXAPPRID",
                    extra={"approval_request_id": "attacker-supplied", "card": "ok"},
                )
            )
        )
        body = client.get("/boom").json()
        assert body["approval_request_id"] == "01HXAPPRID"
        assert body["card"] == "ok"


class TestRateLimitedRetryAfterHeader:
    """``retry_after_seconds`` in ``extra`` promotes to ``Retry-After``."""

    def test_retry_after_header_promoted(self) -> None:
        client = _client(
            _app_raising(RateLimited("slow down", extra={"retry_after_seconds": 42}))
        )
        resp = client.get("/boom")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "42"

    def test_missing_retry_after_omits_header(self) -> None:
        client = _client(_app_raising(RateLimited("slow down")))
        resp = client.get("/boom")
        assert "retry-after" not in {k.lower() for k in resp.headers}

    def test_non_numeric_retry_after_is_silently_dropped(self) -> None:
        """Bad values are best-effort hints; we do NOT 500 on them."""
        client = _client(
            _app_raising(RateLimited(extra={"retry_after_seconds": "soon"}))
        )
        resp = client.get("/boom")
        assert resp.status_code == 429
        assert "retry-after" not in {k.lower() for k in resp.headers}


# ---------------------------------------------------------------------------
# Validation error paths — FastAPI + pydantic
# ---------------------------------------------------------------------------


class _PayloadDTO(BaseModel):
    property_id: str
    count: int


def _app_with_validation_route() -> FastAPI:
    """App with a route that requires a :class:`_PayloadDTO` body.

    Used to trigger :class:`RequestValidationError` from FastAPI's
    body-parsing pipeline (the native validation arm).
    """
    app = FastAPI()

    @app.post("/v")
    def v(body: _PayloadDTO) -> dict[str, str]:
        return {"ok": "ok"}

    add_exception_handlers(app)
    return app


class TestValidationErrorPaths:
    """FastAPI / pydantic ValidationError → 422 with ``errors[]``."""

    def test_request_validation_error_produces_envelope(self) -> None:
        """Missing field triggers FastAPI's RequestValidationError."""
        client = _client(_app_with_validation_route())
        resp = client.post("/v", json={"property_id": "prop_x"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"] == _type_uri("validation")
        assert body["status"] == 422
        assert body["title"] == "Validation error"
        assert body["instance"] == "/v"
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
        assert isinstance(body["errors"], list)
        assert body["errors"], "errors[] must be non-empty"
        first = body["errors"][0]
        # Envelope keys are ``loc``/``msg``/``type`` only — spec §12.
        assert set(first) == {"loc", "msg", "type"}
        assert "count" in first["loc"]

    def test_bare_pydantic_validation_error(self) -> None:
        """A pydantic :class:`ValidationError` raised from a service
        also lands on the 422 envelope.
        """
        try:
            _PayloadDTO.model_validate({"property_id": "prop_x"})
        except ValidationError as exc:
            raised = exc
        else:  # pragma: no cover - must raise
            raise AssertionError("expected pydantic ValidationError")

        client = _client(_app_raising(raised))
        resp = client.get("/boom")
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"] == _type_uri("validation")
        assert body["instance"] == "/boom"
        assert body["errors"]
        assert "count" in body["errors"][0]["loc"]

    def test_raw_pydantic_inputs_are_not_echoed(self) -> None:
        """``input`` / ``ctx`` from pydantic's raw format are dropped
        — we never echo the caller's raw value into an error body
        (PII concern from §15).
        """
        client = _client(_app_with_validation_route())
        resp = client.post(
            "/v", json={"property_id": "prop_x", "count": "not-a-number"}
        )
        body = resp.json()
        for err in body["errors"]:
            assert "input" not in err
            assert "ctx" not in err
            assert "url" not in err


# ---------------------------------------------------------------------------
# HTTPException path — native Starlette/FastAPI exceptions
# ---------------------------------------------------------------------------


class TestHTTPExceptionEnvelope:
    """Native :class:`HTTPException` passes through the envelope map."""

    @pytest.mark.parametrize(
        ("status", "expected_type"),
        [
            (400, "validation"),
            (401, "unauthorized"),
            (403, "forbidden"),
            (404, "not_found"),
            (409, "conflict"),
            (429, "rate_limited"),
            (502, "upstream_unavailable"),
        ],
    )
    def test_known_status_maps_to_canonical_type(
        self, status: int, expected_type: str
    ) -> None:
        client = _client(_app_raising(HTTPException(status_code=status)))
        resp = client.get("/boom")
        assert resp.status_code == status
        body = resp.json()
        assert body["type"] == _type_uri(expected_type)
        assert body["instance"] == "/boom"
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)

    def test_unknown_status_falls_back_to_http_prefix(self) -> None:
        client = _client(_app_raising(HTTPException(status_code=418)))
        body = client.get("/boom").json()
        assert body["type"] == _type_uri("http_418")
        assert body["status"] == 418

    def test_caller_detail_flows_through(self) -> None:
        client = _client(
            _app_raising(HTTPException(status_code=404, detail="missing row"))
        )
        body = client.get("/boom").json()
        assert body["detail"] == "missing row"

    def test_default_detail_equals_title_is_suppressed(self) -> None:
        """FastAPI defaults ``detail = HTTPStatus.phrase`` — avoid the
        duplicate ``detail == title`` in the envelope body.
        """
        client = _client(_app_raising(HTTPException(status_code=404)))
        body = client.get("/boom").json()
        assert body["title"] == "Not found"
        assert "detail" not in body

    def test_caller_headers_preserved(self) -> None:
        """``HTTPException(headers=...)`` stays on the response."""
        client = _client(
            _app_raising(
                HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
            )
        )
        resp = client.get("/boom")
        assert resp.headers["WWW-Authenticate"] == "Bearer"


# ---------------------------------------------------------------------------
# Content-Type + correlation headers + instance
# ---------------------------------------------------------------------------


class TestEnvelopeHeaders:
    """Content-Type, correlation id, and instance path invariants."""

    def test_content_type_is_problem_json(self) -> None:
        client = _client(_app_raising(NotFound()))
        resp = client.get("/boom")
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)

    def test_instance_echoes_request_path(self) -> None:
        """``instance`` is the path the caller actually hit."""
        app = FastAPI()

        @app.get("/deep/path/here")
        def boom() -> dict[str, str]:
            raise NotFound()

        add_exception_handlers(app)
        body = _client(app).get("/deep/path/here").json()
        assert body["instance"] == "/deep/path/here"

    def test_correlation_id_echoed_from_x_correlation_id(self) -> None:
        client = _client(_app_raising(NotFound()))
        resp = client.get("/boom", headers={"X-Correlation-Id": "01HXCORRELATION"})
        assert resp.headers["X-Correlation-Id"] == "01HXCORRELATION"
        assert resp.headers["X-Request-Id"] == "01HXCORRELATION"

    def test_correlation_id_echoed_from_x_request_id(self) -> None:
        client = _client(_app_raising(NotFound()))
        resp = client.get("/boom", headers={"X-Request-Id": "01HXREQID"})
        assert resp.headers["X-Request-Id"] == "01HXREQID"
        assert resp.headers["X-Correlation-Id"] == "01HXREQID"

    def test_x_correlation_id_wins_over_x_request_id(self) -> None:
        """Spec-named header takes precedence when both are present."""
        client = _client(_app_raising(NotFound()))
        resp = client.get(
            "/boom",
            headers={
                "X-Correlation-Id": "spec-name",
                "X-Request-Id": "internal-name",
            },
        )
        assert resp.headers["X-Correlation-Id"] == "spec-name"
        assert resp.headers["X-Request-Id"] == "spec-name"

    def test_no_correlation_header_when_request_has_none(self) -> None:
        client = _client(_app_raising(NotFound()))
        resp = client.get("/boom")
        lower = {k.lower() for k in resp.headers}
        assert "x-correlation-id" not in lower
        assert "x-request-id" not in lower


class TestExtraFieldBehaviour:
    """Extension fields in ``extra`` can't shadow reserved keys."""

    def test_extra_cannot_overwrite_reserved(self) -> None:
        client = _client(
            _app_raising(
                NotFound(
                    "missing",
                    extra={"type": "attacker", "status": 200, "harmless": "ok"},
                )
            )
        )
        body = client.get("/boom").json()
        assert body["type"] == _type_uri("not_found")
        assert body["status"] == 404
        assert body["harmless"] == "ok"
