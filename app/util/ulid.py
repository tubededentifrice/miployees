"""ULID generation and parsing.

Wraps :mod:`ulid` (the ``python-ulid`` package) with a thin, strictly
typed surface:

* :func:`new_ulid` — generate a 26-char Crockford base32 string,
  monotonic within the same millisecond, optionally pinned to a
  :class:`~app.util.clock.Clock` for tests.
* :func:`parse_ulid` — parse back to a :class:`ulid.ULID` instance.

``python-ulid`` 3.x does not ship a built-in monotonic generator, so we
implement the standard "bump the random tail by one whenever the
timestamp hasn't moved" strategy behind a :class:`threading.Lock`.
"""

from __future__ import annotations

import os
import threading
import time

from ulid import ULID

from app.util.clock import Clock

__all__ = ["new_ulid", "parse_ulid"]


_RANDOM_BYTES = 10  # ULID layout: 6 timestamp bytes + 10 random bytes.
_MAX_RANDOM = (1 << (_RANDOM_BYTES * 8)) - 1

_lock = threading.Lock()
_last_ms: int = -1
_last_random: int = 0


def new_ulid(clock: Clock | None = None) -> str:
    """Return a fresh ULID as its 26-char Crockford base32 form.

    Thread-safe and monotonic: two calls inside the same millisecond
    (same host clock tick) are guaranteed to sort strictly in call
    order. If the random tail saturates within a single millisecond we
    bump the timestamp by 1 ms — this is the same escape hatch the
    reference Rust / JS ULID libraries use and keeps strict ordering
    without blocking.

    If ``clock`` is provided, the timestamp portion is derived from
    ``clock.now()`` so tests can pin the prefix deterministically.
    """
    global _last_ms, _last_random

    if clock is not None:
        now_dt = clock.now()
        now_ms = int(now_dt.timestamp() * 1000)
    else:
        # SystemClock path — avoid building a datetime just to drop it.
        now_ms = _system_now_ms()

    with _lock:
        if now_ms == _last_ms:
            # Same millisecond — bump the random tail.
            if _last_random >= _MAX_RANDOM:
                # Random space exhausted; push forward 1ms to keep
                # strict ordering without blocking.
                _last_ms += 1
                _last_random = int.from_bytes(os.urandom(_RANDOM_BYTES), "big")
            else:
                _last_random += 1
        else:
            # Clock moved (forward or backward — backward happens in
            # tests with FrozenClock, and on NTP corrections). Reseed
            # the random tail so ULIDs still reflect the caller's
            # wall-clock source of truth.
            _last_ms = now_ms
            _last_random = int.from_bytes(os.urandom(_RANDOM_BYTES), "big")

        ms = _last_ms
        rand = _last_random

    raw = ms.to_bytes(6, "big") + rand.to_bytes(_RANDOM_BYTES, "big")
    return str(ULID.from_bytes(raw))


def parse_ulid(s: str) -> ULID:
    """Parse a 26-char Crockford base32 ULID string.

    Raises :class:`ValueError` for malformed input (propagated from
    ``python-ulid``).
    """
    return ULID.from_str(s)


def _system_now_ms() -> int:
    """Current wall-clock time as integer milliseconds since epoch.

    Isolated here so ``new_ulid`` has a single call site to mock, and
    so we don't pay for a ``datetime`` round-trip in the hot path.
    """
    return int(time.time() * 1000)
