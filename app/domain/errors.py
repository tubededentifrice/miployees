"""Domain error hierarchy — the shape services raise, the HTTP layer translates.

Services raise :class:`DomainError` subclasses; the transport-agnostic
HTTP seam in :mod:`app.api.errors` translates each subclass into an
RFC 7807 ``problem+json`` envelope with the canonical ``type`` URIs
listed in ``docs/specs/12-rest-api.md`` §"Errors":

* :class:`Validation` → ``validation``
* :class:`NotFound` → ``not_found``
* :class:`Conflict` → ``conflict``
* :class:`Unauthorized` → ``unauthorized``
* :class:`Forbidden` → ``forbidden``
* :class:`RateLimited` → ``rate_limited``
* :class:`UpstreamUnavailable` → ``upstream_unavailable``
* :class:`ApprovalRequired` → ``approval_required``

Subclasses deliberately do **not** know their HTTP status code — the
API translator in :mod:`app.api.errors` owns that mapping. Keeping
statuses at the transport layer lets the same exception serve a CLI
or worker process without pulling ``starlette.status`` into the
domain.

``DomainError`` carries three optional payload slots that flow
straight into the problem+json body:

* ``detail``: human-facing one-liner, placed under ``detail``.
* ``errors``: field-level error list (RFC 7807 extension), placed
  under ``errors``. Each entry should match
  ``{"loc": [...], "msg": str, "type": str}``.
* ``extra``: arbitrary extension fields merged into the envelope
  body. Use for structured context the UI needs to render the error
  (``approval_request_id``, ``expires_at``, ``idempotency_key``,
  ``conflicting_hash``, …).

This module has no downstream dependency on FastAPI, Starlette, or
Pydantic — importing it from a worker, CLI, or test harness is safe.

See also:

* ``docs/specs/12-rest-api.md`` §"Errors" — the canonical envelope.
* ``docs/specs/11-llm-and-agents.md`` §"Approval pipeline" — why
  ``ApprovalRequired`` carries ``approval_request_id``.
* ``app/api/errors.py`` — the HTTP seam (status map + envelope).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Final

__all__ = [
    "ApprovalRequired",
    "Conflict",
    "DomainError",
    "Forbidden",
    "IdempotencyConflict",
    "NotFound",
    "RateLimited",
    "Unauthorized",
    "UpstreamUnavailable",
    "Validation",
]


# Canonical problem+json ``type`` URIs live in spec §12 "Errors". The
# HTTP seam appends these short names to this base to build the full
# URI. Kept as a module constant so a rename lands in one place; the
# seam imports it directly.
CANONICAL_TYPE_BASE: Final[str] = "https://crewday.dev/errors/"


class DomainError(Exception):
    """Base class for every domain-layer signal that crosses the HTTP seam.

    Subclasses set :attr:`title` (a short, human-readable summary
    suitable for the RFC 7807 ``title`` field) and :attr:`type_name`
    (the canonical short-name key used by the HTTP seam to build the
    full ``type`` URI). Both are class-level so an instance without
    constructor arguments still carries the right metadata.

    :param detail: human-facing one-liner, RFC 7807 ``detail``.
    :param errors: optional field-level error list. Each entry is a
        mapping with at least ``loc``, ``msg`` and ``type`` — matches
        :meth:`pydantic.ValidationError.errors` output.
    :param extra: optional extension fields merged into the envelope
        body. The HTTP seam writes these *after* the standard keys,
        so they cannot shadow ``type``/``title``/``status``.
    """

    # Overridden by every concrete subclass. The base values here are
    # load-bearing: a non-DomainError caught by a generic handler is
    # rendered as 500 ``internal``, but a DomainError subclass that
    # forgets to override either attribute should still land in a
    # grep-able place instead of silently degrading.
    title: ClassVar[str] = "Internal server error"
    type_name: ClassVar[str] = "internal"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: Sequence[Mapping[str, object]] | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        # ``super().__init__`` records the detail on ``args`` so
        # ``repr(exc)`` and logging formatters produce useful output
        # even before the HTTP seam wraps the payload.
        super().__init__(detail or self.title)
        self.detail: str | None = detail
        # Materialise into immutable tuples/dicts so callers who
        # mutate the inputs after raising cannot retroactively change
        # the envelope body.
        self.errors: tuple[Mapping[str, object], ...] = (
            tuple(errors) if errors is not None else ()
        )
        self.extra: dict[str, object] = dict(extra) if extra is not None else {}


class Validation(DomainError):
    """Request or payload failed validation. HTTP 422.

    Prefer raising pydantic ``ValidationError`` where possible — the
    HTTP seam handles it natively with ``errors[]`` derived from
    :meth:`pydantic.ValidationError.errors`. Use this class when the
    invalid state is discovered below the DTO layer (e.g. a service
    rejects a cross-field combination the DTO alone cannot encode).
    """

    title: ClassVar[str] = "Validation error"
    type_name: ClassVar[str] = "validation"


class NotFound(DomainError):
    """Entity does not exist, or the caller cannot see it. HTTP 404.

    Per spec §01 "Workspace addressing" we deliberately use 404 (not
    403) when a caller lacks tenancy membership so the tenant surface
    is not enumerable. Service code that knows the caller is
    unauthorised for a visible entity should raise :class:`Forbidden`
    instead — tenancy is the one case where the two collapse.
    """

    title: ClassVar[str] = "Not found"
    type_name: ClassVar[str] = "not_found"


class Conflict(DomainError):
    """Request conflicts with current server state. HTTP 409.

    Use for optimistic-concurrency, soft-delete-in-use, and any state
    transition the current row cannot make (e.g. clocking in when an
    open shift already exists). Field-level amendments belong on
    :attr:`Validation` instead.
    """

    title: ClassVar[str] = "Conflict"
    type_name: ClassVar[str] = "conflict"


class IdempotencyConflict(Conflict):
    """Idempotency-Key replay with a different body hash. HTTP 409.

    Distinct from the generic :class:`Conflict` so the ``type`` URI
    identifies the specific failure mode spec §12 "Idempotency" calls
    out. Callers should populate ``extra`` with ``idempotency_key``
    and the previously stored response summary so the client can
    retry with a fresh key deterministically.
    """

    title: ClassVar[str] = "Idempotency conflict"
    type_name: ClassVar[str] = "idempotency_conflict"


class Unauthorized(DomainError):
    """Caller is not authenticated. HTTP 401.

    Distinct from :class:`Forbidden` — ``401`` tells the client
    "re-authenticate", ``403`` tells the client "you cannot do this
    with your current identity". Services that cannot tell whether
    the caller *is* authenticated (because the middleware already
    rejected the bearer) should let the middleware respond rather
    than raising here.
    """

    title: ClassVar[str] = "Unauthorized"
    type_name: ClassVar[str] = "unauthorized"


class Forbidden(DomainError):
    """Caller is authenticated but lacks the required capability. HTTP 403.

    Fired by :mod:`app.authz` when an action-catalog verb fails for
    the resolved principal. Payload should NOT name the verb the user
    would need — that's an information leak. Instead let the message
    generalise (``"Insufficient permissions for this action"``) and
    log the specific verb server-side.
    """

    title: ClassVar[str] = "Forbidden"
    type_name: ClassVar[str] = "forbidden"


class RateLimited(DomainError):
    """Caller is rate limited. HTTP 429.

    Populate ``extra`` with ``retry_after_seconds`` when a numeric
    hint is known. The HTTP seam additionally stamps a
    ``Retry-After`` header from that hint when present — spec §12
    "Rate limiting".
    """

    title: ClassVar[str] = "Rate limited"
    type_name: ClassVar[str] = "rate_limited"


class UpstreamUnavailable(DomainError):
    """A required upstream (LLM, SMTP, payment gateway, …) is down. HTTP 502.

    Use for dependency failures where retrying the same request later
    stands a reasonable chance of succeeding. Populate ``extra`` with
    ``upstream`` naming the failing component so the UI can tell the
    user which dependency is down.
    """

    title: ClassVar[str] = "Upstream unavailable"
    type_name: ClassVar[str] = "upstream_unavailable"


class ApprovalRequired(DomainError):
    """A delegated-token agent action is pending operator approval. HTTP 409.

    Spec §11 "Approval pipeline" models this as a 202-like state for
    the agent but a blocking 409 for the underlying caller — the row
    is not yet written, the ``approval_request_id`` is the handle the
    client polls for resolution. See spec §12 "Errors" for the
    canonical shape and §11 for the end-to-end flow.

    :param approval_request_id: the ULID of the pending
        ``agent_action`` row. Rendered under ``approval_request_id``
        in the envelope body.
    :param expires_at: optional RFC 3339 UTC timestamp after which
        the approval row expires. Rendered under ``expires_at``.

    Any further context (the summary card, the inline channel, …)
    should be passed via ``extra``; those fields flow into the body
    verbatim next to ``approval_request_id``.
    """

    title: ClassVar[str] = "Approval required"
    type_name: ClassVar[str] = "approval_required"

    def __init__(
        self,
        approval_request_id: str,
        *,
        detail: str | None = None,
        expires_at: str | None = None,
        errors: Sequence[Mapping[str, object]] | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        # Build the ``extra`` payload with the approval-specific keys
        # first so a caller-supplied ``extra`` cannot clobber them.
        # ``expires_at`` is only written when present — a missing
        # expiry is legitimate (e.g. for approvals with no TTL).
        merged: dict[str, object] = {"approval_request_id": approval_request_id}
        if expires_at is not None:
            merged["expires_at"] = expires_at
        if extra is not None:
            for key, value in extra.items():
                # ``approval_request_id`` / ``expires_at`` are the
                # contract — silently refuse to overwrite them rather
                # than surprise a caller who passed ``extra=request.dict()``.
                if key in merged:
                    continue
                merged[key] = value
        super().__init__(detail, errors=errors, extra=merged)
        self.approval_request_id: str = approval_request_id
        self.expires_at: str | None = expires_at
