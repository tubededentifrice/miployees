"""In-memory rate-limiter for the magic-link surface.

**Partial migration in progress — see cd-7huk.** This module
predates the shared abuse-throttle module (``app/abuse/throttle.py``),
which now exists. cd-7huk migrated the passkey-login-begin endpoint
onto the new :func:`~app.abuse.throttle.throttle` decorator. The
remaining buckets (magic-link, signup-start, recover-start, passkey
login-finish lockout) still live here; their handoff to
``app/abuse/throttle.py`` is pending and not yet budgeted.

Three scoped buckets per caller:

* **Request rate** — per-IP and per-email fixed window, 5 hits / 60 s
  on ``/auth/magic/request`` (§15 "Rate limiting and abuse controls":
  "5/min per IP for magic-link send").
* **Consume failure lockout** — per-IP sliding counter, 3 failed
  attempts / 60 s → 10-minute lockout on ``/auth/magic/consume``
  (§15: "3 failed attempts → 10-minute IP lockout").
* **Signup-start budget** — per-IP, per-email, and deployment-wide
  global fixed-window buckets on ``POST /api/v1/signup/start`` (§15
  "Self-serve abuse mitigations": "≤ 5 per IP / hour, ≤ 3 per email
  / hour, ≤ 200 deployment / hour"). Called from
  :mod:`app.auth.signup_abuse`; cd-7huk will absorb this alongside
  the magic-link buckets into the shared abuse throttle.

Storage: single process memory. crew.day v1 runs one worker per
deployment (§01 "One worker pool per process"), so an in-memory dict
is correct for both semantics and audit trail. Horizontal scaling
(if it ever lands) will move this to a shared Redis-backed bucket
inside the cd-7huk rewrite.

Concurrency: a :class:`threading.Lock` guards every dict mutation.
The lock is process-wide but the critical sections are tiny — a list
append + trim — so contention is a non-issue at the deployment sizes
we care about.

No persistence: a process restart resets every bucket. That's a
feature, not a bug, for a dev-scoped throttle: operators can clear
the counters by bouncing the service.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "ConsumeLockout",
    "PasskeyLoginLockout",
    "RateLimited",
    "RecoveryRateLimited",
    "SignupRateLimited",
    "Throttle",
]


# Defaults documented in the module docstring and §15. Exposed as
# module-level Finals so tests can monkey-patch them to tight values
# without re-plumbing the service.
_REQUEST_LIMIT: Final[int] = 5
_REQUEST_WINDOW: Final[timedelta] = timedelta(minutes=1)

_CONSUME_FAIL_LIMIT: Final[int] = 3
_CONSUME_FAIL_WINDOW: Final[timedelta] = timedelta(minutes=1)
_CONSUME_LOCKOUT: Final[timedelta] = timedelta(minutes=10)

# Signup-start budgets — spec §15 "Self-serve abuse mitigations":
#   * ≤ 5 successful starts per source IP per hour
#   * ≤ 3 successful starts per email lifetime on the deployment
#   * ≤ 200 signup starts per deployment per hour (global cool-off)
#
# Spec treats the per-email cap as "lifetime on the deployment"; we
# implement it as a 1-hour rolling window here. The deployment-wide
# persistent counter moves to the shared throttle with cd-7huk — the
# in-memory rolling window is the right local approximation until
# then, and the per-IP + global caps are already enough to defeat a
# single-shot abuser before the email cap even comes into play.
_SIGNUP_IP_LIMIT: Final[int] = 5
_SIGNUP_EMAIL_LIMIT: Final[int] = 3
_SIGNUP_GLOBAL_LIMIT: Final[int] = 200
_SIGNUP_WINDOW: Final[timedelta] = timedelta(hours=1)
_SIGNUP_GLOBAL_KEY: Final[str] = "__global__"

# Recover-start budgets — spec §15 "Self-service lost-device &
# email-change abuse mitigations":
#   * ≤ 3 successful starts per email per hour
#   * ≤ 10 starts per source IP per hour
#   * ≤ 200 starts per deployment per hour (global cool-off)
#
# The email + global caps match signup's; the per-IP cap is looser
# (10 vs 5) because recovery is the "I already have an account" door
# and a shared egress (CGNAT / campus / corporate NAT) can legitimately
# push more concurrent recoveries than signups. Buckets are scoped
# under their own prefixes so a signup burst does not poison the
# recovery counter (and vice versa): the two flows share machinery,
# not state.
_RECOVER_IP_LIMIT: Final[int] = 10
_RECOVER_EMAIL_LIMIT: Final[int] = 3
_RECOVER_GLOBAL_LIMIT: Final[int] = 200
_RECOVER_WINDOW: Final[timedelta] = timedelta(hours=1)
_RECOVER_GLOBAL_KEY: Final[str] = "__global__"

# Passkey-login failure lockout — spec §15 "Passkey specifics" +
# §"Rate limiting and abuse controls" (magic-link consume carries the
# same 3-fails → 10-min shape; we mirror that for passkey assertion
# failures). Keyed on the credential-id hash AND the source-IP hash:
# an attacker cycling IPs against one credential trips the
# credential-scoped lockout; an attacker cycling credentials from one
# IP trips the IP-scoped lockout. The two keys are evaluated
# independently so either one raises on its own.
_PASSKEY_LOGIN_FAIL_LIMIT: Final[int] = 3
_PASSKEY_LOGIN_FAIL_WINDOW: Final[timedelta] = timedelta(minutes=1)
_PASSKEY_LOGIN_LOCKOUT: Final[timedelta] = timedelta(minutes=10)


class RateLimited(Exception):
    """Caller exceeded the per-scope request budget.

    429-equivalent. The HTTP router maps this to ``429 rate_limited``.
    """


class ConsumeLockout(Exception):
    """Caller IP is locked out of consume for the configured window.

    429-equivalent. Distinct from :class:`RateLimited` so the router
    can emit a different error symbol (``consume_locked_out``) and
    the test suite can pin the 3-fail trigger semantics.
    """


class SignupRateLimited(Exception):
    """Caller exceeded a signup-start bucket (per-IP, per-email, global).

    Carries a ``retry_after_seconds`` hint derived from the oldest
    hit still inside the window + :data:`_SIGNUP_WINDOW`. The HTTP
    router maps this to ``429 rate_limited`` with a ``Retry-After``
    header so the SPA can back off deterministically rather than
    poll-spamming. ``scope`` is one of ``"ip"``, ``"email"``, or
    ``"global"`` — audit rows carry it verbatim so operators can tell
    which limit tripped without parsing the exception message.
    """

    def __init__(self, scope: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"signup-start rate limit exceeded (scope={scope!r}, "
            f"retry_after={retry_after_seconds}s)"
        )
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


class PasskeyLoginLockout(Exception):
    """Caller IP or credential is inside the passkey-login lockout window.

    429-equivalent. The router maps this to ``429 rate_limited`` —
    distinct from :class:`ConsumeLockout` (magic-link) so audit rows
    and metrics can tell the two surfaces apart, but the public error
    symbol stays identical so an attacker cannot tell *which* surface
    locked them out.

    ``scope`` is ``"credential"`` or ``"ip"`` — the bucket that
    tripped the lockout. Included so audit / metrics can distinguish
    "this credential is under attack" from "this IP is spraying"; the
    HTTP response body never reveals it (both map to the same
    ``rate_limited`` envelope).
    """

    def __init__(self, scope: str) -> None:
        super().__init__(f"passkey-login locked out (scope={scope!r})")
        self.scope = scope


class RecoveryRateLimited(Exception):
    """Caller exceeded a recover-start bucket (per-IP, per-email, global).

    Mirrors :class:`SignupRateLimited` for the self-service recovery
    surface (§15 "Self-service lost-device & email-change abuse
    mitigations"). A distinct exception type — rather than re-using
    :class:`SignupRateLimited` — means the router can emit a recover-
    specific audit symbol (``audit.recovery.rate_limited``) and tests
    can pin the recover-vs-signup dispatch without a stringly-typed
    ``scope`` discriminator. ``scope`` is one of ``"ip"``, ``"email"``,
    or ``"global"``.
    """

    def __init__(self, scope: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"recover-start rate limit exceeded (scope={scope!r}, "
            f"retry_after={retry_after_seconds}s)"
        )
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


def _signup_limit_for(scope: str) -> int:
    """Return the limit for a signup-start scope (``"ip"``/``"email"``/``"global"``).

    Centralised so the bucket-evaluation loop doesn't branch inline.
    Unknown scopes raise :class:`ValueError` — this is a programming
    error, not a runtime surprise.
    """
    if scope == "ip":
        return _SIGNUP_IP_LIMIT
    if scope == "email":
        return _SIGNUP_EMAIL_LIMIT
    if scope == "global":
        return _SIGNUP_GLOBAL_LIMIT
    raise ValueError(f"unknown signup-start scope: {scope!r}")


def _recover_limit_for(scope: str) -> int:
    """Return the limit for a recover-start scope.

    Sibling of :func:`_signup_limit_for`; kept as a separate helper
    rather than folded into a ``(family, scope)`` two-key lookup
    because the two flows pin their own spec citations and a future
    tweak (e.g. tighter email cap for recovery) should land without
    churning the signup surface.
    """
    if scope == "ip":
        return _RECOVER_IP_LIMIT
    if scope == "email":
        return _RECOVER_EMAIL_LIMIT
    if scope == "global":
        return _RECOVER_GLOBAL_LIMIT
    raise ValueError(f"unknown recover-start scope: {scope!r}")


@dataclass(frozen=True, slots=True)
class _BucketKey:
    """``(scope, key)`` tuple that identifies a single bucket.

    ``scope`` is one of ``"request:ip"``, ``"request:email"``, or
    ``"consume_fail:ip"``; ``key`` is the IP (or email hash) string.
    Frozen so it hashes under :class:`dict` / :class:`defaultdict`.
    """

    scope: str
    key: str


class Throttle:
    """Per-process counter bucket with tripwires for magic-link flows.

    A single instance is shared by both routes; tests construct their
    own so the suite's state never bleeds across cases. The class is
    threadsafe but deliberately not async-aware — the work is
    microseconds of dict mutation, no I/O.
    """

    __slots__ = (
        "_fail_locked_until",
        "_fails",
        "_hits",
        "_lock",
        "_passkey_login_fails",
        "_passkey_login_locked_until",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Fixed-window hits: {(scope, key): [hit_dt, ...]}. A deque
        # keeps the append O(1) and the left-trim cheap.
        self._hits: dict[_BucketKey, deque[datetime]] = defaultdict(deque)
        # Per-IP failed-consume counters — same shape as ``_hits``
        # but the reset trigger is different (§15: 3 fails within
        # the window flips the lockout).
        self._fails: dict[str, deque[datetime]] = defaultdict(deque)
        # IPs currently locked out (value is the moment the lockout
        # expires). Not a deque — single expiry per key.
        self._fail_locked_until: dict[str, datetime] = {}
        # Passkey-login failure counters, keyed by a composite
        # ``("credential"|"ip", key_hash)`` tuple so the two buckets
        # evict independently. Separate dicts from the magic-link
        # ``_fails`` so a consume failure can't count against a login
        # bucket and vice versa (§15 "Passkey specifics").
        self._passkey_login_fails: dict[tuple[str, str], deque[datetime]] = defaultdict(
            deque
        )
        self._passkey_login_locked_until: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------------
    # Request (/auth/magic/request) budget
    # ------------------------------------------------------------------

    def check_request(self, *, ip: str, email_hash: str, now: datetime) -> None:
        """Raise :class:`RateLimited` if either IP or email is over budget.

        Hits against the per-IP and per-email buckets count separately;
        a single call advances both. Exceeding either raises — the
        router maps the exception to ``429 rate_limited``. Below the
        gate, the enumeration guard still applies: a matched email and
        a missing email both produce an identical ``202`` response, so
        a caller who stays under the budget learns nothing about
        whether their email exists.
        """
        with self._lock:
            if self._over_limit(_BucketKey("request:ip", ip), now):
                raise RateLimited(f"per-IP request budget exceeded for {ip!r}")
            if self._over_limit(_BucketKey("request:email", email_hash), now):
                raise RateLimited("per-email request budget exceeded")
            # Under budget — record both hits so future calls see them.
            self._record_hit(_BucketKey("request:ip", ip), now)
            self._record_hit(_BucketKey("request:email", email_hash), now)

    # ------------------------------------------------------------------
    # Consume (/auth/magic/consume) lockout
    # ------------------------------------------------------------------

    def check_consume_allowed(self, *, ip: str, now: datetime) -> None:
        """Raise :class:`ConsumeLockout` if ``ip`` is inside its lockout.

        The router calls this **before** trying to consume the token
        so a locked-out IP never even touches the nonce row. Clears a
        lapsed lockout in passing.
        """
        with self._lock:
            self._evict_expired_lockout(ip, now)
            if ip in self._fail_locked_until:
                raise ConsumeLockout(f"consume locked out for {ip!r}")

    def record_consume_failure(self, *, ip: str, now: datetime) -> None:
        """Increment the per-IP failure counter; flip lockout on the Nth fail.

        The router calls this after a consume raises (bad signature,
        unknown nonce, expired, already-consumed, purpose mismatch) —
        anything observable as "the caller asked us to redeem a token
        that didn't redeem". Success does **not** call this.
        """
        with self._lock:
            bucket = self._fails[ip]
            self._evict_expired(bucket, now, _CONSUME_FAIL_WINDOW)
            bucket.append(now)
            if len(bucket) >= _CONSUME_FAIL_LIMIT:
                self._fail_locked_until[ip] = now + _CONSUME_LOCKOUT
                # Clear the rolling window so the IP has to earn the
                # next lockout from scratch once this one expires.
                bucket.clear()

    def record_consume_success(self, *, ip: str) -> None:
        """Reset the per-IP failure counter on a successful consume.

        A consume that returned a fresh ``MagicLinkOutcome`` means the
        user finally got through — we don't want one bad attempt an
        hour ago to still count against their next legitimate try.
        """
        with self._lock:
            self._fails.pop(ip, None)
            self._fail_locked_until.pop(ip, None)

    # ------------------------------------------------------------------
    # Signup-start (/api/v1/signup/start) budget
    # ------------------------------------------------------------------

    def check_signup_start(
        self, *, ip_hash: str, email_hash: str, now: datetime
    ) -> None:
        """Raise :class:`SignupRateLimited` when any signup bucket is over.

        Evaluates three fixed-window buckets in priority order so the
        caller learns which one tripped first:

        1. **Global** (``_SIGNUP_GLOBAL_LIMIT`` per
           ``_SIGNUP_WINDOW``) — deployment-wide cool-off. Checked
           first so a hostile swarm across distinct IPs still flips
           the brake before either per-IP or per-email even counts.
        2. **Per-IP** (``_SIGNUP_IP_LIMIT`` per
           ``_SIGNUP_WINDOW``) — stop a single IP spraying many
           emails.
        3. **Per-email** (``_SIGNUP_EMAIL_LIMIT`` per
           ``_SIGNUP_WINDOW``) — stop an attacker cycling IPs against
           one inbox.

        On success (below every cap) each bucket is incremented in
        turn — a successful call advances all three. The ``ip`` key
        is an **IP hash**, not the raw IP: signup_abuse hashes the
        address with the per-deployment pepper before handing it in,
        so this module never touches plaintext PII. Mirror this for
        ``email_hash``.

        ``retry_after_seconds`` on the raised exception is computed
        from the oldest hit inside the violating bucket + window, so
        the client's back-off matches the window tail exactly rather
        than always being the full hour.
        """
        with self._lock:
            for scope, bucket_key in (
                ("global", _BucketKey("signup_start:global", _SIGNUP_GLOBAL_KEY)),
                ("ip", _BucketKey("signup_start:ip", ip_hash)),
                ("email", _BucketKey("signup_start:email", email_hash)),
            ):
                limit = _signup_limit_for(scope)
                if self._over_signup_limit(bucket_key, now, limit=limit):
                    retry_after = self._signup_retry_after_seconds(bucket_key, now)
                    raise SignupRateLimited(
                        scope=scope, retry_after_seconds=retry_after
                    )
            # Every bucket is under its cap — record the hit against all
            # three so the next call sees it. The global bucket is
            # advanced *after* per-IP/per-email so a failed per-IP
            # check doesn't pollute the global counter.
            self._record_hit(_BucketKey("signup_start:global", _SIGNUP_GLOBAL_KEY), now)
            self._record_hit(_BucketKey("signup_start:ip", ip_hash), now)
            self._record_hit(_BucketKey("signup_start:email", email_hash), now)

    def _over_signup_limit(self, key: _BucketKey, now: datetime, *, limit: int) -> bool:
        bucket = self._hits[key]
        self._evict_expired(bucket, now, _SIGNUP_WINDOW)
        return len(bucket) >= limit

    def _signup_retry_after_seconds(self, key: _BucketKey, now: datetime) -> int:
        """Return seconds until the oldest hit in ``key`` falls out of window.

        Zero-or-negative values are clamped up to ``1`` so the
        ``Retry-After`` header never tells the client "retry in
        0 seconds", which some SPAs treat as "now".
        """
        bucket = self._hits[key]
        if not bucket:
            # Shouldn't happen on the refusal path (_over_signup_limit
            # only returns True when bucket length >= limit), but the
            # guard keeps the helper total so a future caller can't
            # crash on an empty bucket.
            return int(_SIGNUP_WINDOW.total_seconds())
        expires_at = bucket[0] + _SIGNUP_WINDOW
        seconds = int((expires_at - now).total_seconds())
        return max(seconds, 1)

    # ------------------------------------------------------------------
    # Recover-start (/api/v1/auth/recover/passkey/request) budget
    # ------------------------------------------------------------------

    def check_recover_start(
        self, *, ip_hash: str, email_hash: str, now: datetime
    ) -> None:
        """Raise :class:`RecoveryRateLimited` when any recover bucket is over.

        Mirrors :meth:`check_signup_start` in structure but pins
        recover's own caps (:data:`_RECOVER_IP_LIMIT`,
        :data:`_RECOVER_EMAIL_LIMIT`, :data:`_RECOVER_GLOBAL_LIMIT`)
        and uses distinct bucket prefixes (``recover_start:ip`` /
        ``recover_start:email`` / ``recover_start:global``) so the
        two flows share machinery without sharing state: a signup
        burst does not poison the recover counter, and a recover
        burst does not poison the signup counter.

        Eval order mirrors signup — global → per-IP → per-email — so
        a deployment-wide cool-off fires before either per-IP or
        per-email caps come into play. ``retry_after_seconds`` on
        the raised exception is computed from the oldest hit inside
        the violating bucket.
        """
        with self._lock:
            for scope, bucket_key in (
                (
                    "global",
                    _BucketKey("recover_start:global", _RECOVER_GLOBAL_KEY),
                ),
                ("ip", _BucketKey("recover_start:ip", ip_hash)),
                ("email", _BucketKey("recover_start:email", email_hash)),
            ):
                limit = _recover_limit_for(scope)
                if self._over_recover_limit(bucket_key, now, limit=limit):
                    retry_after = self._recover_retry_after_seconds(bucket_key, now)
                    raise RecoveryRateLimited(
                        scope=scope, retry_after_seconds=retry_after
                    )
            # Every bucket is under its cap — record the hit against
            # all three. Order mirrors signup: global last so a later
            # per-IP refusal doesn't pollute the deployment-wide
            # counter (actually we advance all three once all three
            # passed, matching :meth:`check_signup_start`).
            self._record_hit(
                _BucketKey("recover_start:global", _RECOVER_GLOBAL_KEY), now
            )
            self._record_hit(_BucketKey("recover_start:ip", ip_hash), now)
            self._record_hit(_BucketKey("recover_start:email", email_hash), now)

    def _over_recover_limit(
        self, key: _BucketKey, now: datetime, *, limit: int
    ) -> bool:
        bucket = self._hits[key]
        self._evict_expired(bucket, now, _RECOVER_WINDOW)
        return len(bucket) >= limit

    def _recover_retry_after_seconds(self, key: _BucketKey, now: datetime) -> int:
        """Return seconds until the oldest hit in ``key`` falls out of window.

        Sibling of :meth:`_signup_retry_after_seconds`. Zero-or-
        negative values are clamped up to ``1`` so the ``Retry-After``
        header never tells the client "retry in 0 seconds".
        """
        bucket = self._hits[key]
        if not bucket:
            return int(_RECOVER_WINDOW.total_seconds())
        expires_at = bucket[0] + _RECOVER_WINDOW
        seconds = int((expires_at - now).total_seconds())
        return max(seconds, 1)

    # ------------------------------------------------------------------
    # Passkey-login (/auth/passkey/login/finish) lockout
    # ------------------------------------------------------------------

    def check_passkey_login_allowed(
        self,
        *,
        credential_id_hash: str,
        ip_hash: str,
        now: datetime,
    ) -> None:
        """Raise :class:`PasskeyLoginLockout` on an active lockout.

        Evaluates both buckets (credential-scoped + IP-scoped) in
        turn; either one raises on its own. The router calls this
        **before** calling :func:`app.auth.webauthn.verify_authentication`
        so a locked-out IP or credential never exercises the
        verification code path. Clears lapsed lockouts in passing.

        ``credential_id_hash`` and ``ip_hash`` are caller-supplied
        hashes — this module deliberately never touches plaintext
        credentials or IPs. The caller hashes them with the same
        ``hash_with_pepper`` subkey the audit layer uses so a single
        pepper rotation invalidates every live lockout.
        """
        with self._lock:
            for scope, key in (
                ("credential", credential_id_hash),
                ("ip", ip_hash),
            ):
                bucket_key = (scope, key)
                self._evict_expired_passkey_lockout(bucket_key, now)
                if bucket_key in self._passkey_login_locked_until:
                    raise PasskeyLoginLockout(scope)

    def record_passkey_login_failure(
        self,
        *,
        credential_id_hash: str,
        ip_hash: str,
        now: datetime,
    ) -> None:
        """Increment both buckets; flip the per-bucket lockout at N failures.

        Called by the router on any observable failure: unknown
        credential, bad signature, clone-detected, challenge consumed
        or expired. Bumps the credential-scoped AND IP-scoped
        counters — an attacker spraying credentials from one IP trips
        the IP bucket, an attacker cycling IPs against one credential
        trips the credential bucket. Either lockout is enough to stop
        the next attempt.
        """
        with self._lock:
            for scope, key in (
                ("credential", credential_id_hash),
                ("ip", ip_hash),
            ):
                bucket_key = (scope, key)
                bucket = self._passkey_login_fails[bucket_key]
                self._evict_expired(bucket, now, _PASSKEY_LOGIN_FAIL_WINDOW)
                bucket.append(now)
                if len(bucket) >= _PASSKEY_LOGIN_FAIL_LIMIT:
                    self._passkey_login_locked_until[bucket_key] = (
                        now + _PASSKEY_LOGIN_LOCKOUT
                    )
                    # Clear the rolling window so the bucket has to
                    # earn the next lockout from scratch once this
                    # one expires — matches the magic-link shape.
                    bucket.clear()

    def record_passkey_login_success(
        self,
        *,
        credential_id_hash: str,
        ip_hash: str,
    ) -> None:
        """Reset both per-credential and per-IP failure counters.

        A successful login means the user finally got through — we
        don't want one bad attempt 30 seconds ago to still count
        against their next legitimate try. Called by the router
        after :func:`app.auth.passkey.login_finish` returns.
        """
        with self._lock:
            for scope, key in (
                ("credential", credential_id_hash),
                ("ip", ip_hash),
            ):
                bucket_key = (scope, key)
                self._passkey_login_fails.pop(bucket_key, None)
                self._passkey_login_locked_until.pop(bucket_key, None)

    def _evict_expired_passkey_lockout(
        self, bucket_key: tuple[str, str], now: datetime
    ) -> None:
        """Clear ``bucket_key`` from the lockout table if the ban has elapsed."""
        expires_at = self._passkey_login_locked_until.get(bucket_key)
        if expires_at is not None and expires_at <= now:
            del self._passkey_login_locked_until[bucket_key]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _over_limit(self, key: _BucketKey, now: datetime) -> bool:
        bucket = self._hits[key]
        self._evict_expired(bucket, now, _REQUEST_WINDOW)
        return len(bucket) >= _REQUEST_LIMIT

    def _record_hit(self, key: _BucketKey, now: datetime) -> None:
        self._hits[key].append(now)

    @staticmethod
    def _evict_expired(
        bucket: deque[datetime], now: datetime, window: timedelta
    ) -> None:
        """Drop hits older than ``now - window`` from the left of ``bucket``."""
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _evict_expired_lockout(self, ip: str, now: datetime) -> None:
        """Clear ``ip`` from the lockout table if the ban has elapsed."""
        expires_at = self._fail_locked_until.get(ip)
        if expires_at is not None and expires_at <= now:
            del self._fail_locked_until[ip]
