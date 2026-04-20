"""HTTP middleware shared across the API surface.

Each middleware is a thin, single-purpose wrapper around
:class:`starlette.middleware.base.BaseHTTPMiddleware`. Concrete modules:

* :mod:`app.api.middleware.security_headers` — strict CSP with a
  per-request nonce, HSTS (opt-in), Permissions-Policy with a
  worker-route carve-out, and the rest of the §15 header set.
* :mod:`app.api.middleware.idempotency` — persisted replay cache for
  ``POST`` + ``Idempotency-Key`` retries (spec §12 "Idempotency").

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers",
``docs/specs/12-rest-api.md`` §"Idempotency".
"""

from __future__ import annotations

from app.api.middleware.idempotency import (
    IdempotencyMiddleware,
    prune_expired_idempotency_keys,
)
from app.api.middleware.security_headers import (
    SecurityHeadersMiddleware,
    build_csp_header,
    build_permissions_policy,
    generate_csp_nonce,
)

__all__ = [
    "IdempotencyMiddleware",
    "SecurityHeadersMiddleware",
    "build_csp_header",
    "build_permissions_policy",
    "generate_csp_nonce",
    "prune_expired_idempotency_keys",
]
