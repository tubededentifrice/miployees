"""Small HMAC helpers for chat gateway adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping


def header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def hmac_hex(secret: str, body: bytes, *, digest: str = "sha256") -> str:
    algorithm = getattr(hashlib, digest)
    return hmac.new(secret.encode("utf-8"), body, algorithm).hexdigest()


def hmac_base64(secret: str, body: bytes, *, digest: str = "sha1") -> str:
    algorithm = getattr(hashlib, digest)
    mac = hmac.new(secret.encode("utf-8"), body, algorithm).digest()
    return base64.b64encode(mac).decode("ascii")


def compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
