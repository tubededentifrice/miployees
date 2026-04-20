"""Web-session domain service — issue, validate, revoke, invalidate.

Pure domain code. **No FastAPI coupling.** The HTTP router owns
cookie-header emission, ``Request`` reading, and status-code mapping;
this module only owns the row lifecycle + audit. The cookie builder
lives in :mod:`app.auth.session_cookie` — a dedicated chokepoint so
the §15 flag set is enforced in exactly one place; the name is
re-exported here for callers that already import from
``app.auth.session``.

Cookie shape (§03 "Sessions", §15 "Cookies"):

* Name: ``__Host-crewday_session``.
* Flags: ``Secure; HttpOnly; SameSite=Lax; Path=/``. **No** ``Domain``
  attribute — that's what the ``__Host-`` prefix forbids; a cookie
  carrying ``Domain=...`` is rejected by every modern browser as a
  violation of the prefix's origin-pin.
  :func:`app.auth.session_cookie.build_session_cookie` refuses to emit
  one with ``Domain`` set for the same reason.
* Value: a 192-bit random token generated via
  :func:`secrets.token_urlsafe(24)`. The random bytes themselves are
  the cookie value; the DB row's primary key is
  ``sha256(cookie_value).hexdigest()`` — a 64-char hex digest. The
  raw token is never persisted, so a DB leak cannot be replayed
  against a live session.

**Why sha256-hex in ``Session.id`` and no migration.** Re-reading
:mod:`app.adapters.db.identity.models`, ``Session.id`` is a plain
``String`` column already indexed unique as the PK. Storing the
64-char hex digest there gives us O(1) lookup by ``row.id`` without
adding a ``token_hash`` column (and its migration). The ``id`` is
still opaque to every other table — the FK targets on
``passkey_credential.user_id``, ``session.user_id``, etc. don't
reference ``session.id`` at all. Calling code that logs ``session_id``
still gets a stable identifier; the only change versus a ULID is the
field's length and character set.

Lifetime (§03 "Sessions"):

* **has_owner_grant = True** → ``settings.session_owner_ttl_days``
  (default 7d). Applies to users who hold a ``manager`` surface
  grant on any scope **or** are members of any ``owners`` permission
  group at the moment of login.
* **has_owner_grant = False** → ``settings.session_user_ttl_days``
  (default 30d). Workers, clients, guests.

The caller — the login handler — is the one that knows whether to
flip ``has_owner_grant`` on, because it's the one that just walked
the user's grants + group memberships to produce the
:class:`WorkspaceContext`. The domain service trusts that boolean
and does **not** re-derive it; re-derivation would couple this
module to the authz schema and break the mid-session "keeps the
longer lifetime" rule (the spec pins the lifetime at login, not at
each request).

**Sliding refresh.** Per spec AC: "Sliding refresh only fires past
the half-life mark." :func:`validate` extends ``expires_at`` to
``now + ttl`` whenever ``now - created_at > ttl/2``. This mirrors the
"refreshed on each request after half its lifetime has elapsed"
clause. Before the half-life, ``expires_at`` is left alone (only
``last_seen_at`` moves).

**Absolute timeout (§15 "Cookies").** Every new session carries an
:attr:`~app.adapters.db.identity.models.Session.absolute_expires_at`
pinned at ``now + 90 days`` — a hard cap that sliding refresh cannot
cross. A stolen cookie that keeps bouncing a tab off the server past
half-life still hits this wall; :func:`validate` checks the absolute
cap *before* the idle cap so the two failure modes are distinguishable
in logs (the row carries enough data for post-hoc forensics).

**Fingerprint check (§15 "Shared-origin XSS containment").** Every
session is stamped with
:attr:`~app.adapters.db.identity.models.Session.fingerprint_hash`
— SHA-256 of ``User-Agent + "\n" + Accept-Language`` under an
HKDF-peppered key. A mismatch on :func:`validate` emits an
``audit.session.fingerprint_mismatch`` row and raises
:class:`SessionInvalid` so the browser is forced through a fresh
passkey ceremony. The check is a coarse signal — a user switching
browsers on the same device trips it and will re-auth — but the
tradeoff favours defence-in-depth: a stolen cookie replayed from a
different machine almost always carries a different UA / Accept-
Language, and the user re-authentication cost is ~15 seconds vs. the
blast radius of an un-contained shared-origin XSS that steals the
cookie.

**Invalidation vs. revocation.** Two shapes:

* :func:`revoke` / :func:`revoke_all_for_user` — explicit, destructive.
  The row is deleted. Used for user-driven "sign out" / "sign out
  every other device".
* :func:`invalidate_for_user` / :func:`invalidate_for_credential` —
  non-destructive. The row is kept for forensics but
  ``invalidated_at`` / ``invalidation_cause`` are stamped so
  :func:`validate` refuses it. Used for automatic security events
  (passkey registered / revoked, recovery consumed, sign-count
  rollback detected) where the forensic trail matters. Every
  invalidation writes ``audit.session.invalidated`` with the cause.

**PII minimisation (§15).** We never store the raw User-Agent or IP;
only their SHA-256-peppered hashes via
:func:`app.auth._hashing.hash_with_pepper` against an HKDF subkey
derived from ``settings.root_key``. The hashes are stable enough to
show a user "three sessions on this device" in the security page
without turning the table into a PII sink.

**Audit.** Every mutation writes to ``audit_log`` via
:func:`app.audit.write_audit`. Sessions are tenant-agnostic at issue
time (``workspace_id`` may be NULL — users pick a workspace after
login), so the audit context is the shared ``_agnostic_audit_ctx``
shape used by :mod:`app.auth.magic_link`. Actions emitted:

* ``session.created`` — on :func:`issue`.
* ``session.refreshed`` — on :func:`validate` when sliding refresh
  fires. Omitted on no-op validates so audit volume stays
  proportional to interesting mutations.
* ``session.revoked`` — on :func:`revoke`.
* ``session.revoked_all`` — on :func:`revoke_all_for_user` with the
  count in the diff. A single row instead of one-per-session so the
  audit trail shows the intent ("user rotated every session") not
  just the N individual deletes.

The caller's UoW owns the transaction; this module never calls
``session.commit()``.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.identity.models import PasskeyCredential
from app.adapters.db.identity.models import Session as SessionRow
from app.audit import write_audit
from app.auth._hashing import hash_with_pepper
from app.auth.keys import derive_subkey

# Cookie builder + canonical cookie names live in :mod:`session_cookie`
# — a dedicated chokepoint so the §15 flag set is enforced in exactly
# one place. Re-exported here so callers that already import from
# ``app.auth.session`` don't have to change.
from app.auth.session_cookie import SESSION_COOKIE_NAME, build_session_cookie
from app.config import Settings, get_settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "SESSION_COOKIE_NAME",
    "SessionExpired",
    "SessionInvalid",
    "SessionIssue",
    "build_session_cookie",
    "hash_cookie_value",
    "invalidate_for_credential",
    "invalidate_for_user",
    "issue",
    "revoke",
    "revoke_all_for_user",
    "validate",
]


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# 24 bytes → 192 bits. :func:`secrets.token_urlsafe` returns a
# base64url-encoded string roughly ``ceil(24 * 4 / 3) == 32`` chars
# long; every caller should treat the length as opaque (it can vary
# by ±1 char depending on padding) but assert **at least** 32 chars to
# guard against a future regression narrowing the alphabet.
_COOKIE_TOKEN_BYTES: Final[int] = 24

# HKDF purpose for the pepper used on ua_hash / ip_hash / fingerprint.
# Separate from the magic-link subkey so an oracle on one surface
# doesn't compromise the other. Changing this value invalidates every
# previously-computed hash and is a breaking change; treat it like a
# schema migration.
_HKDF_PURPOSE: Final[str] = "session-hash"

# Absolute session cap (§15 "Cookies"): **90 days**. Even a session
# that slides indefinitely past half-life stops being honoured once
# ``absolute_expires_at`` passes. Pinned here (not in :class:`Settings`)
# because the value is a security invariant, not a deploy knob — an
# operator who needs a shorter cap stacks a §11 authorisation rule on
# top; raising it is a spec change and an ADR.
_ABSOLUTE_SESSION_TTL: Final[timedelta] = timedelta(days=90)

# Shared tenant-agnostic audit sentinel — mirrors
# :mod:`app.auth.magic_link`. A real workspace is resolved only after
# the user picks one post-login, and most session events (issue,
# revoke, refresh, invalidate) happen before that pick or on explicit
# sign-out / auto-invalidation, where no single workspace is in scope.
_AGNOSTIC_WORKSPACE_ID: Final[str] = "00000000000000000000000000"
_AGNOSTIC_ACTOR_ID: Final[str] = "00000000000000000000000000"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionIssue:
    """Result of :func:`issue`.

    Carries the opaque cookie value the router stamps into
    ``Set-Cookie``, the row's primary key (the sha256-hex digest —
    useful for revocation APIs and for calling
    :func:`revoke_all_for_user` with ``except_session_id=``), and the
    absolute expiry the cookie's ``Max-Age`` / ``Expires`` attribute
    should reflect.
    """

    session_id: str
    cookie_value: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionInvalid(ValueError):
    """Cookie value did not match any row (never issued, or revoked).

    401-equivalent. The caller should clear the cookie and re-prompt
    for a passkey. We deliberately do NOT distinguish "never existed"
    from "revoked" in the error type — leaking that bit would turn
    the session table into an enumeration oracle.
    """


class SessionExpired(ValueError):
    """Row exists but ``expires_at`` has passed.

    401-equivalent. Semantically equivalent to :class:`SessionInvalid`
    from the user's perspective (both land on "sign in again"), but
    the domain service surfaces the distinction so metrics can tell
    "user came back after their session expired" from
    "unauthenticated probe" apart.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _pepper(settings: Settings | None) -> bytes:
    """Return the HKDF subkey used to pepper ua_hash / ip_hash.

    Separate subkey from the magic-link signer (purpose label differs)
    so a leaked session hash can't be replayed against a magic-link
    oracle and vice versa. See :mod:`app.auth.keys` for the shape.
    """
    s = settings if settings is not None else get_settings()
    return derive_subkey(s.root_key, purpose=_HKDF_PURPOSE)


def _ttl_for(settings: Settings | None, *, has_owner_grant: bool) -> timedelta:
    """Return the session lifetime for the given grant population.

    Owners / managers get a shorter lifetime (default 7d) because the
    blast radius of a stolen owner session is larger; workers, clients,
    and guests get the longer one (default 30d) to reduce re-auth
    friction on a population whose credentials are commonly tied to
    a shared device (kitchen tablet, front-desk kiosk).
    """
    s = settings if settings is not None else get_settings()
    days = s.session_owner_ttl_days if has_owner_grant else s.session_user_ttl_days
    return timedelta(days=days)


def _agnostic_audit_ctx() -> WorkspaceContext:
    """Return a sentinel :class:`WorkspaceContext` for tenant-agnostic events.

    Sessions are issued before any workspace is picked, and revoked
    either globally (logout everywhere) or during re-enrolment where
    multiple workspaces may be in scope. A synthetic context with
    zero-ULID workspace + actor ids keeps the audit writer happy
    without pretending the event belongs to one specific tenant.
    """
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=_AGNOSTIC_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",  # unused for system actors
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def hash_cookie_value(cookie_value: str) -> str:
    """Return the row-PK form of ``cookie_value`` (lower-hex sha256).

    Exposed publicly so the HTTP router can map an inbound cookie
    directly to a ``Session.id`` lookup without re-deriving the rule
    inline. The digest is deterministic (no pepper, no salt) — the
    row PK is just a lookup key, not a credential; the 192-bit random
    cookie is the actual credential.
    """
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


def _compute_fingerprint(ua: str, accept_language: str, pepper: bytes) -> str:
    """Return the fingerprint hash stamped onto a new session row.

    Concatenates ``User-Agent`` and ``Accept-Language`` with a ``\\n``
    separator (so ``"foo"`` + ``"bar"`` and ``"foob"`` + ``"ar"`` hash
    to different values) and peppers with the HKDF subkey. The
    separator is a newline rather than a colon / pipe because HTTP
    header values never contain raw newlines — they're forbidden by
    RFC 7230 — so the fingerprint cannot be forged by a header-
    injection attack that smuggles the separator into one value.

    The pepper is the same subkey as ``ua_hash`` / ``ip_hash`` use —
    the three hashes already share an oracle model (all computed
    server-side, stored verbatim, never echoed), and splitting them
    into distinct subkeys would multiply the key-rotation surface
    without moving the threat model.
    """
    composite = f"{ua}\n{accept_language}"
    return hash_with_pepper(composite, pepper)


# ---------------------------------------------------------------------------
# Public surface — issue
# ---------------------------------------------------------------------------


def issue(
    session: DbSession,
    *,
    user_id: str,
    workspace_id: str | None = None,
    has_owner_grant: bool,
    ua: str,
    ip: str,
    accept_language: str = "",
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> SessionIssue:
    """Insert a fresh :class:`Session` row and return the opaque cookie value.

    The caller — login handler, first-passkey-enrolment finish route —
    commits the surrounding UoW. On success the row exists with
    ``expires_at = now + ttl``, ``absolute_expires_at = now + 90d``,
    ``last_seen_at = created_at = now``, and a fingerprint hash stamped
    from ``ua`` + ``accept_language``; a failed commit rolls back the
    row and the audit event together, so nothing leaks.

    ``has_owner_grant`` is supplied by the caller after walking the
    user's grants + group memberships; we pin the lifetime at login
    time per spec (recomputed on login, not mid-session). The audit
    row carries the chosen TTL so downstream readers can detect a
    config drift without re-deriving the value.

    ``accept_language`` defaults to ``""`` so older callers (tests that
    only model the UA / IP shape) keep working without refactoring;
    HTTP-layer callers MUST forward the browser's ``Accept-Language``
    header so the fingerprint pin carries its full signal.
    """
    resolved_now = now if now is not None else _now(clock)
    pepper = _pepper(settings)

    cookie_value = secrets.token_urlsafe(_COOKIE_TOKEN_BYTES)
    session_id = hash_cookie_value(cookie_value)
    ttl = _ttl_for(settings, has_owner_grant=has_owner_grant)
    expires_at = resolved_now + ttl
    absolute_expires_at = resolved_now + _ABSOLUTE_SESSION_TTL
    # Clip the idle expiry to the absolute cap if the operator cranked
    # up the idle TTL past 90 days. The DB is the enforcement point; a
    # clipped ``expires_at`` is also less confusing than a row whose
    # idle expiry sits past its absolute cap (sliding refresh would
    # never touch it, and the failure mode would surface as "absolute
    # cap hit" rather than the expected "idle timeout").
    if expires_at > absolute_expires_at:
        expires_at = absolute_expires_at
    fingerprint_hash = _compute_fingerprint(ua, accept_language, pepper)

    row = SessionRow(
        id=session_id,
        user_id=user_id,
        workspace_id=workspace_id,
        expires_at=expires_at,
        absolute_expires_at=absolute_expires_at,
        last_seen_at=resolved_now,
        ua_hash=hash_with_pepper(ua, pepper),
        ip_hash=hash_with_pepper(ip, pepper),
        fingerprint_hash=fingerprint_hash,
        created_at=resolved_now,
        invalidated_at=None,
        invalidation_cause=None,
    )
    # justification: ``session`` is user-scoped, not workspace-scoped
    # (see :mod:`app.adapters.db.identity`). The ORM tenant filter has
    # nothing to apply here, and login runs before a
    # :class:`WorkspaceContext` is in scope anyway.
    with tenant_agnostic():
        session.add(row)
        session.flush()

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="session",
        entity_id=session_id,
        action="session.created",
        diff={
            "user_id": user_id,
            "workspace_id": workspace_id,
            "has_owner_grant": has_owner_grant,
            "ttl_seconds": int(ttl.total_seconds()),
            "absolute_ttl_seconds": int(_ABSOLUTE_SESSION_TTL.total_seconds()),
            "ua_hash": row.ua_hash,
            "ip_hash": row.ip_hash,
            "fingerprint_hash": fingerprint_hash,
        },
        clock=clock,
    )

    return SessionIssue(
        session_id=session_id,
        cookie_value=cookie_value,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Public surface — validate
# ---------------------------------------------------------------------------


def validate(
    session: DbSession,
    *,
    cookie_value: str,
    ua: str = "",
    accept_language: str = "",
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> str:
    """Return the session's ``user_id`` if the cookie is live; raise otherwise.

    Gates, in order:

    1. Row exists (else :class:`SessionInvalid`).
    2. ``invalidated_at`` is NULL (else :class:`SessionInvalid`;
       forensic row preserved).
    3. ``absolute_expires_at`` in the future when present (else
       :class:`SessionExpired` — the 90-day hard cap).
    4. ``expires_at`` in the future (else :class:`SessionExpired` —
       the idle cap).
    5. Fingerprint matches the caller's ``ua + accept_language`` when
       the row carries one and the caller supplied one (else
       :class:`SessionInvalid` + ``audit.session.fingerprint_mismatch``).

    Side effects on success:

    * ``last_seen_at`` is bumped to ``now`` on every call (cheap —
      one UPDATE on the already-indexed PK).
    * Sliding refresh: if the session is past its half-life,
      ``expires_at`` extends to ``now + ttl`` — **clipped to
      ``absolute_expires_at``** when one is set, so the 90-day cap
      genuinely bounds the row.

    ``ua`` / ``accept_language`` default to ``""`` so older callers
    that don't yet route headers through can invoke ``validate`` and
    skip the fingerprint gate. The HTTP router MUST forward the real
    values for the gate to carry its defence-in-depth signal.

    Raises:

    * :class:`SessionInvalid` — no row, invalidated row, or
      fingerprint mismatch. All three collapse to the same error type
      so the caller cannot distinguish them — leaking that bit would
      give an attacker an enumeration / replay oracle.
    * :class:`SessionExpired` — row exists but an expiry gate fired.

    The caller's UoW owns transaction boundaries; nothing here
    commits.
    """
    resolved_now = now if now is not None else _now(clock)
    session_id = hash_cookie_value(cookie_value)

    # justification: ``session`` is user-scoped; no tenant predicate applies.
    with tenant_agnostic():
        row = session.get(SessionRow, session_id)
    if row is None:
        raise SessionInvalid(f"session id {session_id!r} unknown")

    # Invalidated-mid-flight check — the row still exists (forensic
    # trail) but :func:`invalidate_for_user` / :func:`invalidate_for_
    # credential` stamped the two columns. Treat as a 401-equivalent:
    # no audit (the invalidate path already wrote one).
    if row.invalidated_at is not None:
        raise SessionInvalid(
            f"session id {session_id!r} invalidated (cause={row.invalidation_cause!r})"
        )

    # Absolute cap check BEFORE the idle cap so the two failure modes
    # are visually distinguishable in logs. ``absolute_expires_at``
    # is nullable for pre-hardening rows — skip the gate in that case
    # (the idle cap still enforces a bound).
    absolute_expires_at = row.absolute_expires_at
    if absolute_expires_at is not None:
        if absolute_expires_at.tzinfo is None:
            absolute_expires_at = absolute_expires_at.replace(
                tzinfo=resolved_now.tzinfo
            )
        if absolute_expires_at <= resolved_now:
            raise SessionExpired(
                f"session past absolute cap at {absolute_expires_at.isoformat()}"
            )

    # Normalise the round-tripped expiry — SQLite drops ``tzinfo`` on
    # ``DateTime(timezone=True)`` read, the same caveat as everywhere
    # else in the identity layer.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=resolved_now.tzinfo)
    if expires_at <= resolved_now:
        raise SessionExpired(f"session expired at {expires_at.isoformat()}")

    # Fingerprint check — only fires when the row carries one (pre-
    # hardening rows skip) AND the caller supplied a non-empty pair.
    # An empty caller pair with a stamped fingerprint would always
    # mismatch (the stored value is the hash of ``"\n"``), so we skip
    # to keep the legacy ``validate(cookie_value=...)`` call shape
    # working during the rollout. The HTTP router MUST pass the real
    # headers.
    if row.fingerprint_hash is not None and (ua or accept_language):
        pepper = _pepper(settings)
        expected = _compute_fingerprint(ua, accept_language, pepper)
        if expected != row.fingerprint_hash:
            write_audit(
                session,
                _agnostic_audit_ctx(),
                entity_kind="session",
                entity_id=session_id,
                action="session.fingerprint_mismatch",
                diff={
                    "user_id": row.user_id,
                    "stored_fingerprint": row.fingerprint_hash,
                    "presented_fingerprint": expected,
                },
                clock=clock,
            )
            raise SessionInvalid(f"session id {session_id!r} fingerprint mismatch")

    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=resolved_now.tzinfo)
    # Measured TTL = ``expires_at - created_at`` at read time. On a
    # freshly-issued row this is the configured lifetime (7d / 30d);
    # after a prior refresh it's larger, because ``created_at`` is
    # pinned at issue but ``expires_at`` has moved forward. That's
    # the intended behaviour — the half-life pivot slides with the
    # session, so a heavily-used session keeps refreshing without
    # ever hitting an "about to expire" cliff.
    ttl = expires_at - created_at
    elapsed = resolved_now - created_at
    remaining = expires_at - resolved_now
    # Both gates together match the spec's intent: we only refresh
    # past the half-life mark **and** when less than half the TTL is
    # left. The two are nearly equivalent on an un-refreshed session
    # (elapsed + remaining == ttl), but differ subtly after prior
    # refreshes, and carrying both keeps the refresh behaviour
    # obvious to somebody re-reading this in a year.
    past_halflife = elapsed > (ttl / 2) and remaining < (ttl / 2)

    # Always bump ``last_seen_at``. Touching one column on a PK-indexed
    # row is cheap on both backends, and we want the security page to
    # reflect the actual last-use time without waiting for the next
    # refresh trigger.
    row.last_seen_at = resolved_now

    if past_halflife:
        # Sliding refresh: extend to ``now + ttl``. Using the measured
        # TTL (not the config-derived lifetime) means a previously-
        # extended session keeps extending by its own cadence — a user
        # who signed in on a short-lived worker account but later gained
        # an owners grant keeps the session shape they started with
        # until the next login. See §03 "Sessions": "Recomputed on
        # login, not mid-session".
        #
        # §15 "Cookies": the absolute cap is a **hard** ceiling. Clip
        # the refreshed expiry to ``absolute_expires_at`` so the 90-day
        # wall genuinely bounds the row. The next call after the
        # absolute cap passes raises :class:`SessionExpired` via the
        # cap gate above.
        new_expires = resolved_now + ttl
        if absolute_expires_at is not None and new_expires > absolute_expires_at:
            new_expires = absolute_expires_at
        row.expires_at = new_expires
        write_audit(
            session,
            _agnostic_audit_ctx(),
            entity_kind="session",
            entity_id=session_id,
            action="session.refreshed",
            diff={
                "user_id": row.user_id,
                "old_expires_at": expires_at.isoformat(),
                "new_expires_at": new_expires.isoformat(),
            },
            clock=clock,
        )

    return row.user_id


# ---------------------------------------------------------------------------
# Public surface — revoke
# ---------------------------------------------------------------------------


def revoke(
    session: DbSession,
    *,
    session_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> None:
    """Delete the session row. Idempotent on a missing row.

    The caller passes the row's primary key (sha256-hex digest), not
    the cookie value — a logout handler that already hashed the
    inbound cookie should pass the hash through unchanged. A missing
    row is treated as a no-op on the assumption the user clicked
    "Sign out" twice; the audit row still lands so the trail reflects
    the intent.
    """
    resolved_now = now if now is not None else _now(clock)
    # justification: ``session`` is user-scoped; no tenant predicate applies.
    with tenant_agnostic():
        row = session.get(SessionRow, session_id)
        if row is not None:
            session.delete(row)
            session.flush()

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="session",
        entity_id=session_id,
        action="session.revoked",
        diff={
            "user_id": row.user_id if row is not None else None,
            "existed": row is not None,
            "at": resolved_now.isoformat(),
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public surface — revoke_all_for_user
# ---------------------------------------------------------------------------


def revoke_all_for_user(
    session: DbSession,
    *,
    user_id: str,
    except_session_id: str | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> int:
    """Delete every ``Session`` row for ``user_id``; return the deleted count.

    ``except_session_id`` is the calling session's PK — use it to
    implement "Sign out of every other device": the user's current
    session survives, every other one is gone. When ``None``, every
    session for the user is revoked (re-enrolment, full logout).

    A single audit row lands regardless of count, so the trail shows
    the intent ("user rotated every session") without bloating the
    log with N per-session deletes.
    """
    resolved_now = now if now is not None else _now(clock)
    # justification: ``session`` is user-scoped; no tenant predicate applies.
    with tenant_agnostic():
        stmt = select(SessionRow).where(SessionRow.user_id == user_id)
        if except_session_id is not None:
            stmt = stmt.where(SessionRow.id != except_session_id)
        rows = list(session.scalars(stmt).all())
        count = len(rows)
        if count > 0:
            delete_stmt = delete(SessionRow).where(SessionRow.user_id == user_id)
            if except_session_id is not None:
                delete_stmt = delete_stmt.where(SessionRow.id != except_session_id)
            session.execute(
                delete_stmt.execution_options(synchronize_session="fetch"),
            )
            session.flush()

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="session",
        entity_id=user_id,
        action="session.revoked_all",
        diff={
            "user_id": user_id,
            "except_session_id": except_session_id,
            "count": count,
            "at": resolved_now.isoformat(),
        },
        clock=clock,
    )

    return count


# ---------------------------------------------------------------------------
# Public surface — invalidate (non-destructive, keeps forensic row)
# ---------------------------------------------------------------------------


def invalidate_for_user(
    session: DbSession,
    *,
    user_id: str,
    cause: str,
    except_session_id: str | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> int:
    """Mark every active session for ``user_id`` invalidated; return the count.

    Non-destructive sibling of :func:`revoke_all_for_user`. Used for
    automatic security events (passkey registered / revoked, recovery
    consumed) where the forensic trail matters: the row stays in the
    table with ``invalidated_at = now`` + ``invalidation_cause = cause``
    so operators can still join sign-in audit → session → subsequent
    activity, but :func:`validate` refuses it.

    ``except_session_id`` preserves the caller's own session when the
    invalidation was driven by an action inside that session (e.g.
    "user added a passkey" — their current session shouldn't be cut
    mid-click). When ``None``, every active session for the user is
    invalidated.

    A single ``audit.session.invalidated`` row lands per call — the
    count + cause are in the diff. One row vs N keeps the audit
    proportional to intent, same pattern as
    :func:`revoke_all_for_user`.
    """
    resolved_now = now if now is not None else _now(clock)

    # "Active" = not already invalidated AND not already idle-expired.
    # Re-flagging an already-expired-but-live row would just add
    # unnecessary audit weight; skipping it keeps the count matching
    # what the caller would visually think of as "sessions I just cut".
    # justification: session is user-scoped; no tenant predicate applies.
    with tenant_agnostic():
        select_stmt = select(SessionRow.id).where(
            SessionRow.user_id == user_id,
            SessionRow.invalidated_at.is_(None),
            SessionRow.expires_at > resolved_now,
        )
        if except_session_id is not None:
            select_stmt = select_stmt.where(SessionRow.id != except_session_id)
        target_ids = list(session.scalars(select_stmt).all())
        count = len(target_ids)
        if count > 0:
            update_stmt = (
                update(SessionRow)
                .where(SessionRow.id.in_(target_ids))
                .values(invalidated_at=resolved_now, invalidation_cause=cause)
            )
            session.execute(update_stmt.execution_options(synchronize_session="fetch"))
            session.flush()

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="session",
        entity_id=user_id,
        action="session.invalidated",
        diff={
            "user_id": user_id,
            "cause": cause,
            "except_session_id": except_session_id,
            "count": count,
            "at": resolved_now.isoformat(),
        },
        clock=clock,
    )

    return count


def invalidate_for_credential(
    session: DbSession,
    *,
    credential_id: bytes,
    cause: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> int:
    """Invalidate every active session for the user who owns ``credential_id``.

    §15 "Passkey specifics" "sign-count rollback auto-revoke" — when
    :class:`~app.auth.passkey.CloneDetected` fires, we invalidate
    every session for the credential's owner so the attacker's cookie
    (if any) is rejected on the next request. Same non-destructive
    shape as :func:`invalidate_for_user`; the row is preserved for
    forensics.

    Returns the count of sessions invalidated. A missing credential
    (deleted between clone detection and this call) returns ``0`` and
    still emits an audit row so the trail records the intent.
    """
    resolved_now = now if now is not None else _now(clock)

    # justification: passkey_credential is identity-scoped.
    with tenant_agnostic():
        credential_row = session.get(PasskeyCredential, credential_id)
    if credential_row is None:
        # No credential → no user → nothing to invalidate. Still write
        # the audit row so the trail shows "we tried and found
        # nothing" — an operator investigating a clone event needs
        # that evidence even if the credential was concurrently
        # revoked.
        write_audit(
            session,
            _agnostic_audit_ctx(),
            entity_kind="session",
            entity_id="",
            action="session.invalidated",
            diff={
                "user_id": None,
                "credential_id": None,
                "cause": cause,
                "count": 0,
                "at": resolved_now.isoformat(),
                "note": "credential_not_found",
            },
            clock=clock,
        )
        return 0

    return invalidate_for_user(
        session,
        user_id=credential_row.user_id,
        cause=cause,
        now=resolved_now,
        clock=clock,
    )
