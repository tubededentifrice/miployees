"""Self-serve signup domain service.

Three public entry points wire the spec's four-step flow (§03
"Self-serve signup"):

1. :func:`start_signup` — validate slug + reserved + homoglyph + grace
   period, insert the :class:`SignupAttempt`, mint the magic link,
   audit ``signup.requested``.
2. :func:`consume_verify` — consume the magic link with
   ``expected_purpose='signup_verify'``, flip the attempt's
   ``verified_at`` timestamp, audit ``signup.verified``.
3. :func:`complete_signup` — one transaction: create ``workspace`` +
   ``user`` + ``user_workspace`` + four system permission groups +
   ``permission_group_member`` in ``owners@<ws>`` + ``role_grant`` +
   finish the passkey ceremony. Audit ``signup.completed``.

Plus :func:`prune_stale_signups`, the GC callable invoked by the
future APScheduler wiring (see module note at the end of this file).

**Atomicity.** :func:`complete_signup` never calls ``session.commit()``
— the caller's UoW owns the transaction boundary (§01). An exception
in any of the insert steps (passkey verification failure, DB integrity
error) propagates, the UoW rolls back, and the workspace / user /
grant / credential rows are all reverted together. No partial state.

**Enumeration guard — limits.** Slug-related errors at ``start_signup``
DO surface to the client (409 ``slug_taken`` carries a suggested
alternative in the body, per spec). The enumeration guard only covers
the "does this email already exist?" question, which is
email-orthogonal here: signup does NOT care whether the caller's
email already has a ``user`` row (different workspace, different
signup story). The email-hash rate limit is the caller's defence.

**Rate limiting.** Per-IP / per-email rate limits and the
disposable-domain blocklist live in cd-055 (``p2.signup.abuse``). This
module stubs the throttle call at the top of :func:`start_signup`
with a clear TODO; the real implementation will drop in when
:mod:`app.abuse.throttle` lands.

**PII.** Plaintext email is written to ``signup_attempt.email_lower``
— needed at complete time to seed the ``user`` row. ``email_hash`` /
``ip_hash`` (SHA-256 + HKDF pepper, matching the magic-link shape)
drive abuse correlation. Audit rows carry hashes only.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup" and
``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.bootstrap import (
    seed_owners_system_group,
    seed_system_permission_groups,
)
from app.adapters.db.identity.models import (
    MagicLinkNonce,
    SignupAttempt,
    User,
    canonicalise_email,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.adapters.mail.ports import Mailer
from app.audit import write_audit
from app.auth import magic_link, passkey
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import Throttle
from app.auth.keys import derive_subkey
from app.capabilities import Capabilities
from app.config import Settings, get_settings
from app.domain.plans import seed_free_tier_10pct
from app.tenancy import (
    InvalidSlug,
    WorkspaceContext,
    is_homoglyph_collision,
    is_reserved,
    tenant_agnostic,
    validate_slug,
)
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "CompletedSignup",
    "HomoglyphCollision",
    "SignupAttemptExpired",
    "SignupAttemptMissing",
    "SignupCompletionState",
    "SignupDisabled",
    "SignupSession",
    "SlugHomoglyphError",
    "SlugInGracePeriod",
    "SlugReserved",
    "SlugTaken",
    "complete_signup",
    "consume_verify",
    "prune_stale_signups",
    "start_signup",
]


# Spec §03: signup_attempt carries a 15-minute TTL; the magic link it
# emits picks the same cap so both expire together.
_SIGNUP_TTL: Final[timedelta] = timedelta(minutes=15)

# Cap for :func:`_suggest_alternative_slug`'s digit probe. We try
# ``<slug>-2`` .. ``<slug>-<N+1>`` and stop at the first free one. The
# ``20``-attempt ceiling is a deliberate quadratic-scan bound: each
# probe validates + checks the reserved list + checks homoglyph
# collisions against every existing slug, and in the pathological case
# every candidate is taken. 20 attempts bounds the work at ~20 * O(|ws|)
# per 409 response, which stays well inside a single request budget.
_MAX_SUGGESTION_ATTEMPTS: Final[int] = 20

# Spec §03: the ``signup_gc`` worker prunes orphaned workspaces after
# 1 hour. An orphan is a workspace with no ``user_workspace`` rows
# AND no completed signup attempt and a ``created_at`` older than the
# cutoff. Anything fresher might still be mid-ceremony.
_ORPHAN_CUTOFF: Final[timedelta] = timedelta(hours=1)

# HKDF purpose for the email / IP hash pepper. Reuses the magic-link
# subkey deliberately — a signup attempt and its sibling magic-link
# nonce row hash the same email with the same subkey, so abuse
# correlation joins without a re-derivation.
_HKDF_PURPOSE: Final[str] = "magic-link"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SignupDisabled(RuntimeError):
    """``settings.signup_enabled`` is false — every signup route 404s."""


class SlugReserved(ValueError):
    """Desired slug is in the reserved routing blocklist. 409."""


class SlugTaken(ValueError):
    """An existing active workspace already owns the exact slug. 409.

    Carries a ``suggested_alternative`` — a free, valid, non-reserved,
    non-colliding slug the signup UI can offer the visitor in one click
    (§03 "Self-serve signup" step 1: "`409 slug_taken` returns a
    suggested alternative"). The suggestion is found by
    :func:`_suggest_alternative_slug`; if every candidate in the bounded
    probe range is also taken (pathological workspace soup), the
    suggestion falls back to the original slug so the UI can still
    render a message without the router blowing up.
    """

    def __init__(self, slug: str, suggested_alternative: str) -> None:
        super().__init__(f"slug {slug!r} already taken")
        self.slug = slug
        self.suggested_alternative = suggested_alternative


class SlugHomoglyphError(ValueError):
    """Desired slug collides typographically with an existing slug. 409."""

    def __init__(self, candidate: str, colliding_slug: str) -> None:
        super().__init__(
            f"slug {candidate!r} is a homoglyph for existing slug {colliding_slug!r}"
        )
        self.candidate = candidate
        self.colliding_slug = colliding_slug


class SlugInGracePeriod(ValueError):
    """Slug was recently released by another workspace; still on hold. 409.

    **Stubbed.** §03 spec describes a 30-day ``slug_reservation`` hold
    for archived / hard-deleted workspaces. The ``slug_reservation``
    table is not part of the v1 slice (cd-3i5 only lists ``workspace``,
    ``user``, ``user_workspace``, ``permission_group``, ``role_grant``
    in its Phase 1 tables), so :func:`start_signup` carries a
    placeholder predicate that always returns ``False``. When the
    reservation table lands, the predicate plugs in without changing
    the exception surface.
    """


class HomoglyphCollision(SlugHomoglyphError):
    """Back-compat alias for :class:`SlugHomoglyphError`.

    The task spec uses both names; keeping them both lets calling code
    (and tests) discover either without a compat import. The class is
    a subclass so an ``isinstance`` check against either name works.
    """


class SignupAttemptMissing(LookupError):
    """No ``signup_attempt`` row matches the given id. 404/409."""


class SignupAttemptExpired(ValueError):
    """``signup_attempt.expires_at`` is in the past, or the state is wrong.

    Covers three shapes:

    * ``expires_at <= now`` — TTL lapsed. 410.
    * ``verified_at is None`` at :func:`complete_signup` time. 409.
    * ``completed_at is not None`` at :func:`complete_signup` time. 409.

    Callers distinguish via :class:`SignupCompletionState` on the
    ``state`` attribute so the HTTP layer can map each to its own
    symbol without reaching into the exception message.
    """

    def __init__(self, message: str, *, state: SignupCompletionState) -> None:
        super().__init__(message)
        self.state = state


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignupSession:
    """Payload returned by :func:`consume_verify`.

    The fields are sufficient for the caller to kick off the passkey
    ceremony without a round-trip: ``signup_attempt_id`` doubles as
    the challenge's ``signup_session_id``, ``email_lower`` seeds the
    WebAuthn user entity's ``name``, and ``desired_slug`` tells the
    client which workspace is about to be born.
    """

    signup_attempt_id: str
    email_lower: str
    desired_slug: str


@dataclass(frozen=True, slots=True)
class CompletedSignup:
    """Payload returned by :func:`complete_signup`.

    Drives the final redirect — the SPA takes ``slug`` and sends the
    browser to ``/w/<slug>/today``.
    """

    user_id: str
    workspace_id: str
    slug: str


# Finer-grained "why can't we complete" discriminator so the HTTP
# router can pick the right status code without parsing the
# exception message.
SignupCompletionState = str  # one of: "expired" | "not_verified" | "already_completed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    return (clock if clock is not None else SystemClock()).now()


def _pepper(settings: Settings | None) -> bytes:
    s = settings if settings is not None else get_settings()
    return derive_subkey(s.root_key, purpose=_HKDF_PURPOSE)


# Local re-export of the shared helper — see :mod:`app.auth._hashing`
# (cd-3dc7). Keeping the private alias means intra-module call sites
# (``email_hash = _hash_with_pepper(email_lower, pepper)`` etc.) stay
# a one-word swap without reshuffling the entire file.
_hash_with_pepper = hash_with_pepper


def _agnostic_audit_ctx() -> WorkspaceContext:
    """Sentinel ``WorkspaceContext`` for pre-workspace audit rows.

    Mirrors :func:`app.auth.magic_link._agnostic_audit_ctx`: the start
    and verify steps have no real workspace to borrow, so we synthesise
    a zero-ULID context with ``actor_kind='system'``. Once
    :func:`complete_signup` has the freshly-minted workspace, audit
    emissions switch to a real ctx.
    """
    return WorkspaceContext(
        workspace_id="00000000000000000000000000",
        workspace_slug="",
        actor_id="00000000000000000000000000",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _existing_active_slugs(session: Session) -> list[str]:
    """Return the ``workspace.slug`` values that are currently in use.

    The slug column is globally unique. A future archive step (§02
    ``verification_state='archived'``) will exclude archived rows from
    this set so their slugs become reclaimable under the grace
    predicate. Until ``verification_state`` lands, every row is
    considered active.
    """
    # justification: signup runs before a WorkspaceContext exists; the
    # workspace table is workspace-scoped and the ORM tenant filter
    # would otherwise reject this SELECT for want of a workspace_id
    # predicate.
    with tenant_agnostic():
        rows = session.scalars(select(Workspace.slug)).all()
    return list(rows)


def _is_slug_in_grace_period(session: Session, *, slug: str, now: datetime) -> bool:
    """Return ``True`` when the slug is inside a 30-day reservation hold.

    **Stubbed.** The spec's ``slug_reservation`` table (§02) is not
    part of cd-3i5's Phase 1 tables — the predicate is intentionally
    false until that table lands. When it does, replace the body with
    a ``SELECT reserved_until FROM slug_reservation WHERE slug = :slug
    AND reserved_until > :now`` query.

    Flagged for the Documenter: once ``slug_reservation`` lands, this
    predicate becomes a real DB read and :class:`SlugInGracePeriod`
    starts firing.
    """
    del session, slug, now
    return False


def _suggest_alternative_slug(
    desired_slug: str,
    *,
    existing_slugs: list[str],
) -> str:
    """Return a free ``<base>-<n>`` slug or ``desired_slug`` on failure.

    Strategy — spec §03 step 1 "`409 slug_taken` returns a suggested
    alternative":

    * Pick a ``base`` that keeps ``<base>-<suffix>`` under the 40-char
      ceiling. When the caller's slug is long enough that even
      ``<desired>-2`` blows the cap (38+ chars of base + ``-`` + ≥1
      digit), truncate the trailing characters — including any stray
      hyphens — so the suggestion stays inside :func:`validate_slug`'s
      bounds. Truncation is a last resort; shorter bases win first.
    * Probe ``<base>-2``, ``<base>-3`` .. up to
      ``<base>-<_MAX_SUGGESTION_ATTEMPTS + 1>``.
    * Accept the first candidate that (a) passes :func:`validate_slug`
      (so the regex bound + ≤40-char ceiling are respected), (b) is
      not reserved, (c) is not in ``existing_slugs``, and (d) is not a
      homoglyph collision against ``existing_slugs``.
    * If every probe fails (pathological: 20 matching sibling
      workspaces, truncation ate the base dry, etc.), return the
      original ``desired_slug``. The caller uses this as the suggestion
      body anyway; the UI surfaces it with a prompt to type a different
      one.

    Pure function — no DB access. The caller pre-fetches
    ``existing_slugs`` once and threads it in so the suggestion scan
    doesn't double the query count on a 409.
    """
    existing_set = set(existing_slugs)
    # The longest suffix we ever probe is ``-<_MAX_SUGGESTION_ATTEMPTS + 1>``;
    # two-digit suffixes are possible when _MAX_SUGGESTION_ATTEMPTS >= 9.
    # Pre-compute a shrink budget that keeps the worst-case candidate
    # under the 40-char ceiling so the long-slug case doesn't fall back
    # to the no-op "return the slug the caller already knows is taken".
    max_suffix_digits = len(str(1 + _MAX_SUGGESTION_ATTEMPTS))
    reserved_tail = 1 + max_suffix_digits  # "-" + digits
    max_base_len = 40 - reserved_tail
    base = desired_slug
    if len(base) > max_base_len:
        base = base[:max_base_len].rstrip("-")
    # After trimming, the base must still satisfy :func:`validate_slug`
    # (≥3 chars, ends [a-z0-9], no ``--`` tail). If the truncation ate
    # everything, abort the probe and fall back to the caller-visible
    # slug.
    try:
        validate_slug(base)
    except InvalidSlug:
        return desired_slug

    for suffix in range(2, 2 + _MAX_SUGGESTION_ATTEMPTS):
        candidate = f"{base}-{suffix}"
        try:
            validate_slug(candidate)
        except InvalidSlug:
            # Shouldn't happen once the base is bounded, but staying
            # defensive means a future spec bump to reserved_tail
            # doesn't silently resurrect the "suggest the taken slug"
            # bug.
            break
        if is_reserved(candidate):
            continue
        if candidate in existing_set:
            continue
        if is_homoglyph_collision(candidate, existing_set) is not None:
            continue
        return candidate
    return desired_slug


def _throttle_signup_start(*, ip_hash: str, email_hash: str, now: datetime) -> None:
    """Rate-limit gate for :func:`start_signup`.

    Stubbed for cd-3i5. cd-055 (``p2.signup.abuse``) owns the real
    rate-limit table: per-IP 5 signups/hour, per-email 3 lifetime on
    the SaaS deployment (§03 "Throttle on repeat provisioning"). Until
    :mod:`app.abuse.throttle` lands, this call is a no-op — the tests
    for this module assert the absence of rate-limit semantics; cd-055
    will add the real tests alongside the real enforcement.
    """
    # TODO(cd-055): call ``throttle.check_request(scope="signup_start",
    # ip_hash=..., email_hash=..., now=...)`` once the shared abuse
    # throttle lands. Keeping the argument shape stable here means the
    # swap is a one-line diff inside :func:`start_signup`.
    del ip_hash, email_hash, now


def _ensure_signup_enabled(capabilities: Capabilities | None) -> None:
    """Raise :class:`SignupDisabled` when the deployment flag is off.

    The caller passes the :class:`Capabilities` envelope explicitly so
    tests can swap in a fresh instance per case. When ``capabilities``
    is ``None`` the caller's context is "I already checked" — the
    domain service trusts the HTTP layer to gate every route and
    skips the second check.
    """
    if capabilities is None:
        return
    if not capabilities.settings.signup_enabled:
        raise SignupDisabled("self-serve signup is disabled on this deployment")


def _find_pending_signup_attempt(
    session: Session,
    *,
    email_lower: str,
    desired_slug: str,
    now: datetime,
) -> SignupAttempt | None:
    """Return the still-actionable ``signup_attempt`` for the pair, if any.

    "Still actionable" means ``completed_at IS NULL`` and
    ``expires_at > now``. A row that satisfies this is the one the
    duplicate-IntegrityError guard needs to update-in-place — the
    ``UNIQUE (email_lower, desired_slug)`` constraint on the table
    would otherwise make the second :func:`start_signup` a 500. We
    ignore expired rows here deliberately: the domain layer treats
    them as garbage the GC will sweep, and an expired row does not
    cross the unique-index business bar (the spec calls the retry
    path "a fresh magic link for the same attempt", not "resurrect
    the expired one").
    """
    resolved_now = _aware_utc(now)
    # justification: signup_attempt is identity-scoped.
    with tenant_agnostic():
        candidates = session.scalars(
            select(SignupAttempt).where(
                SignupAttempt.email_lower == email_lower,
                SignupAttempt.desired_slug == desired_slug,
                SignupAttempt.completed_at.is_(None),
            )
        ).all()
    # Filter TTL in Python so the SQLite/Postgres tzinfo round-trip (see
    # :func:`_aware_utc`) doesn't bite the WHERE clause.
    for candidate in candidates:
        if _aware_utc(candidate.expires_at) > resolved_now:
            return candidate
    return None


def _invalidate_pending_nonces(session: Session, *, subject_id: str) -> None:
    """Delete every unconsumed signup-verify :class:`MagicLinkNonce` for ``subject_id``.

    Used by the :func:`start_signup` retry path: when a pending signup
    attempt is re-opened, its prior magic link must stop being
    redeemable so only the freshly-minted one works. Deleting the
    pending nonce rows (rather than flipping ``consumed_at``) is the
    narrow-scope path — the rejected-audit trail for the old token
    will land as ``already_consumed`` naturally when a lucky client
    tries it after the refresh. We deliberately leave already-consumed
    rows untouched: those are forensic records and shouldn't be
    tombstoned by the retry.

    **Purpose guard.** The ``subject_id`` column is soft-typed — a
    ``signup_verify`` nonce carries the signup_attempt's ULID, but a
    ``recover_passkey`` / ``email_change_confirm`` / ``grant_invite``
    nonce carries a ``user.id`` / ``invite.id`` that is *also* a ULID
    drawn from the same 128-bit space. Collisions are astronomically
    unlikely, but the retry has no business touching any nonce that
    isn't a signup-verify. Narrowing the predicate to
    ``purpose='signup_verify'`` is a defence-in-depth guard: even a
    freakishly unlucky ULID collision can only sweep a row the signup
    flow legitimately owns.
    """
    # justification: magic_link_nonce is identity-scoped.
    with tenant_agnostic():
        session.execute(
            delete(MagicLinkNonce)
            .where(
                MagicLinkNonce.subject_id == subject_id,
                MagicLinkNonce.purpose == "signup_verify",
                MagicLinkNonce.consumed_at.is_(None),
            )
            .execution_options(synchronize_session=False)
        )
        session.flush()


def _load_signup_attempt(session: Session, *, signup_attempt_id: str) -> SignupAttempt:
    """Load the signup-attempt row or raise :class:`SignupAttemptMissing`."""
    # justification: signup_attempt is identity-scoped (tenant-agnostic
    # by design; see the model docstring). The ORM tenant filter has
    # nothing to apply.
    with tenant_agnostic():
        row = session.get(SignupAttempt, signup_attempt_id)
    if row is None:
        raise SignupAttemptMissing(signup_attempt_id)
    return row


def _aware_utc(value: datetime) -> datetime:
    """Normalise naive ``datetime`` values to aware UTC.

    SQLite's ``DateTime(timezone=True)`` drops tzinfo on round-trip
    (the column stores ISO text without offset). Postgres preserves
    it. Every TTL comparison in this module normalises both sides to
    aware UTC so backend selection doesn't leak into the domain logic.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ---------------------------------------------------------------------------
# start_signup
# ---------------------------------------------------------------------------


def start_signup(
    session: Session,
    *,
    email: str,
    desired_slug: str,
    ip: str,
    mailer: Mailer,
    base_url: str,
    throttle: Throttle,
    capabilities: Capabilities | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> None:
    """Kick off a self-serve signup attempt.

    Spec §03 "Self-serve signup" step 1. Steps, in order:

    1. Gate on ``capabilities.settings.signup_enabled``; disabled →
       :class:`SignupDisabled`.
    2. Validate ``desired_slug`` against the §02 regex — raises
       :class:`~app.tenancy.InvalidSlug`.
    3. Reserved check — raises :class:`SlugReserved`.
    4. Look up active ``workspace.slug`` values. Exact match →
       :class:`SlugTaken`. Homoglyph match →
       :class:`SlugHomoglyphError` with the colliding slug attached.
    5. Grace-period check (stubbed; see
       :func:`_is_slug_in_grace_period`). Hit →
       :class:`SlugInGracePeriod`.
    6. Stub the future rate-limit call (see
       :func:`_throttle_signup_start`).
    7. Insert (or refresh) the :class:`SignupAttempt` row — 15-minute
       TTL. If an uncompleted, unexpired row already exists for
       ``(email_lower, desired_slug)``, reuse it: refresh
       ``expires_at`` + ``ip_hash`` and invalidate any pending
       magic-link nonces so only the freshly-minted token redeems.
       The ``UNIQUE (email_lower, desired_slug)`` constraint on the
       table would otherwise surface as a 500 on a retry; this path
       keeps the caller-visible shape 202 idempotent.
    8. Mint a magic link with ``purpose='signup_verify'``, pinning the
       ``signup_attempt_id`` as the token's ``subject_id`` so the
       verify step can look up the attempt without trusting the
       client's body. The magic link's own throttle (:class:`Throttle`)
       fires independently.
    9. Audit ``signup.requested`` with ``email_hash`` / ``ip_hash`` /
       ``desired_slug`` only — PII never lands in the diff.

    Returns ``None``. The caller's UoW owns the transaction; the
    router commits on 202.
    """
    _ensure_signup_enabled(capabilities)

    resolved_now = now if now is not None else _now(clock)
    email_lower = canonicalise_email(email)

    # Reserved list fires BEFORE pattern validation so the 409
    # ``slug_reserved`` response is distinct from the 422
    # ``invalid_slug`` one. :func:`validate_slug` itself also rejects
    # reserved slugs, but via :class:`InvalidSlug` — we want a
    # typed domain error here so the HTTP router maps it to the
    # right symbol + status.
    if is_reserved(desired_slug):
        raise SlugReserved(f"slug {desired_slug!r} is reserved")
    try:
        validate_slug(desired_slug)
    except InvalidSlug:
        # Re-raise unchanged — the router maps :class:`InvalidSlug` to
        # 422 ``invalid_slug`` directly, no repackaging needed.
        raise

    existing_slugs = _existing_active_slugs(session)
    if desired_slug in existing_slugs:
        raise SlugTaken(
            desired_slug,
            suggested_alternative=_suggest_alternative_slug(
                desired_slug, existing_slugs=existing_slugs
            ),
        )

    collision = is_homoglyph_collision(desired_slug, existing_slugs)
    if collision is not None:
        raise SlugHomoglyphError(candidate=desired_slug, colliding_slug=collision)

    if _is_slug_in_grace_period(session, slug=desired_slug, now=resolved_now):
        raise SlugInGracePeriod(f"slug {desired_slug!r} is in its 30-day grace period")

    pepper = _pepper(settings)
    email_hash = _hash_with_pepper(email_lower, pepper)
    ip_hash = _hash_with_pepper(ip, pepper)

    _throttle_signup_start(ip_hash=ip_hash, email_hash=email_hash, now=resolved_now)

    # Idempotent retry: a second call with the same ``(email_lower,
    # desired_slug)`` inside the 15-minute TTL updates the existing
    # signup_attempt in place (refreshing ``expires_at`` + ``ip_hash``)
    # and mints a fresh magic link against the same ``subject_id``.
    # Without this guard the ``UNIQUE (email_lower, desired_slug)``
    # constraint on the table would surface as a 500 on the second
    # request; with it, the caller gets 202 idempotently.
    existing_attempt = _find_pending_signup_attempt(
        session,
        email_lower=email_lower,
        desired_slug=desired_slug,
        now=resolved_now,
    )
    if existing_attempt is not None:
        signup_attempt_id = existing_attempt.id
        existing_attempt.expires_at = resolved_now + _SIGNUP_TTL
        # Latest IP wins — the retry typically carries the same IP,
        # but a legitimate user hopping networks mid-retry shouldn't
        # be penalised for it.
        existing_attempt.ip_hash = ip_hash
        # justification: signup_attempt is identity-scoped.
        with tenant_agnostic():
            session.flush()
        # Drop any still-pending magic-link nonces for this attempt
        # BEFORE minting a new one, so the old token can't redeem.
        _invalidate_pending_nonces(session, subject_id=signup_attempt_id)
    else:
        signup_attempt_id = new_ulid(clock=clock)
        expires_at = resolved_now + _SIGNUP_TTL

        # justification: signup_attempt is identity-scoped; the ORM
        # tenant filter has nothing to apply.
        with tenant_agnostic():
            session.add(
                SignupAttempt(
                    id=signup_attempt_id,
                    email_lower=email_lower,
                    email_hash=email_hash,
                    desired_slug=desired_slug,
                    ip_hash=ip_hash,
                    created_at=resolved_now,
                    expires_at=expires_at,
                    verified_at=None,
                    completed_at=None,
                    workspace_id=None,
                )
            )
            session.flush()

    # Mint the magic link. The signup_attempt row we just inserted (or
    # refreshed) is the token's ``subject_id`` — passing it explicitly
    # means :func:`consume_verify` can resolve the attempt straight
    # from the redeemed token without trusting the client's body. The
    # magic-link service's own TTL cap already pins ``signup_verify``
    # at 15 minutes, matching :data:`_SIGNUP_TTL`; passing the ttl
    # explicitly keeps the two in sync if the cap shifts.
    magic_link.request_link(
        session,
        email=email_lower,
        purpose="signup_verify",
        ip=ip,
        mailer=mailer,
        base_url=base_url,
        now=resolved_now,
        ttl=_SIGNUP_TTL,
        throttle=throttle,
        settings=settings,
        clock=clock,
        subject_id=signup_attempt_id,
    )

    # Audit row lands on the caller's UoW so it commits iff the rest
    # of the insert commits. Pre-tenant context — see
    # :func:`_agnostic_audit_ctx`.
    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="signup_attempt",
        entity_id=signup_attempt_id,
        action="signup.requested",
        diff={
            "email_hash": email_hash,
            "ip_hash": ip_hash,
            "desired_slug": desired_slug,
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# consume_verify
# ---------------------------------------------------------------------------


def consume_verify(
    session: Session,
    *,
    token: str,
    ip: str,
    throttle: Throttle,
    capabilities: Capabilities | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> SignupSession:
    """Consume the signup-verify magic link; mark the attempt verified.

    Delegates the token unseal + single-use flip to
    :func:`app.auth.magic_link.consume_link`. The magic-link service
    raises its own typed errors (:class:`~app.auth.magic_link.InvalidToken`
    / :class:`~app.auth.magic_link.TokenExpired` /
    :class:`~app.auth.magic_link.AlreadyConsumed` /
    :class:`~app.auth.magic_link.PurposeMismatch`); the HTTP router
    maps those to their existing symbols — we don't re-wrap.

    Returns a :class:`SignupSession` the caller uses to kick off the
    passkey ceremony.
    """
    _ensure_signup_enabled(capabilities)
    resolved_now = now if now is not None else _now(clock)

    outcome = magic_link.consume_link(
        session,
        token=token,
        expected_purpose="signup_verify",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )

    # Load the signup_attempt row — the token's subject_id points at
    # its id. A missing row means the caller bypassed
    # :func:`start_signup` with a hand-forged token whose subject
    # didn't match; treat it as a 404.
    attempt = _load_signup_attempt(session, signup_attempt_id=outcome.subject_id)

    if _aware_utc(attempt.expires_at) <= resolved_now:
        raise SignupAttemptExpired(
            f"signup_attempt {attempt.id!r} expired",
            state="expired",
        )
    if attempt.completed_at is not None:
        raise SignupAttemptExpired(
            f"signup_attempt {attempt.id!r} already completed",
            state="already_completed",
        )

    # Flip verified_at.
    attempt.verified_at = resolved_now
    # justification: signup_attempt is identity-scoped.
    with tenant_agnostic():
        session.flush()

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="signup_attempt",
        entity_id=attempt.id,
        action="signup.verified",
        diff={
            "email_hash": attempt.email_hash,
            "ip_hash_at_verify": _hash_with_pepper(ip, _pepper(settings)),
            "desired_slug": attempt.desired_slug,
        },
        clock=clock,
    )

    return SignupSession(
        signup_attempt_id=attempt.id,
        email_lower=attempt.email_lower,
        desired_slug=attempt.desired_slug,
    )


# ---------------------------------------------------------------------------
# complete_signup
# ---------------------------------------------------------------------------


def complete_signup(
    session: Session,
    *,
    signup_attempt_id: str,
    display_name: str,
    timezone: str,
    challenge_id: str,
    passkey_payload: dict[str, Any],
    ip: str,
    capabilities: Capabilities | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> CompletedSignup:
    """Finalise the signup — one transaction, 10-ish row writes.

    Inside the caller's UoW (§01) we insert, in order:

    1. :class:`Workspace` — plan=``free``, quota=``seed_free_tier_10pct()``.
    2. :class:`User` — email case-folded, display name + timezone from
       the browser.
    3. :class:`UserWorkspace` — source=``workspace_grant``.
    4. Four system permission groups (``owners`` via
       :func:`seed_owners_system_group`; the other three via
       :func:`seed_system_permission_groups`).
    5. The ``owners`` group member row + the ``manager`` role grant —
       seeded by :func:`seed_owners_system_group`.
    6. :func:`app.auth.passkey.register_finish_signup` — verifies the
       attestation, inserts the credential row, deletes the challenge.
    7. ``signup_attempt.completed_at`` / ``workspace_id`` — flip to
       signal the row is settled (so ``signup_gc`` skips it).
    8. ``audit.signup.completed`` under the freshly-minted
       :class:`WorkspaceContext`.

    Any exception anywhere (passkey verification fails, FK violation,
    integrity error) propagates; the caller's UoW rolls back and none
    of the rows above land. See the cd-3i5 AC #1 test for the
    atomicity guarantee.
    """
    _ensure_signup_enabled(capabilities)
    resolved_now = now if now is not None else _now(clock)

    attempt = _load_signup_attempt(session, signup_attempt_id=signup_attempt_id)
    if _aware_utc(attempt.expires_at) <= resolved_now:
        raise SignupAttemptExpired(
            f"signup_attempt {attempt.id!r} expired",
            state="expired",
        )
    if attempt.verified_at is None:
        raise SignupAttemptExpired(
            f"signup_attempt {attempt.id!r} not verified",
            state="not_verified",
        )
    if attempt.completed_at is not None:
        raise SignupAttemptExpired(
            f"signup_attempt {attempt.id!r} already completed",
            state="already_completed",
        )

    workspace_id = new_ulid(clock=clock)
    user_id = new_ulid(clock=clock)

    # justification: Workspace is workspace-scoped but we are CREATING
    # the tenancy anchor — no WorkspaceContext exists yet to filter
    # against. User / UserWorkspace likewise sit before the context
    # is resolved.
    with tenant_agnostic():
        workspace = Workspace(
            id=workspace_id,
            slug=attempt.desired_slug,
            name=attempt.desired_slug,  # display name defaults to slug;
            # operators rename via ``crewday admin workspace set-name``
            # in §04. Keeping name=slug here means the UI's workspace
            # picker has something readable on day 1 without a schema
            # bump to make ``name`` nullable.
            plan="free",
            quota_json=seed_free_tier_10pct(),
            created_at=resolved_now,
        )
        session.add(workspace)
        session.flush()

        user = User(
            id=user_id,
            email=attempt.email_lower,
            email_lower=attempt.email_lower,
            display_name=display_name,
            timezone=timezone,
            created_at=resolved_now,
        )
        session.add(user)
        session.flush()

        session.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=resolved_now,
            )
        )
        session.flush()

        # Build the real workspace context eagerly so the owners-
        # bootstrap audit row (cd-ckr) is attributed to the freshly-
        # minted tenancy from the very first audit emission. Reused
        # below for the ``signup.completed`` row — a single
        # correlation id links the two together.
        real_ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=attempt.desired_slug,
            actor_id=user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        # Seed the four system groups. The owners seed also emits the
        # member + role-grant rows + the ``owners_bootstrapped``
        # audit row — the other three are empty scaffolding today
        # (capability payloads land with cd-zkr).
        seed_owners_system_group(
            session,
            real_ctx,
            workspace_id=workspace_id,
            owner_user_id=user_id,
            clock=clock,
        )
        seed_system_permission_groups(
            session,
            workspace_id=workspace_id,
            clock=clock,
        )

    # Passkey finish — verifies the attestation, inserts the credential
    # row, deletes the one-shot challenge. Raising propagates out of
    # this block and the whole transaction rolls back (AC #1
    # atomicity test).
    passkey.register_finish_signup(
        session,
        signup_session_id=signup_attempt_id,
        user_id=user_id,
        challenge_id=challenge_id,
        credential=passkey_payload,
        clock=clock,
    )

    # Flip the signup_attempt row now that every downstream insert
    # succeeded. Under rollback this flip is discarded too, which is
    # the correct behaviour — a rolled-back complete leaves the
    # attempt re-usable until its own TTL elapses.
    attempt.completed_at = resolved_now
    attempt.workspace_id = workspace_id
    with tenant_agnostic():
        session.flush()

    # ``real_ctx`` was built earlier (before the seed step) so the
    # owners-bootstrapped audit row is attributed to the new
    # workspace. Reuse it here so the ``signup.completed`` row
    # carries the same ``audit_correlation_id`` — the forensic trail
    # joins on correlation id across the two rows.
    write_audit(
        session,
        real_ctx,
        entity_kind="workspace",
        entity_id=workspace_id,
        action="signup.completed",
        diff={
            "slug": attempt.desired_slug,
            "email_hash": attempt.email_hash,
            "ip_hash_at_completion": _hash_with_pepper(ip, _pepper(settings)),
        },
        clock=clock,
    )

    return CompletedSignup(
        user_id=user_id,
        workspace_id=workspace_id,
        slug=attempt.desired_slug,
    )


# ---------------------------------------------------------------------------
# signup_gc — orphan prune
# ---------------------------------------------------------------------------


def prune_stale_signups(
    session: Session,
    *,
    now: datetime,
) -> list[str]:
    """Delete orphaned workspaces older than 1 hour with no members.

    Selection criteria (§03 "Passkey enrollment + break-glass codes"):
    a workspace is an orphan if

    * it has **zero** ``user_workspace`` rows, AND
    * ``created_at < now - 1h``, AND
    * every signup attempt pointing at it has ``completed_at IS NULL``
      — a completed attempt implies the sibling user insert lined up,
      and its membership row should exist. If it doesn't we have a
      worse bug than GC can paper over.

    Returns the list of deleted workspace ids so the caller (or test)
    can assert the set. Wrapping in :func:`tenant_agnostic` is
    mandatory: the GC job runs without any :class:`WorkspaceContext`,
    so the ORM filter would otherwise reject the SELECT for want of a
    predicate.

    **Scheduler wiring.** Today this is a plain callable. APScheduler
    isn't wired into :mod:`app.worker` yet; the scheduler follow-up
    will register this at hourly cadence. The function is pure (aside
    from the DB writes) and idempotent — running it twice in a row
    deletes nothing the first run didn't already.
    """
    cutoff = now - _ORPHAN_CUTOFF
    deleted_ids: list[str] = []

    # justification: GC runs tenant-agnostic — no live workspace
    # context exists during background jobs.
    with tenant_agnostic():
        workspaces = session.scalars(
            select(Workspace).where(Workspace.created_at < cutoff)
        ).all()
        for workspace in workspaces:
            member_count = session.scalar(
                select(UserWorkspace)
                .where(UserWorkspace.workspace_id == workspace.id)
                .limit(1)
            )
            if member_count is not None:
                continue
            # A completed signup attempt should never leave the
            # workspace membership-less; if one does, we refuse to
            # sweep it so the bug surfaces rather than being swept
            # under the rug.
            completed_attempt = session.scalar(
                select(SignupAttempt)
                .where(SignupAttempt.workspace_id == workspace.id)
                .where(SignupAttempt.completed_at.is_not(None))
                .limit(1)
            )
            if completed_attempt is not None:
                continue
            session.delete(workspace)
            deleted_ids.append(workspace.id)
        session.flush()

    return deleted_ids
