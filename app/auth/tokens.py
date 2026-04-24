"""API-token domain service — mint, verify, list, revoke.

Pure domain code. **No FastAPI coupling.** The HTTP router
(:mod:`app.api.v1.auth.tokens`, :mod:`app.api.v1.auth.me_tokens`)
owns status-code mapping + request parsing; this module owns row
lifecycle + argon2id verification + audit writes. The caller's UoW
owns the transaction boundary (§01 "Key runtime invariants" #3) —
this module never calls ``session.commit()``.

**Three token kinds** (§03 "API tokens"):

* ``scoped`` — workspace-pinned, scope-limited, long-lived. The
  cd-c91 default. :func:`mint` requires ``workspace_id`` and a
  non-empty ``scopes`` dict.
* ``delegated`` — workspace-pinned but scope-less: authority
  inherits from the delegating user's :class:`RoleGrant` rows (§11
  embedded agents). :func:`mint` requires
  ``delegate_for_user_id`` + ``workspace_id`` and refuses non-empty
  ``scopes``; default TTL 30 days.
* ``personal`` — PAT minted by a user for themselves, ``me:*``
  scopes only, **no workspace** (§03 "Personal access tokens").
  :func:`mint` refuses ``workspace_id`` for this kind, requires
  ``subject_user_id``, and validates every scope key starts with
  ``me.`` (the router re-validates against the action catalog).

**Token shape** (§03 "API tokens / Creation"):

* ``mip_<key_id>_<secret>``
* ``key_id`` is a 26-char Crockford-base32 ULID (public; stored in
  the clear as :attr:`ApiToken.id` so every request can be O(1)
  located).
* ``secret`` is 256 bits of random drawn from
  :func:`secrets.token_bytes`, encoded as RFC 4648 base32 without
  padding — 52 characters from the alphabet ``A-Z2-7``.
* Total length: ``4 + 26 + 1 + 52 = 83`` characters. Kept opaque to
  callers: they should not parse it beyond the ``mip_`` prefix.

**Hashing** (§03 "Principles" / §15 "Token hashing"): only the
argon2id digest of the secret is stored. :class:`argon2.PasswordHasher`
applies a per-hash random salt, so two tokens sharing a secret would
still produce distinct stored values — the ``hash`` column carries
the full PHC string (``$argon2id$v=19$m=...,t=...,p=...$<salt>$<digest>``)
which the verifier re-parses on every request.

**Argon2 parameters.** ``time_cost=3, memory_cost=65536 (64 MiB),
parallelism=4`` — argon2-cffi's documented defaults. Rationale: the
secret carries 256 bits of entropy already, so the hash is not a
brute-force barrier but a *leak* barrier (if the DB is exfiltrated,
an attacker cannot replay the secret without also breaking argon2id's
memory-hard work factor). The defaults are comfortably above OWASP's
2023 floor (m=19 MiB, t=2) while remaining cheap enough for per-
request verification on modern hardware (~15 ms on a cloud VM).
Rotation (cd-c91 follow-up) will store ``time_cost`` / ``memory_cost``
in a sibling column so a parameter bump can re-hash on next use
without a big-bang migration.

**Caps** (§03 "Guardrails", task spec): 5 active tokens per user per
workspace. Creating a 6th raises :class:`TooManyTokens`, mapped to
HTTP 422 ``too_many_tokens``. The count is computed inside the mint
transaction so two concurrent creates cannot both land a 6th row.

**``last_used_at`` debouncing.** Per-request updates are the single
biggest source of write amplification for tokens — every API call
would otherwise touch the token row's PK index. We coalesce writes
to ≤1 per minute per token: :func:`verify` bumps ``last_used_at``
only when the stored value is ``NULL`` or the delta since the last
write exceeds :data:`_LAST_USED_DEBOUNCE`. Matches the spec clause
"Updated best-effort per request (coalesced to ≤1 write/minute per
token to bound write amp)" in §03 verbatim.

**Audit** (§03 "Every enrollment, login, rotation, and revocation
writes to the audit log"):

* ``audit.api_token.minted`` on :func:`mint` — carries ``token_id``,
  ``prefix``, ``label``, ``scopes`` keys. Never the plaintext token.
* ``audit.api_token.revoked`` on :func:`revoke` when a live row is
  flipped to revoked, and on :func:`revoke_personal` for PATs.
* ``audit.api_token.revoked_noop`` on :func:`revoke` when the row
  was already revoked — kept separate so the trail distinguishes an
  intentional double-click from a real revocation event.

Workspace-scoped events (``scoped`` / ``delegated``) land on the
caller's workspace; PAT events land on the tenant-agnostic identity
seam (zero-ULID workspace id + ``actor_id = subject_user_id``, see
:func:`_pat_audit_ctx`) so workspace-scoped audit views exclude
them and the ``/me`` PAT audit view can filter per-user directly.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import ApiToken
from app.audit import write_audit
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DELEGATED_DEFAULT_TTL_DAYS",
    "PERSONAL_DEFAULT_TTL_DAYS",
    "PERSONAL_SCOPE_PREFIX",
    "SCOPED_DEFAULT_TTL_DAYS",
    "InvalidToken",
    "MintedToken",
    "TokenExpired",
    "TokenKind",
    "TokenKindInvalid",
    "TokenMintFailed",
    "TokenRevoked",
    "TokenShapeError",
    "TokenSummary",
    "TooManyPersonalTokens",
    "TooManyTokens",
    "VerifiedToken",
    "list_personal_tokens",
    "list_tokens",
    "mint",
    "revoke",
    "revoke_personal",
    "verify",
]


# ``TokenKind`` is the domain vocabulary for §03's three-way
# discriminator. Defined as a ``Literal`` so callers get compile-time
# validation and the DB CHECK constraint + the service layer share a
# single source of truth for the allowed values.
TokenKind = Literal["scoped", "delegated", "personal"]


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# ``mip_`` is the crew.day standalone / delegated / personal token
# family prefix. Mirrors ``mip_<key_id>_<secret>`` in §03 "Creation".
# Kept as a module constant so the parser + builder share one rule and
# a future rename (e.g. ``crd_``) is a single-line edit.
_TOKEN_PREFIX: Final[str] = "mip_"

# 32 bytes → 256 bits of secret material per token. The base32 encoding
# below expands this to 52 characters of ASCII so the whole token is
# URL-safe without quoting. 256 bits of entropy is more than enough to
# make brute-force infeasible regardless of the argon2 parameters — the
# hash exists to blunt a DB leak, not to slow down a guessing attack.
_SECRET_BYTES: Final[int] = 32

# First 8 chars of the secret are stored in ``ApiToken.prefix`` so the
# listings page can show a human-recognisable "mip_xxxxxxxx" suffix
# without ever loading the plaintext. 8 characters of base32 carries
# 40 bits of entropy — uniquely recognisable in a manager's token list
# without leaking enough to brute-force the remaining 216 bits.
_PREFIX_CHARS: Final[int] = 8

# Per-user per-workspace active-token cap. Matches the 5-passkey cap
# (§03 "Additional passkeys") — one mental model for the end user,
# same "revoke one to add one" UX shape. Note: this is the per-user
# cap, not the §03 workspace-wide 50-token cap, which is a separate
# guardrail (tracked for follow-up under cd-c91 if needed). Applies
# to ``scoped`` + ``delegated`` tokens on the same workspace;
# ``personal`` tokens carry their own per-subject 5-token cap below.
_MAX_ACTIVE_TOKENS_PER_USER: Final[int] = 5

# Per-subject personal-access-token cap (§03 "Personal access tokens"
# guardrails). Separate from the workspace-scoped cap above because
# PATs live at the identity scope — a user with 5 scoped tokens on a
# workspace can still hold 5 PATs.
_MAX_PERSONAL_TOKENS_PER_USER: Final[int] = 5

# Default TTLs per kind. The router still owns the HTTP-surface
# default (so the 201 response carries the expected ``expires_at``),
# but the service layer mirrors the constant so direct callers (CLI,
# worker) don't have to import the router module for a policy value.
# §03 "Guardrails": "Scoped tokens default to 90 days TTL if
# ``expires_at_days`` is omitted; delegated tokens default to 30 days;
# personal access tokens default to 90 days."
SCOPED_DEFAULT_TTL_DAYS: Final[int] = 90
DELEGATED_DEFAULT_TTL_DAYS: Final[int] = 30
PERSONAL_DEFAULT_TTL_DAYS: Final[int] = 90

# Scope-key prefix every PAT scope MUST carry. §03 "Personal access
# tokens" pins the ``me:*`` family: ``me.tasks:read``,
# ``me.bookings:read``, etc. The dot separator between ``me`` and the
# resource narrows the family in a way that can't be confused with a
# workspace scope (``tasks:read``) — mixing the two on the same
# token is a 422 ``me_scope_conflict``.
PERSONAL_SCOPE_PREFIX: Final[str] = "me."

# ``last_used_at`` write debounce. A heavily-used token (an agent
# polling every few seconds) would otherwise hammer its row's PK
# index on every request; the debounce drops the write rate to
# ≤1/min per token — the exact ceiling §03 pins.
_LAST_USED_DEBOUNCE: Final[timedelta] = timedelta(minutes=1)


# Sentinel workspace id for tenant-agnostic (identity-scope) audit
# rows — PAT mint / revoke have no workspace to borrow. Mirrors the
# private constants in :mod:`app.auth.session`, :mod:`app.auth.magic_link`,
# and :mod:`app.api.v1.auth.me_avatar`. cd-rqhy promotes all four
# copies to a shared helper; until then each module redefines locally
# rather than reaching across module boundaries for a private symbol.
_AGNOSTIC_WORKSPACE_ID: Final[str] = "00000000000000000000000000"


# argon2-cffi's ``PasswordHasher`` is thread-safe and stateless once
# constructed, so sharing a single instance across the process is
# cheap. The parameters match the v1 choice documented in the module
# docstring; a follow-up rotation task (cd-c91 extension) wires a
# per-token ``hash_params`` column so a parameter bump can rehash
# lazily on next :func:`verify`.
_HASHER: Final[PasswordHasher] = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MintedToken:
    """Result of :func:`mint` — the plaintext token, shown to the user once.

    The caller (HTTP router) surfaces ``token`` in the 201 response
    body and the mobile / CLI clients echo it back with every
    subsequent request. After the response lands there is no way to
    retrieve the plaintext again — only :attr:`ApiToken.hash` remains
    in the database, so a lost token forces the user to mint a new
    one.

    ``kind`` echoes the domain discriminator so the caller can render
    the right UI chrome ("Delegated as Alice", "Personal") without a
    follow-up fetch.
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None
    kind: TokenKind


@dataclass(frozen=True, slots=True)
class TokenSummary:
    """Public projection of one :class:`ApiToken` row for list / audit UIs.

    Mirrors §03 "Revocation and rotation" / §14's ``/tokens`` panel:
    every field is safe to show to any workspace manager, none of
    them leak the plaintext secret. ``hash`` is deliberately
    **omitted** — the list surface never needs it, and leaving it
    off the projection makes it structurally impossible for a router
    to return the digest by mistake.

    ``kind`` + ``delegate_for_user_id`` + ``subject_user_id`` surface
    the cd-i1qe discriminator so the ``/tokens`` UI (workspace view)
    can flag "delegated as Alice" rows and the ``/me`` UI (personal
    view) can list PATs without rejoining :class:`User` or reparsing
    the token. The list endpoints narrow by kind where appropriate
    (manager /tokens surface omits personal; /me surface omits
    scoped / delegated); the projection is shared so both routers
    read the same shape.
    """

    key_id: str
    label: str
    prefix: str
    scopes: Mapping[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    kind: TokenKind
    delegate_for_user_id: str | None
    subject_user_id: str | None


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """Result of :func:`verify` — the identity + authority the token grants.

    The caller (tenancy middleware, once cd-ika7 lands) uses
    ``user_id`` + ``workspace_id`` to build the request's
    :class:`WorkspaceContext`, and walks ``scopes`` at the action-catalog
    seam to gate the action. ``key_id`` is echoed into audit so every
    write made through this token is traceable back to one row on the
    ``/tokens`` page.

    ``workspace_id`` is **nullable** because ``personal`` tokens live
    at the identity scope (no workspace pin). The router-level gate
    in the workspace-scoped tree must reject a ``workspace_id is None``
    verify result as ``404 workspace_out_of_scope`` — the domain
    service returns the raw shape and lets the caller decide how to
    surface the mismatch. ``kind`` is echoed so the caller can branch
    on the three families (e.g. delegated → walk the user's grants,
    scoped → walk ``scopes``, personal → narrow to subject).
    """

    user_id: str
    workspace_id: str | None
    scopes: Mapping[str, Any]
    key_id: str
    kind: TokenKind
    delegate_for_user_id: str | None
    subject_user_id: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidToken(ValueError):
    """Token format is malformed or its ``key_id`` doesn't resolve.

    Collapsed shape: malformed prefix, wrong segment count, unknown
    ``key_id``, and "secret didn't verify" all raise this same type so
    the HTTP layer can't fingerprint which failure mode fired. The
    router maps it to 401 on the Bearer-auth path and 404 on the
    management path (§03 distinguishes "is this a live credential?"
    from "does this credential exist on the tokens list?").
    """


class TokenExpired(ValueError):
    """Token row exists but ``expires_at`` has passed.

    401-equivalent. Kept distinct from :class:`InvalidToken` so
    metrics can separate "expired tokens still in use" (a client
    that missed the rotation) from "unknown credential" (a probe).
    The HTTP response shape stays opaque.
    """


class TokenRevoked(ValueError):
    """Token row has ``revoked_at`` set.

    401-equivalent, same opaque-response pattern as
    :class:`TokenExpired`.
    """


class TokenMintFailed(RuntimeError):
    """:func:`mint` could not produce a token — internal error.

    Reserved for structural failures (argon2 hasher threw, RNG
    refused). Not mapped to a typed HTTP error — the router lets it
    bubble to 500 so the operator sees the traceback.
    """


class TooManyTokens(ValueError):
    """User already holds :data:`_MAX_ACTIVE_TOKENS_PER_USER` live tokens.

    422-equivalent — the HTTP layer maps to ``too_many_tokens`` with
    the 5-token cap in the message so the UI can surface "revoke
    one to add another" without hard-coding the number. The count
    is computed inside the mint transaction so concurrent creates
    cannot both land a 6th row. Applies to ``scoped`` + ``delegated``
    tokens on the same workspace; ``personal`` tokens get their own
    :class:`TooManyPersonalTokens`.
    """


class TooManyPersonalTokens(ValueError):
    """User already holds :data:`_MAX_PERSONAL_TOKENS_PER_USER` live PATs.

    422-equivalent — §03 "Personal access tokens" guardrails pin the
    per-user cap at 5, separate from the workspace-scoped cap. The
    HTTP layer maps to ``too_many_personal_tokens`` per spec.
    """


class TokenKindInvalid(ValueError):
    """Caller asked to mint a kind outside :data:`TokenKind`.

    422-equivalent. Raised before any DB work so a typo in a CLI
    never reaches argon2.
    """


class TokenShapeError(ValueError):
    """Mint arguments violate a per-kind invariant (§03 "API tokens").

    Shape violations that map to 422 validation errors at the HTTP
    layer:

    * ``scoped`` without a workspace, or with ``delegate_for_user_id``
      / ``subject_user_id`` populated;
    * ``delegated`` without a ``delegate_for_user_id``, or with
      non-empty scopes;
    * ``personal`` with a ``workspace_id``, or with a scope key
      outside the ``me:*`` family, or with an empty scope dict.

    The router maps each case to its spec-specific error code
    (``me_scope_conflict`` / ``scopes_required`` / ``kind_conflict``);
    the service layer collapses them into one error type with a
    human message so the router owns the code taxonomy in one place.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _pat_audit_ctx(subject_user_id: str) -> WorkspaceContext:
    """Return a tenant-agnostic :class:`WorkspaceContext` for a PAT audit row.

    Personal access tokens live at the identity scope — they have no
    workspace, so the usual :func:`app.audit.write_audit` path (which
    demands a live :class:`WorkspaceContext`) has nothing to borrow.
    We mint a synthetic context that pins:

    * ``workspace_id`` to the zero-ULID sentinel shared with
      :mod:`app.auth.session`, :mod:`app.auth.magic_link`,
      :mod:`app.auth.signup`, and :mod:`app.auth.recovery` — the
      audit reader recognises that value as "identity-scope event"
      and workspace-scoped views naturally exclude it.
    * ``actor_id`` to the **real** subject user's id (not the
      zero-ULID). The ``/me`` "Personal access tokens" audit view
      (§03, §14) filters on
      ``workspace_id=<zero-ulid> AND actor_id=<user>``, so the
      subject's id has to land on the row itself — putting it only
      inside ``diff`` would force a JSON scan on every read. The
      four sibling auth-module helpers use the zero-ULID actor
      because their events (magic link consumed, session revoked
      "everywhere", …) do not yet have a bound user at the moment
      of emission; a PAT mint / revoke always does.
    * ``actor_kind`` to ``"user"`` (the domain literal). The row
      represents a user-initiated rotation / revocation, not a
      system worker firing on schedule.

    ``actor_grant_role`` and ``actor_was_owner_member`` are unused
    for this event family (PATs grant no workspace authority) and
    follow the neutral defaults the sibling helpers pick. The
    correlation id is fresh per call so sibling writes (rare — each
    mint / revoke is a single audit row) still get their own
    trace cursor.
    """
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=subject_user_id,
        actor_kind="user",
        actor_grant_role="manager",  # unused for identity-scope events
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _generate_secret() -> str:
    """Return a 52-char RFC 4648 base32 secret with padding stripped.

    :func:`secrets.token_bytes(32)` draws 32 bytes from the OS CSPRNG;
    :func:`base64.b32encode` produces 56 chars with 4 trailing ``=``
    pads, which we strip. The result is URL-safe (base32's
    ``A-Z2-7`` alphabet), fixed length (52), and case-insensitive —
    the same shape as ULIDs so it round-trips cleanly through
    Authorization headers and shell one-liners without quoting.
    """
    raw = secrets.token_bytes(_SECRET_BYTES)
    # ``b32encode`` always pads to a multiple of 8 characters; for 32
    # bytes of input that's 56 chars with 4 ``=`` suffix. Stripping
    # padding is lossless because the decoder re-derives it from the
    # remaining length (we never decode — this is a verifier-side
    # opaque string — but the shape is still predictable).
    encoded = base64.b32encode(raw).rstrip(b"=").decode("ascii")
    return encoded


def _parse(token: str) -> tuple[str, str]:
    """Return ``(key_id, secret)`` or raise :class:`InvalidToken`.

    Format: ``mip_<key_id>_<secret>``. We split on the **first two**
    underscores only — every downstream character belongs to the
    secret, including any ``_`` that could appear in a future
    encoding. Today's base32 alphabet excludes ``_``, so the current
    secret portion never carries one, but keeping the parse
    future-proof costs nothing.
    """
    if not token.startswith(_TOKEN_PREFIX):
        raise InvalidToken("token does not start with 'mip_'")
    body = token[len(_TOKEN_PREFIX) :]
    # ``split("_", 1)`` keeps the secret unsplit if it ever gains an
    # underscore — we still split on exactly one separator between
    # key_id and secret.
    parts = body.split("_", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidToken("token body is not <key_id>_<secret>")
    return parts[0], parts[1]


def _count_active_workspace(
    session: Session, *, user_id: str, workspace_id: str, now: datetime
) -> int:
    """Return the number of live ``scoped`` + ``delegated`` tokens on a workspace.

    Runs under :func:`tenant_agnostic` because ``api_token`` isn't a
    workspace-scoped table in the ORM filter sense (see
    :mod:`app.adapters.db.identity` docstring) — scoping is explicit
    on the ``workspace_id`` column rather than a registered tenant
    filter.

    "Live" means ``revoked_at IS NULL`` **and** ``expires_at IS NULL
    OR expires_at > now``. Expired-but-not-revoked tokens shouldn't
    count against the cap because they're effectively inert; a user
    with 5 dead tokens gathering dust on their /tokens page would
    otherwise be stuck.

    ``personal`` tokens are **excluded** by the ``kind != 'personal'``
    predicate — they get their own per-subject cap via
    :func:`_count_active_personal`. The workspace CAP is about
    "how many workspace-scoped authorities has this user minted
    here"; a PAT doesn't live on this workspace, so counting it
    would over-restrict a user that happens to hold a workspace
    grant + some PATs.
    """
    # justification: api_token is identity-scoped; the ORM tenant
    # filter has no predicate registered for this table and would
    # otherwise refuse the read under a live WorkspaceContext.
    with tenant_agnostic():
        stmt = (
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.user_id == user_id,
                ApiToken.workspace_id == workspace_id,
                ApiToken.revoked_at.is_(None),
                ApiToken.kind != "personal",
            )
        )
        # Expiry gate: ``expires_at IS NULL`` (no-expiry tokens, via
        # the workspace-override setting) OR ``expires_at > now``
        # (still live). We build the predicate inline because
        # SQLAlchemy's ``func.coalesce`` would require a fallback
        # sentinel that outlives real timestamps, which is harder to
        # reason about than the two-branch OR.
        stmt = stmt.where((ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now))
        return session.scalar(stmt) or 0


def _count_active_personal(
    session: Session, *, subject_user_id: str, now: datetime
) -> int:
    """Return the number of live PATs for a given subject user.

    Per-subject cap (§03 "Personal access tokens" guardrails): 5
    PATs per user, separate from the workspace-scoped cap. "Live"
    follows the same rule as :func:`_count_active_workspace`
    (``revoked_at IS NULL`` and unexpired).
    """
    with tenant_agnostic():
        stmt = (
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.subject_user_id == subject_user_id,
                ApiToken.kind == "personal",
                ApiToken.revoked_at.is_(None),
            )
        )
        stmt = stmt.where((ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now))
        return session.scalar(stmt) or 0


def _normalise_expires_at(value: datetime, now: datetime) -> datetime:
    """Return ``value`` as an aware datetime aligned to ``now``'s tzinfo.

    SQLite's ``DateTime(timezone=True)`` drops tzinfo on roundtrip;
    :func:`verify` compares the round-tripped value against ``now``
    and needs both sides to share an offset. This mirrors the
    pattern used by :mod:`app.auth.session` and
    :mod:`app.auth.passkey`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=now.tzinfo)
    return value


def _maybe_bump_last_used(row: ApiToken, *, now: datetime) -> bool:
    """Return ``True`` and mutate ``row.last_used_at`` if the debounce allows.

    The write actually lands when the caller's UoW flushes — we only
    mutate the ORM-attached instance here. Keeping the decision
    read-only on ``now - row.last_used_at`` means two concurrent
    verifies hitting the same token within the debounce window
    collapse cleanly (they both see "no bump needed" and neither
    write races the other). The first verify past the window wins
    the update; any sibling concurrent verify past the window will
    also bump, which is fine — the ``last_used_at`` column is
    best-effort and idempotent at the minute granularity we care
    about.
    """
    last = row.last_used_at
    if last is None:
        row.last_used_at = now
        return True
    normalised = _normalise_expires_at(last, now)
    if (now - normalised) >= _LAST_USED_DEBOUNCE:
        row.last_used_at = now
        return True
    return False


def _narrow_kind(value: str) -> TokenKind:
    """Narrow a raw DB string to the :data:`TokenKind` literal.

    The CHECK constraint guarantees only the three allowed values
    ever land on disk; the narrow is defensive against a future
    hand-edited row and gives mypy the specific literal type the
    projection + verify return shapes depend on. A truly unknown
    value raises :class:`TokenKindInvalid` so the caller sees a
    domain error instead of a silent collapse.
    """
    if value == "scoped":
        return "scoped"
    if value == "delegated":
        return "delegated"
    if value == "personal":
        return "personal"
    raise TokenKindInvalid(f"unknown token kind {value!r}")


def _project(row: ApiToken) -> TokenSummary:
    """Project an :class:`ApiToken` ORM row onto the public summary.

    Hash column is intentionally absent — see :class:`TokenSummary`
    docstring.
    """
    return TokenSummary(
        key_id=row.id,
        label=row.label,
        prefix=row.prefix,
        scopes=dict(row.scope_json),
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        kind=_narrow_kind(row.kind),
        delegate_for_user_id=row.delegate_for_user_id,
        subject_user_id=row.subject_user_id,
    )


# ---------------------------------------------------------------------------
# Public surface — mint
# ---------------------------------------------------------------------------


def _validate_scoped_shape(
    *, scopes: Mapping[str, Any], ctx: WorkspaceContext | None
) -> None:
    """Raise :class:`TokenShapeError` if a scoped-token mint is malformed."""
    if ctx is None:
        raise TokenShapeError("scoped tokens require a WorkspaceContext")
    for key in scopes:
        if key.startswith(PERSONAL_SCOPE_PREFIX):
            # §03 "Personal access tokens": mixing me:* with workspace
            # scopes is a hard error, flagged as ``me_scope_conflict``
            # at the router.
            raise TokenShapeError(f"scoped token must not carry me:* scope {key!r}")


def _validate_delegated_shape(
    *,
    scopes: Mapping[str, Any],
    ctx: WorkspaceContext | None,
    delegate_for_user_id: str | None,
) -> None:
    """Raise :class:`TokenShapeError` if a delegated-token mint is malformed."""
    if ctx is None:
        raise TokenShapeError("delegated tokens require a WorkspaceContext")
    if delegate_for_user_id is None:
        raise TokenShapeError(
            "delegated tokens require delegate_for_user_id (the session user's id)"
        )
    if scopes:
        # §03 "Delegated tokens": "scopes: empty. Permission checks
        # resolve against the delegating user's role_grants." A
        # non-empty scopes dict would give the agent a narrower
        # authority than the spec reserves; reject to keep the
        # invariant obvious to callers.
        raise TokenShapeError("delegated tokens must have empty scopes")


def _validate_personal_shape(
    *,
    scopes: Mapping[str, Any],
    ctx: WorkspaceContext | None,
    subject_user_id: str | None,
) -> None:
    """Raise :class:`TokenShapeError` if a PAT mint is malformed."""
    if ctx is not None:
        raise TokenShapeError(
            "personal access tokens are identity-scoped; pass ctx=None"
        )
    if subject_user_id is None:
        raise TokenShapeError(
            "personal access tokens require subject_user_id (the session user's id)"
        )
    if not scopes:
        raise TokenShapeError("personal access tokens require at least one me:* scope")
    for key in scopes:
        if not key.startswith(PERSONAL_SCOPE_PREFIX):
            raise TokenShapeError(
                f"personal access tokens accept only me:* scopes — got {key!r}"
            )


def mint(
    session: Session,
    ctx: WorkspaceContext | None,
    *,
    user_id: str,
    label: str,
    scopes: Mapping[str, Any],
    expires_at: datetime | None,
    kind: TokenKind = "scoped",
    delegate_for_user_id: str | None = None,
    subject_user_id: str | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> MintedToken:
    """Create a fresh :class:`ApiToken` row and return the plaintext token.

    The caller's UoW owns the commit; on successful return the row
    exists with ``revoked_at = NULL``, the audit row is queued, and
    :attr:`MintedToken.token` is the **only** place the plaintext
    ever appears — the caller surfaces it in the HTTP response and
    never again.

    **Per-kind contract** (§03 "API tokens"):

    * ``kind='scoped'`` (default) — pass a live :class:`WorkspaceContext`
      and a ``scopes`` dict of workspace-level action keys. Do NOT
      pass ``delegate_for_user_id`` / ``subject_user_id``.
    * ``kind='delegated'`` — pass a live :class:`WorkspaceContext`
      AND ``delegate_for_user_id`` (the session user's id). ``scopes``
      MUST be empty (delegated tokens inherit the user's grants per
      §03 "Delegated tokens").
    * ``kind='personal'`` — pass ``ctx=None`` AND ``subject_user_id``
      (the session user's id). ``scopes`` MUST be non-empty and every
      key MUST start with ``me.`` (the ``me:*`` scope family,
      §03 "Personal access tokens"). The resulting row carries
      ``workspace_id=NULL``.

    Raises:

    * :class:`TokenKindInvalid` — ``kind`` is outside :data:`TokenKind`.
    * :class:`TokenShapeError` — per-kind input shape invariant
      violated (missing / extra fields, ``me:*`` vs workspace scope
      mixing, empty scope set on PAT, non-empty scope set on delegated).
    * :class:`TooManyTokens` — scoped / delegated cap of 5 live tokens
      per user per workspace tripped. Count is workspace-scoped so PAT
      holders can still mint workspace tokens.
    * :class:`TooManyPersonalTokens` — per-user PAT cap of 5 tripped.
    * :class:`TokenMintFailed` — structural failure (argon2 refused,
      RNG refused). Rare enough to bubble as 500.

    Every successful mint emits one ``api_token.minted`` audit row
    with ``kind`` stamped into its diff so the ``/tokens`` page can
    filter (§03 "per-token audit log view") and the downstream
    owner-revoke path can walk delegated tokens per delegating-user
    without re-joining. Workspace-scoped mints land on the caller's
    workspace via ``ctx``; PAT mints land on the tenant-agnostic
    identity seam (zero-ULID workspace id, real subject user as the
    actor) so the ``/me`` audit view can filter per-user without a
    JSON scan.
    """
    resolved_now = now if now is not None else _now(clock)

    # Narrow to the domain literal before any DB work so a CLI typo
    # (``kind='scopped'``) fails cheap and clear.
    if kind not in ("scoped", "delegated", "personal"):
        raise TokenKindInvalid(f"unknown token kind {kind!r}")

    # Per-kind input-shape validation. Each branch raises
    # :class:`TokenShapeError` with a human message the router
    # translates into the spec's error taxonomy (``me_scope_conflict``,
    # ``scopes_required``, etc.).
    if kind == "scoped":
        _validate_scoped_shape(scopes=scopes, ctx=ctx)
    elif kind == "delegated":
        _validate_delegated_shape(
            scopes=scopes, ctx=ctx, delegate_for_user_id=delegate_for_user_id
        )
    else:
        _validate_personal_shape(
            scopes=scopes, ctx=ctx, subject_user_id=subject_user_id
        )

    # Cap enforcement — distinct quotas per kind. Run BEFORE hashing
    # so a rejected request doesn't burn an argon2 cycle.
    if kind in ("scoped", "delegated"):
        # ``ctx`` is guaranteed non-None here by the shape validators
        # above; mypy needs the explicit narrowing so the attribute
        # access below type-checks.
        assert ctx is not None
        active = _count_active_workspace(
            session,
            user_id=user_id,
            workspace_id=ctx.workspace_id,
            now=resolved_now,
        )
        if active >= _MAX_ACTIVE_TOKENS_PER_USER:
            raise TooManyTokens(
                f"user {user_id!r} already has {active} active workspace tokens "
                f"(max {_MAX_ACTIVE_TOKENS_PER_USER})"
            )
    else:
        assert subject_user_id is not None
        active_pat = _count_active_personal(
            session,
            subject_user_id=subject_user_id,
            now=resolved_now,
        )
        if active_pat >= _MAX_PERSONAL_TOKENS_PER_USER:
            raise TooManyPersonalTokens(
                f"user {subject_user_id!r} already has {active_pat} active personal "
                f"tokens (max {_MAX_PERSONAL_TOKENS_PER_USER})"
            )

    key_id = new_ulid(clock=clock)
    secret = _generate_secret()
    prefix = secret[:_PREFIX_CHARS]

    # argon2-cffi raises subclasses of :class:`Argon2Error` on
    # structural failure (parameters out of range, hash refused by the
    # native lib). Rewrap into the domain vocabulary so the HTTP layer
    # doesn't have to reach past the seam; the cause chain preserves
    # the upstream message for operator logs. We narrow to
    # ``Argon2Error`` specifically — anything else is a programming
    # bug that should bubble as a 500 with full traceback.
    try:
        hash_value = _HASHER.hash(secret)
    except Argon2Error as exc:
        raise TokenMintFailed(f"argon2id hash failed: {exc}") from exc

    workspace_id: str | None = ctx.workspace_id if ctx is not None else None
    row = ApiToken(
        id=key_id,
        user_id=user_id,
        workspace_id=workspace_id,
        kind=kind,
        delegate_for_user_id=delegate_for_user_id if kind == "delegated" else None,
        subject_user_id=subject_user_id if kind == "personal" else None,
        label=label,
        scope_json=dict(scopes),
        prefix=prefix,
        hash=hash_value,
        expires_at=expires_at,
        last_used_at=None,
        revoked_at=None,
        created_at=resolved_now,
    )
    # justification: api_token is identity-scoped; writing under a
    # live WorkspaceContext would otherwise force the ORM filter to
    # inject a predicate the table doesn't carry.
    with tenant_agnostic():
        session.add(row)
        session.flush()

    # Every mint writes an audit row. Workspace-scoped tokens
    # (``scoped`` / ``delegated``) land on the caller's workspace via
    # ``ctx``; PATs land on the tenant-agnostic identity seam via
    # :func:`_pat_audit_ctx` — zero-ULID workspace id + real subject
    # user id as the actor, so the ``/me`` audit view can filter on
    # ``workspace_id=<zero-ulid> AND actor_id=<user>`` without a JSON
    # scan (§03 "API tokens", §14 "/me Personal access tokens").
    if ctx is not None:
        write_audit(
            session,
            ctx,
            entity_kind="api_token",
            entity_id=key_id,
            action="api_token.minted",
            diff={
                "token_id": key_id,
                "user_id": user_id,
                "workspace_id": ctx.workspace_id,
                "label": label,
                "prefix": prefix,
                "scopes": sorted(scopes.keys()),
                "expires_at": (
                    expires_at.isoformat() if expires_at is not None else None
                ),
                "kind": kind,
                "delegate_for_user_id": (
                    delegate_for_user_id if kind == "delegated" else None
                ),
            },
            clock=clock,
        )
    else:
        # ``subject_user_id`` is guaranteed non-None on this branch by
        # :func:`_validate_personal_shape` above; mypy needs the
        # explicit narrowing for the helper call.
        assert subject_user_id is not None
        write_audit(
            session,
            _pat_audit_ctx(subject_user_id=subject_user_id),
            entity_kind="api_token",
            entity_id=key_id,
            action="api_token.minted",
            diff={
                "token_id": key_id,
                "user_id": user_id,
                "subject_user_id": subject_user_id,
                "label": label,
                "prefix": prefix,
                "scopes": sorted(scopes.keys()),
                "expires_at": (
                    expires_at.isoformat() if expires_at is not None else None
                ),
                "kind": kind,  # always "personal" on this branch
            },
            clock=clock,
        )

    return MintedToken(
        token=f"{_TOKEN_PREFIX}{key_id}_{secret}",
        key_id=key_id,
        prefix=prefix,
        expires_at=expires_at,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Public surface — list_tokens
# ---------------------------------------------------------------------------


def list_tokens(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
) -> list[TokenSummary]:
    """Return every ``scoped`` / ``delegated`` token on the caller's workspace.

    ``user_id`` narrows to one subject when set. Workspace managers
    call with ``user_id=None`` to audit every workspace token; the
    list includes both active and revoked rows (the UI shows the
    revoked-history tail).

    **Personal access tokens are deliberately excluded** — §03 "PATs
    are not listed on the workspace-wide /tokens admin page". A
    manager's audit view should not surface "every worker's printer
    script"; PATs are governed by the subject user on ``/me``. Use
    :func:`list_personal_tokens` for the subject-side listing.

    The projection intentionally omits the hash column — see
    :class:`TokenSummary` docstring for why.
    """
    # justification: api_token is identity-scoped; reuse of the
    # tenant-agnostic gate mirrors ``_count_active_workspace``.
    with tenant_agnostic():
        stmt = (
            select(ApiToken)
            .where(
                ApiToken.workspace_id == ctx.workspace_id,
                ApiToken.kind != "personal",
            )
            .order_by(ApiToken.created_at.desc())
        )
        if user_id is not None:
            stmt = stmt.where(ApiToken.user_id == user_id)
        rows = list(session.scalars(stmt).all())
    return [_project(row) for row in rows]


def list_personal_tokens(
    session: Session,
    *,
    subject_user_id: str,
) -> list[TokenSummary]:
    """Return every PAT (active + revoked) for a given subject user.

    Identity-scoped — no :class:`WorkspaceContext` needed because
    PATs live outside any workspace. The ``/me`` "Personal access
    tokens" panel (§14, cd-i1qe-me-panel follow-up) reads through
    this surface.
    """
    with tenant_agnostic():
        stmt = (
            select(ApiToken)
            .where(
                ApiToken.subject_user_id == subject_user_id,
                ApiToken.kind == "personal",
            )
            .order_by(ApiToken.created_at.desc())
        )
        rows = list(session.scalars(stmt).all())
    return [_project(row) for row in rows]


# ---------------------------------------------------------------------------
# Public surface — revoke
# ---------------------------------------------------------------------------


def revoke(
    session: Session,
    ctx: WorkspaceContext,
    *,
    token_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> None:
    """Revoke a ``scoped`` / ``delegated`` token on the caller's workspace.

    Idempotent on an already-revoked row. The row is **not** deleted —
    keeping it preserves the link target for existing audit rows
    that reference this token_id (§03 "per-token audit log view").
    A revoked row still appears on the /tokens page in the
    "decommissioned" section.

    **Personal access tokens are refused here.** §03 "Revocation":
    "Personal access tokens are revocable only by their subject
    user or via a cascade" — a manager on the workspace /tokens
    page cannot revoke a worker's PAT directly. A PAT token_id
    surfaced on this router therefore collapses to
    :class:`InvalidToken` (404), same shape as "unknown token";
    the router maps it to ``token_not_found``.

    Raises:

    * :class:`InvalidToken` — no row with this id on the caller's
      workspace, OR the row is a PAT (which isn't workspace-managed).
      Both map to 404 at the router.

    A second call with the same ``token_id`` lands no state change
    but still writes an ``api_token.revoked_noop`` audit row so the
    trail distinguishes a double-click from the initial revocation.
    """
    resolved_now = now if now is not None else _now(clock)

    # justification: api_token is identity-scoped; read under
    # tenant_agnostic for consistency with the other accessors.
    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    # Fail-closed on cross-workspace access, unknown ids, AND
    # personal tokens (which live outside any workspace). All three
    # collapse into the same 404 shape at the HTTP layer so the API
    # doesn't leak which of the three actually happened.
    if row is None or row.kind == "personal" or row.workspace_id != ctx.workspace_id:
        raise InvalidToken(f"token {token_id!r} not found on this workspace")

    if row.revoked_at is not None:
        # Idempotent no-op — leave the existing ``revoked_at``
        # untouched so the trail keeps the original revocation time.
        write_audit(
            session,
            ctx,
            entity_kind="api_token",
            entity_id=token_id,
            action="api_token.revoked_noop",
            diff={
                "token_id": token_id,
                "already_revoked_at": row.revoked_at.isoformat(),
                "at": resolved_now.isoformat(),
            },
            clock=clock,
        )
        return

    with tenant_agnostic():
        row.revoked_at = resolved_now
        session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="api_token",
        entity_id=token_id,
        action="api_token.revoked",
        diff={
            "token_id": token_id,
            "user_id": row.user_id,
            "workspace_id": row.workspace_id,
            "at": resolved_now.isoformat(),
            "kind": row.kind,
        },
        clock=clock,
    )


def revoke_personal(
    session: Session,
    *,
    token_id: str,
    subject_user_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> None:
    """Revoke a PAT owned by ``subject_user_id``. Identity-scoped.

    §03 "Personal access tokens are revocable only by their subject
    user" — the caller passes the session user's id and the row is
    only revoked if it matches. A mismatch, a workspace token, or an
    unknown id all collapse into :class:`InvalidToken` (404) so the
    API doesn't leak whose tokens exist.

    Writes one ``api_token.revoked`` audit row through the tenant-
    agnostic identity seam (see :func:`_pat_audit_ctx`) so the
    ``/me`` "Personal access tokens" audit view has a trail. An
    already-revoked row is an idempotent no-op and does NOT write a
    second row — matching the workspace-side :func:`revoke`
    precedent of "one revoke event per token lifetime" (the
    workspace path writes an ``api_token.revoked_noop`` for the
    double-click; PATs don't currently need that distinction).
    """
    resolved_now = now if now is not None else _now(clock)

    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    if row is None or row.kind != "personal" or row.subject_user_id != subject_user_id:
        raise InvalidToken(f"personal token {token_id!r} not found for this user")

    if row.revoked_at is not None:
        # Idempotent no-op — no second audit row, matching the
        # "one revoke per token lifetime" invariant.
        return

    with tenant_agnostic():
        row.revoked_at = resolved_now
        session.flush()

    write_audit(
        session,
        _pat_audit_ctx(subject_user_id=subject_user_id),
        entity_kind="api_token",
        entity_id=token_id,
        action="api_token.revoked",
        diff={
            "token_id": token_id,
            "subject_user_id": subject_user_id,
            "at": resolved_now.isoformat(),
            "kind": "personal",
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public surface — verify
# ---------------------------------------------------------------------------


def verify(
    session: Session,
    *,
    token: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> VerifiedToken:
    """Return the :class:`VerifiedToken` for a valid plaintext token.

    Resolution:

    1. Parse ``mip_<key_id>_<secret>``. Malformed → :class:`InvalidToken`.
    2. Look up by ``id = key_id``. Missing → :class:`InvalidToken`.
    3. ``revoked_at is not None`` → :class:`TokenRevoked`.
    4. ``expires_at <= now`` (when set) → :class:`TokenExpired`.
    5. Verify the secret with argon2id. Mismatch →
       :class:`InvalidToken` — wrapping argon2's
       :class:`VerifyMismatchError` so the caller sees the domain
       vocabulary only.
    6. Debounced ``last_used_at`` bump — see module docstring.

    Caller's UoW owns the transaction; this function never commits.
    A successful verify returns the ``user_id``, ``workspace_id``,
    and ``scopes`` the caller needs to authorise the action; the
    middleware (cd-ika7) walks ``scopes`` at the action catalog
    seam to decide whether to admit the request.

    The return deliberately does **not** enforce a
    :class:`WorkspaceContext` match — the service layer returns the
    row's ``workspace_id`` and the caller asserts the route's
    workspace agrees with the token's (§03 "A scoped token used
    against the wrong workspace returns 404 workspace_out_of_scope"
    is enforced at the router seam). Keeping the match at the
    router keeps the domain service usable from contexts that don't
    yet have a tenancy middleware (CLI, worker).

    .. note:: **Delegator / subject liveness gap (cd-et6y).** §03
       "Delegated tokens" requires a 401 when the delegating user
       is archived, globally deactivated, or has lost every
       non-revoked grant; §03 "Personal access tokens" carries the
       same rule against the subject user. This function does NOT
       implement that check today — :class:`User` does not yet
       carry an ``archived_at`` column (landing under cd-65kn /
       the Users identity hardening follow-up). Until cd-et6y is
       closed a delegated token continues to verify after the
       user is archived; this is a known spec gap and is a
       **blocker on any prod release**.
    """
    resolved_now = now if now is not None else _now(clock)

    key_id, secret = _parse(token)

    # justification: api_token is identity-scoped; reuse of the
    # tenant-agnostic gate mirrors the other accessors.
    with tenant_agnostic():
        row = session.get(ApiToken, key_id)
    if row is None:
        raise InvalidToken(f"no token with key_id {key_id!r}")

    if row.revoked_at is not None:
        raise TokenRevoked(f"token {key_id!r} revoked at {row.revoked_at}")

    if row.expires_at is not None:
        expires_at = _normalise_expires_at(row.expires_at, resolved_now)
        if expires_at <= resolved_now:
            raise TokenExpired(f"token {key_id!r} expired at {expires_at}")

    try:
        _HASHER.verify(row.hash, secret)
    except VerifyMismatchError as exc:
        # Collapse into the opaque "not a real token" shape so
        # metrics / HTTP cannot tell a mangled secret apart from
        # an unknown ``key_id`` at the wire.
        raise InvalidToken(f"token {key_id!r} secret did not verify") from exc

    # Debounced best-effort update. We don't write to audit for this
    # — the /tokens UI reads ``last_used_at`` directly from the row
    # and the audit trail already captures the high-value events
    # (mint + revoke).
    if _maybe_bump_last_used(row, now=resolved_now):
        with tenant_agnostic():
            session.flush()

    return VerifiedToken(
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        scopes=dict(row.scope_json),
        key_id=row.id,
        kind=_narrow_kind(row.kind),
        delegate_for_user_id=row.delegate_for_user_id,
        subject_user_id=row.subject_user_id,
    )
