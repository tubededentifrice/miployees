"""Tests for :mod:`app.util.hashids`."""

from __future__ import annotations

import time

import pytest

from app.util.hashids import (
    TokenExpired,
    TokenInvalid,
    decode_token,
    encode_token,
)

_SECRET = "test-secret-do-not-use-in-production"
_SALT = "unit-test"


class TestRoundTrip:
    def test_encode_decode(self) -> None:
        payload = {"stay_id": "abc", "role": "guest"}
        token = encode_token(payload, _SECRET, _SALT)
        assert isinstance(token, str)
        decoded = decode_token(token, _SECRET, _SALT, max_age_seconds=60)
        assert decoded == payload

    def test_token_is_url_safe(self) -> None:
        token = encode_token({"k": "v"}, _SECRET, _SALT)
        # URL-safe base64 alphabet + dots as field separators.
        assert all(c.isalnum() or c in "-_." for c in token), token


class TestTampering:
    def test_flipped_byte_raises_token_invalid(self) -> None:
        token = encode_token({"k": "v"}, _SECRET, _SALT)
        # Flip the last character — sits inside the signature tail.
        last = token[-1]
        swap = "A" if last != "A" else "B"
        tampered = token[:-1] + swap
        with pytest.raises(TokenInvalid):
            decode_token(tampered, _SECRET, _SALT, max_age_seconds=60)

    def test_wrong_secret_raises_token_invalid(self) -> None:
        token = encode_token({"k": "v"}, _SECRET, _SALT)
        with pytest.raises(TokenInvalid):
            decode_token(token, "different-secret", _SALT, max_age_seconds=60)

    def test_wrong_salt_raises_token_invalid(self) -> None:
        token = encode_token({"k": "v"}, _SECRET, _SALT)
        with pytest.raises(TokenInvalid):
            decode_token(token, _SECRET, "other-salt", max_age_seconds=60)


class TestExpiry:
    def test_expired_token_raises_token_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sign now, then fast-forward "time" (itsdangerous reads
        # ``time.time()`` in ``itsdangerous.timed``) so the token looks
        # older than ``max_age_seconds``.
        token = encode_token({"k": "v"}, _SECRET, _SALT)
        real_time = time.time
        monkeypatch.setattr(
            "itsdangerous.timed.time.time",
            lambda: real_time() + 3600,
        )
        with pytest.raises(TokenExpired):
            decode_token(token, _SECRET, _SALT, max_age_seconds=60)

    def test_token_expired_is_token_invalid_subclass(self) -> None:
        # Callers that only care "unusable" can catch TokenInvalid.
        assert issubclass(TokenExpired, TokenInvalid)


class TestPayloadShape:
    def test_garbage_string_raises_token_invalid(self) -> None:
        with pytest.raises(TokenInvalid):
            decode_token("not.a.real.token", _SECRET, _SALT, 60)

    def test_list_payload_would_raise(self) -> None:
        # Direct construction via itsdangerous is the only way to get a
        # non-dict top-level, but encode_token's contract only accepts
        # Mapping[str, Any] and forces dict(). We exercise the guard by
        # hand-signing a list.
        from itsdangerous import URLSafeTimedSerializer

        serializer = URLSafeTimedSerializer(_SECRET, salt=_SALT)
        token = serializer.dumps(["not", "a", "dict"])
        with pytest.raises(TokenInvalid, match="expected JSON object"):
            decode_token(token, _SECRET, _SALT, max_age_seconds=60)
