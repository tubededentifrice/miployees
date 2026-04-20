"""Shared deployment-wide abuse / throttle primitives.

This package hosts the cross-feature pieces of the abuse-mitigation
toolkit that multiple auth surfaces share:

* :mod:`app.abuse.throttle` — :class:`ShieldStore` +
  :func:`throttle` decorator for per-scope sliding-window rate
  limiting. Used by :mod:`app.api.v1.auth.passkey` for the 10/min/IP
  login-begin cap (§15 "Rate limiting and abuse controls"). Other
  surfaces (magic-link send, signup start, recovery start) keep
  their feature-specific :class:`~app.auth._throttle.Throttle`
  buckets for now; a full migration is tracked as a follow-up (see
  the module docstring in :mod:`app.auth._throttle`).
* :mod:`app.abuse.data.disposable_domains` (file) — the curated
  disposable-email blocklist, with a leading ``# generated YYYY-MM-DD``
  freshness pin the unit tests check.

Both :class:`ShieldStore` and :func:`throttle` are re-exported at
the package surface so callers can write
``from app.abuse import ShieldStore, throttle`` rather than reaching
through the internal ``app.abuse.throttle`` module. The internal
module path remains importable for callers that need to alias
``throttle`` to avoid shadowing a local ``throttle`` variable.

See ``docs/specs/15-security-privacy.md`` §"Rate limiting and abuse
controls" + §"Self-serve abuse mitigations" for the spec intent.
"""

from __future__ import annotations

from app.abuse.throttle import ShieldStore, throttle

__all__ = ["ShieldStore", "throttle"]
