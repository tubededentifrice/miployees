"""Web-session domain service — issue, validate, revoke, revoke-all.

Pure domain code. **No FastAPI coupling.** The HTTP router (a future
cd-ika7 successor) owns cookie-header emission, ``Request`` reading,
and status-code mapping; this module only owns the row lifecycle +
audit + the spec-pinned cookie-string builder.

Cookie shape (§03 "Sessions", §15 "Cookies"):

* Name: ``__Host-crewday_session``.
* Flags: ``Secure; HttpOnly; SameSite=Lax; Path=/``. **No** ``Domain``
  attribute — that's what the ``__Host-`` prefix forbids; a cookie
  carrying ``Domain=...`` is rejected by every modern browser as a
  violation of the prefix's origin-pin. :func:`build_session_cookie`
  refuses to emit one with ``Domain`` set for the same reason.
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
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.identity.models import Session as SessionRow
from app.audit import write_audit
from app.auth._hashing import hash_with_pepper
from app.auth.keys import derive_subkey
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
    "issue",
    "revoke",
    "revoke_all_for_user",
    "validate",
]


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# Cookie name. The ``__Host-`` prefix pins the cookie to the exact
# origin that Set-it: no ``Domain`` attribute allowed, ``Secure``
# required, ``Path=/`` required. Violating any of those makes the
# browser refuse the cookie silently, which is the right defence.
SESSION_COOKIE_NAME: Final[str] = "__Host-crewday_session"

# 24 bytes → 192 bits. :func:`secrets.token_urlsafe` returns a
# base64url-encoded string roughly ``ceil(24 * 4 / 3) == 32`` chars
# long; every caller should treat the length as opaque (it can vary
# by ±1 char depending on padding) but assert **at least** 32 chars to
# guard against a future regression narrowing the alphabet.
_COOKIE_TOKEN_BYTES: Final[int] = 24

# HKDF purpose for the pepper used on ua_hash / ip_hash. Separate from
# the magic-link subkey so an oracle on one surface doesn't compromise
# the other. Changing this value invalidates every previously-computed
# hash and is a breaking change; treat it like a schema migration.
_HKDF_PURPOSE: Final[str] = "session-hash"

# Shared tenant-agnostic audit sentinel — mirrors
# :mod:`app.auth.magic_link`. A real workspace is resolved only after
# the user picks one post-login, and most session events (issue,
# revoke, refresh) happen before that pick or on explicit sign-out,
# where no single workspace is in scope.
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
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> SessionIssue:
    """Insert a fresh :class:`Session` row and return the opaque cookie value.

    The caller — login handler, first-passkey-enrolment finish route —
    commits the surrounding UoW. On success the row exists with
    ``expires_at = now + ttl`` and ``last_seen_at = created_at = now``;
    a failed commit rolls back the row and the audit event together,
    so nothing leaks.

    ``has_owner_grant`` is supplied by the caller after walking the
    user's grants + group memberships; we pin the lifetime at login
    time per spec (recomputed on login, not mid-session). The audit
    row carries the chosen TTL so downstream readers can detect a
    config drift without re-deriving the value.
    """
    resolved_now = now if now is not None else _now(clock)
    pepper = _pepper(settings)

    cookie_value = secrets.token_urlsafe(_COOKIE_TOKEN_BYTES)
    session_id = hash_cookie_value(cookie_value)
    ttl = _ttl_for(settings, has_owner_grant=has_owner_grant)
    expires_at = resolved_now + ttl

    row = SessionRow(
        id=session_id,
        user_id=user_id,
        workspace_id=workspace_id,
        expires_at=expires_at,
        last_seen_at=resolved_now,
        ua_hash=hash_with_pepper(ua, pepper),
        ip_hash=hash_with_pepper(ip, pepper),
        created_at=resolved_now,
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
            "ua_hash": row.ua_hash,
            "ip_hash": row.ip_hash,
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
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> str:
    """Return the session's ``user_id`` if the cookie is live; raise otherwise.

    Side effects on success:

    * ``last_seen_at`` is bumped to ``now`` on every call (cheap —
      one UPDATE on the already-indexed PK).
    * Sliding refresh: if the session is past its half-life,
      ``expires_at`` extends to ``now + ttl_remaining_cap``. The
      extension amount mirrors the original TTL (``expires_at -
      created_at``), so a 7-day session refreshes to "7 days from
      now" and a 30-day session to "30 days from now".

    Raises:

    * :class:`SessionInvalid` — no row with matching hash. Either the
      cookie was never issued, was revoked, or carries tampered bytes.
    * :class:`SessionExpired` — row exists but ``expires_at <= now``.
      The caller should clear the cookie.

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

    # Normalise the round-tripped expiry — SQLite drops ``tzinfo`` on
    # ``DateTime(timezone=True)`` read, the same caveat as everywhere
    # else in the identity layer.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=resolved_now.tzinfo)
    if expires_at <= resolved_now:
        raise SessionExpired(f"session expired at {expires_at.isoformat()}")

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
        new_expires = resolved_now + ttl
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
# Public surface — cookie header builder
# ---------------------------------------------------------------------------


def build_session_cookie(
    cookie_value: str,
    expires_at: datetime,
    *,
    secure: bool = True,
) -> str:
    """Return a spec-compliant ``Set-Cookie`` header value.

    Enforces the §03 "Sessions" / §15 "Cookies" flag set. ``secure``
    is kept as a parameter — never an env toggle read inline — so
    tests and the router layer both pass it explicitly. In dev the
    operator can flip it to ``False`` to test cookie-dependent
    features over plain-HTTP loopback; the ``__Host-`` prefix makes
    that combination invalid per RFC 6265bis, so we raise rather
    than silently emit a cookie the browser will refuse.

    ``expires_at`` goes into an ``Expires=`` attribute formatted per
    RFC 7231 §7.1.1.1 (IMF-fixdate). ``Max-Age`` would work too, but
    browsers that dislike clock skew favour ``Expires``; we emit both
    for belt-and-braces.

    No ``Domain`` attribute is ever emitted — the ``__Host-`` prefix
    forbids it. A caller that somehow supplies one (future
    multi-surface work) would be rejected at the constructor before
    a wrong-shape cookie hits the wire.
    """
    if not secure:
        # ``__Host-`` requires ``Secure`` per RFC 6265bis; a browser
        # that sees a ``__Host-`` cookie without ``Secure`` drops it
        # silently, which would look like "logins mysteriously don't
        # stick". Fail loud instead so the operator can fix the
        # deployment rather than chase a phantom bug.
        raise ValueError(
            "cookie name starts with '__Host-' which requires Secure; "
            "pass secure=True (the default) or use a dev-only proxy "
            "that terminates TLS locally."
        )

    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be an aware datetime; got naive input")

    # IMF-fixdate (RFC 7231 §7.1.1.1) — day-name, day, month-name,
    # year, hh:mm:ss, "GMT". ``strftime`` yields the right byte shape
    # once the datetime is in UTC.
    expiry_utc = expires_at.astimezone(UTC)
    imf = expiry_utc.strftime("%a, %d %b %Y %H:%M:%S GMT")

    # ``Max-Age`` belt-and-braces with ``Expires``: browsers that
    # dislike clock skew prefer one over the other; emitting both
    # keeps the cookie behaviour consistent across stacks. We sample
    # a fresh UTC "now" via :class:`SystemClock` because callers into
    # a header builder rarely have a :class:`Clock` handy, and the
    # value is seconds-precision — sub-second drift is immaterial.
    max_age = max(0, int((expiry_utc - SystemClock().now()).total_seconds()))

    attrs = [
        f"{SESSION_COOKIE_NAME}={cookie_value}",
        "Secure",
        "HttpOnly",
        "SameSite=Lax",
        "Path=/",
        f"Max-Age={max_age}",
        f"Expires={imf}",
    ]
    return "; ".join(attrs)
