"""RFC 7807 ``problem+json`` error envelope — the HTTP seam for domain errors.

:func:`add_exception_handlers` registers three handlers on a
:class:`fastapi.FastAPI` that together ensure every error response
carries the spec §12 "Errors" envelope:

* :class:`app.domain.errors.DomainError` subclasses → status + ``type``
  URI + title map. Unknown subclasses fall back to 500 ``internal``.
* :class:`fastapi.exceptions.RequestValidationError` and
  :class:`pydantic.ValidationError` → 422 ``validation`` with an
  ``errors[]`` array derived from :meth:`ValidationError.errors`.
* :class:`starlette.exceptions.HTTPException` → status from the
  exception, ``type`` URI chosen from the HTTP-status map where one
  exists (``404 → not_found``, ``401 → unauthorized``, …) or a
  generic ``http_<status>`` URI otherwise.

Every rendered response:

* sets ``Content-Type: application/problem+json``;
* echoes the inbound correlation header (``X-Correlation-Id`` or
  ``X-Request-Id``) on its way out, so a chained caller's
  distributed-tracing thread stays intact;
* sets ``instance`` to the request path so operators pairing a
  problem+json payload with a log line don't have to cross-reference
  access logs.

Migration is deliberately out of scope: the existing routers still
raise ``LookupError`` / ``ValueError`` / ``PermissionError`` and the
default Starlette/FastAPI handlers continue to translate native
:class:`HTTPException`. Follow-up Beads tasks convert service modules
to the :class:`DomainError` hierarchy; landing the seam first lets
those conversions ship as small, reviewable diffs.

See ``docs/specs/12-rest-api.md`` §"Errors" for the envelope spec,
``docs/specs/11-llm-and-agents.md`` §"Approval pipeline" for the
``approval_required`` case, and :mod:`app.domain.errors` for the
exception hierarchy itself.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.domain.errors import (
    CANONICAL_TYPE_BASE,
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
from app.domain.identity.permission_groups import LastOwnerMember

__all__ = [
    "CONTENT_TYPE_PROBLEM_JSON",
    "CORRELATION_HEADERS",
    "add_exception_handlers",
    "problem_response",
]

_log = logging.getLogger(__name__)


# RFC 7807 media type. A single module constant so a typo only bites
# once; the envelope tests pin this exact string.
CONTENT_TYPE_PROBLEM_JSON: Final[str] = "application/problem+json"

# Correlation headers we pass through verbatim on error responses.
# ``X-Correlation-Id`` is the spec §12 "Agent audit headers" name;
# ``X-Request-Id`` is the in-process alias the tenancy middleware
# emits (see :data:`app.tenancy.middleware.CORRELATION_ID_HEADER`).
# Either can arrive on a request; both are echoed so a caller that
# set ``X-Correlation-Id`` doesn't have to know the internal spelling.
CORRELATION_HEADERS: Final[tuple[str, ...]] = ("X-Correlation-Id", "X-Request-Id")


# DomainError subclass → HTTP status. Spec §12 "Errors" pins each of
# these; keeping the map right here (not on the exception classes)
# means a CLI or worker raising the same exception stays transport-
# agnostic. The keys are class objects, not names, so a typo surfaces
# at import time rather than at first 500.
_DOMAIN_STATUS_MAP: Final[dict[type[DomainError], int]] = {
    Validation: 422,
    LastOwnerMember: 422,
    NotFound: 404,
    Conflict: 409,
    IdempotencyConflict: 409,
    Unauthorized: 401,
    Forbidden: 403,
    RateLimited: 429,
    UpstreamUnavailable: 502,
    ApprovalRequired: 409,
}


# HTTP status → canonical type short-name. Used by the
# :class:`StarletteHTTPException` handler when it can identify a
# known status; unknown statuses fall back to ``http_<status>``. The
# values are the *short* names — the envelope builder prepends
# :data:`CANONICAL_TYPE_BASE` for the full URI.
_HTTP_STATUS_TYPE_MAP: Final[dict[int, str]] = {
    400: "validation",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation",
    429: "rate_limited",
    502: "upstream_unavailable",
}


# Default RFC 7807 ``title`` for each HTTP status we commonly emit.
# Keeps the envelope body human-readable without forcing every
# ``raise HTTPException`` site to spell out its own title. Unknown
# statuses fall back to the generic ``HTTPStatus.phrase`` lookup in
# :func:`_http_title`.
_HTTP_STATUS_TITLE_MAP: Final[dict[int, str]] = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    422: "Validation error",
    429: "Rate limited",
    500: "Internal server error",
    502: "Upstream unavailable",
}


def _type_uri(short_name: str) -> str:
    """Return the full canonical ``type`` URI for a short name."""
    return f"{CANONICAL_TYPE_BASE}{short_name}"


def _http_title(status: int) -> str:
    """Resolve the RFC 7807 ``title`` for an arbitrary HTTP status.

    Prefers the hand-curated :data:`_HTTP_STATUS_TITLE_MAP` so common
    cases match the :class:`DomainError` titles verbatim; falls back
    to :class:`http.HTTPStatus` for anything else. Returns the bare
    string ``"HTTP error"`` when the status is not recognised at all
    (e.g. a custom ``418`` an upstream handler chose to use) so the
    envelope still carries a non-empty title.
    """
    if status in _HTTP_STATUS_TITLE_MAP:
        return _HTTP_STATUS_TITLE_MAP[status]
    from http import HTTPStatus

    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "HTTP error"


def _sanitize_header_value(value: str) -> str:
    """Strip characters that would break HTTP header serialisation.

    CRLF (``\\r``, ``\\n``) and NUL (``\\x00``) are illegal in HTTP/1.1
    header values per RFC 7230 §3.2. When echoing caller-supplied
    values back on the response (correlation-id echo), a value that
    survived a lenient inbound transport would cause the outbound
    ``h11`` serialiser to abort the connection with a
    ``LocalProtocolError`` — effectively a DoS for that request.
    Stripping them here is the defence-in-depth guard.
    """
    return value.translate({ord("\r"): None, ord("\n"): None, ord("\x00"): None})


def _correlation_id(request: Request) -> str | None:
    """Return the first inbound correlation header value, if any.

    ``X-Correlation-Id`` takes precedence over ``X-Request-Id`` so a
    caller that set the spec-level name gets that value back — the
    two fall back to each other (§12 "Agent audit headers"). Case-
    insensitive because Starlette's :class:`Headers` already is.

    The returned value is sanitized by :func:`_sanitize_header_value`
    before being placed in the outbound response headers.
    """
    for header in CORRELATION_HEADERS:
        value = request.headers.get(header)
        if value:
            return _sanitize_header_value(value)
    return None


def problem_response(
    request: Request,
    *,
    status: int,
    type_name: str,
    title: str,
    detail: str | None = None,
    errors: tuple[dict[str, object], ...] | None = None,
    extra: dict[str, object] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the final :class:`JSONResponse` for a problem+json envelope.

    Split out so the three handler flavours share the same header +
    body-assembly path. ``type_name`` is the short canonical key
    (``validation``, ``not_found``, …); the full URI is built here.

    The caller passes field-level ``errors`` already shaped per
    §12 "Errors" (``{"loc": [...], "msg": str, "type": str}``).
    ``extra`` flows into the body *after* the standard keys so a
    buggy caller cannot accidentally stomp ``type`` / ``title`` /
    ``status`` / ``instance`` / ``detail`` / ``errors``.
    """
    body: dict[str, object] = {
        "type": _type_uri(type_name),
        "title": title,
        "status": status,
        "instance": str(request.url.path),
    }
    if detail is not None:
        body["detail"] = detail
    if errors:
        body["errors"] = list(errors)
    if extra:
        # Reserved keys cannot be overridden via ``extra``. Silent
        # skip rather than raise because ``extra`` is free-form by
        # contract; the extra-key tests pin this behaviour.
        reserved = {"type", "title", "status", "instance", "detail", "errors"}
        for key, value in extra.items():
            if key in reserved:
                continue
            body[key] = value

    headers: dict[str, str] = {"content-type": CONTENT_TYPE_PROBLEM_JSON}
    correlation = _correlation_id(request)
    if correlation is not None:
        # Echo both canonical header names so downstream callers that
        # only know one of them still see the id. The ``X-Request-Id``
        # echo matches the tenancy middleware's outgoing spelling; the
        # ``X-Correlation-Id`` echo matches the spec §12 name.
        for header in CORRELATION_HEADERS:
            headers[header] = correlation
    if extra_headers:
        headers.update(extra_headers)

    return JSONResponse(status_code=status, content=body, headers=headers)


def _format_validation_errors(
    raw: list[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Reshape pydantic errors into the envelope's ``errors[]`` items.

    Pydantic v2 emits ``{"type", "loc", "msg", "input", "ctx", "url"}``
    per item; spec §12 "Errors" only contracts ``loc``/``msg``/``type``.
    We keep those three verbatim and drop the rest to avoid leaking
    raw inputs (which may contain PII) into error responses — a
    common mistake when the naive "pass ``exc.errors()`` through"
    pattern is used.
    """
    out: list[dict[str, object]] = []
    for item in raw:
        loc_value = item.get("loc")
        # Pydantic v2 guarantees ``loc`` is a tuple; the fallback to
        # ``[]`` defends against a hand-crafted pydantic-shaped mapping
        # from a service that might deviate from the contract.
        loc_list: list[object] = (
            list(loc_value) if isinstance(loc_value, tuple | list) else []
        )
        entry: dict[str, object] = {
            "loc": loc_list,
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        out.append(entry)
    return tuple(out)


def _handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Render a :class:`DomainError` subclass as a problem+json response.

    Unknown subclasses (a new :class:`DomainError` added without a
    status-map entry) fall back to 500 with ``type=internal`` so the
    response is still shaped per §12 — a loud 500 is better than a
    silent bypass to the default error handler.
    """
    status = _DOMAIN_STATUS_MAP.get(type(exc))
    if status is None:
        _log.error(
            "unmapped domain error",
            extra={
                "event": "problem_json.unmapped_domain_error",
                "exception_type": type(exc).__name__,
            },
        )
        return problem_response(
            request,
            status=500,
            type_name="internal",
            title="Internal server error",
            detail=None,
        )

    extra_headers: dict[str, str] | None = None
    if isinstance(exc, RateLimited):
        # ``retry_after_seconds`` in ``extra`` lifts into a
        # ``Retry-After`` header (§12 "Rate limiting"). We accept any
        # number-compatible value and stringify it; bad types are
        # dropped rather than raising — the caller's contract is
        # "best-effort hint", not "must be int".
        retry_after = exc.extra.get("retry_after_seconds")
        if isinstance(retry_after, int | float) and not isinstance(retry_after, bool):
            extra_headers = {"Retry-After": str(int(retry_after))}

    return problem_response(
        request,
        status=status,
        type_name=exc.type_name,
        title=exc.title,
        detail=exc.detail,
        errors=tuple(dict(e) for e in exc.errors) if exc.errors else None,
        extra=dict(exc.extra) if exc.extra else None,
        extra_headers=extra_headers,
    )


def _handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render a FastAPI :class:`RequestValidationError` as 422 ``validation``.

    Wraps :func:`_format_validation_errors` so the ``errors[]`` shape
    matches spec §12. ``exc.errors()`` is already the list form; we
    rely on pydantic v2's stable contract there.
    """
    return problem_response(
        request,
        status=422,
        type_name="validation",
        title="Validation error",
        detail="Request validation failed",
        errors=_format_validation_errors([dict(e) for e in exc.errors()]),
    )


def _handle_pydantic_validation_error(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """Render a bare pydantic :class:`ValidationError` as 422 ``validation``.

    Same envelope as :func:`_handle_request_validation_error`; split
    so FastAPI's explicit ``RequestValidationError`` (which wraps the
    pydantic one with request-location metadata) stays the primary
    handler and bare pydantic errors raised inside a service only
    match this fallback.
    """
    return problem_response(
        request,
        status=422,
        type_name="validation",
        title="Validation error",
        detail="Request validation failed",
        errors=_format_validation_errors([dict(e) for e in exc.errors()]),
    )


def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render a Starlette :class:`HTTPException` as problem+json.

    Chooses the ``type`` short-name from :data:`_HTTP_STATUS_TYPE_MAP`
    where possible, otherwise falls back to ``http_<status>`` so every
    response still carries a stable URI. FastAPI's built-in
    :class:`HTTPException` subclasses :class:`StarletteHTTPException`
    so a single handler covers both.
    """
    status = exc.status_code
    type_name = _HTTP_STATUS_TYPE_MAP.get(status, f"http_{status}")
    title = _http_title(status)
    # ``exc.detail`` is whatever the caller passed to ``HTTPException``.
    # FastAPI defaults it to :class:`HTTPStatus.phrase` (e.g. ``"Not
    # Found"``) when omitted, which would duplicate ``title``; only
    # render it when the caller set a distinct value. Compare
    # case-insensitively to handle the spec's lowercase titles
    # (``"Not found"``) vs stdlib's title-case phrases (``"Not Found"``).
    detail_value: str | None
    if exc.detail is None:
        detail_value = None
    else:
        detail_value = str(exc.detail)
        if detail_value.casefold() == title.casefold():
            detail_value = None

    extra_headers: dict[str, str] | None = None
    if exc.headers:
        # Preserve caller-set headers (``Retry-After``, ``Location``, …)
        # — the envelope body additions take precedence, but any
        # already-set header the handler code chose stays on the
        # response.
        extra_headers = dict(exc.headers)

    return problem_response(
        request,
        status=status,
        type_name=type_name,
        title=title,
        detail=detail_value,
        extra_headers=extra_headers,
    )


def add_exception_handlers(app: FastAPI) -> None:
    """Register the three problem+json handlers on ``app``.

    Call from :func:`app.api.factory.create_app` after the context
    routers are mounted so every route inherits the envelope. A
    single registration per exception type is intentional — FastAPI
    dispatches via ``isinstance`` so the :class:`DomainError` handler
    also catches every subclass listed in :data:`_DOMAIN_STATUS_MAP`.
    """

    # FastAPI's ``add_exception_handler`` expects a handler callable
    # typed as ``(Request, Exception) -> Response``. Our specialised
    # handlers take narrower exception types for clarity, so we wrap
    # them in thin adapters that assert the narrowing. The ``assert``
    # is load-bearing only in the development assertion sense — the
    # ``isinstance`` dispatch FastAPI performs already guarantees the
    # cast is safe, so an ``AssertionError`` here would mean FastAPI
    # itself regressed.

    async def on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    async def on_request_validation_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return _handle_request_validation_error(request, exc)

    async def on_pydantic_validation_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        assert isinstance(exc, ValidationError)
        return _handle_pydantic_validation_error(request, exc)

    async def on_http_exception(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return _handle_http_exception(request, exc)

    app.add_exception_handler(DomainError, on_domain_error)
    app.add_exception_handler(RequestValidationError, on_request_validation_error)
    app.add_exception_handler(ValidationError, on_pydantic_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_exception)
