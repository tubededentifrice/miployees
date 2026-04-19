"""Short-lived signed token helpers.

Used for guest-welcome pages (`/w/<slug>/guest/<token>`, §04) and magic
links (§03). Wraps :class:`itsdangerous.URLSafeTimedSerializer` so
callers never import or catch ``itsdangerous`` exceptions directly; the
adapter contract is ours.

The name of the module is historical ("hashids" was the original PyPI
lib we considered) — kept for the public import path agreed in §01's
shared-kernel layout. The implementation is ``itsdangerous``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from itsdangerous import URLSafeTimedSerializer
from itsdangerous.exc import BadData, SignatureExpired

__all__ = ["TokenExpired", "TokenInvalid", "decode_token", "encode_token"]


class TokenInvalid(Exception):
    """Raised when a token is malformed, tampered with, or mis-signed."""


class TokenExpired(TokenInvalid):
    """Raised when a token's age exceeds ``max_age_seconds``.

    Inherits :class:`TokenInvalid` so callers that only care about
    "unusable token" can catch a single exception; callers that want to
    distinguish "expired" from "tampered" catch :class:`TokenExpired`
    first.
    """


def encode_token(
    payload: Mapping[str, Any],
    secret: str,
    salt: str,
) -> str:
    """Sign ``payload`` and return a URL-safe, time-stamped token.

    ``secret`` is the app-wide signing key (injected from config, never
    hard-coded here). ``salt`` namespaces tokens — reuse the same
    string on both sides of a given flow (e.g. ``"guest-welcome"``).
    """
    serializer = URLSafeTimedSerializer(secret, salt=salt)
    # dict() materialises the Mapping so itsdangerous can JSON-encode it.
    return serializer.dumps(dict(payload))


def decode_token(
    token: str,
    secret: str,
    salt: str,
    max_age_seconds: int,
) -> dict[str, Any]:
    """Verify and decode a token produced by :func:`encode_token`.

    Raises :class:`TokenExpired` if the token is older than
    ``max_age_seconds``; :class:`TokenInvalid` for every other failure
    mode (bad signature, wrong salt, garbled payload, wrong shape).
    """
    serializer = URLSafeTimedSerializer(secret, salt=salt)
    try:
        decoded = serializer.loads(token, max_age=max_age_seconds)
    except SignatureExpired as exc:
        raise TokenExpired(str(exc)) from exc
    except BadData as exc:
        raise TokenInvalid(str(exc)) from exc

    if not isinstance(decoded, dict):
        raise TokenInvalid(
            f"expected JSON object payload, got {type(decoded).__name__}"
        )
    # Narrow key type: JSON object keys are always strings.
    return {str(k): v for k, v in decoded.items()}
