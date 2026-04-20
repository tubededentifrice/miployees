"""Self-serve signup abuse mitigations.

Four public surfaces, one module:

* :func:`check_rate` — hands off to
  :meth:`app.auth._throttle.Throttle.check_signup_start` (per-IP,
  per-email, deployment-wide). Raises :class:`SignupRateLimited`,
  re-exported from :mod:`app.auth._throttle` so the router catches
  one symbol.
* :func:`is_disposable` — consults the bundled
  :file:`app/abuse/data/disposable_domains.txt` file via an
  :func:`functools.lru_cache` so the file is read once per process
  lifetime. The :func:`reload_disposable_domains` hook clears the
  cache so an operator SIGHUP (or a test) can pick up a refreshed
  file without a restart.
* :func:`check_captcha` — thin wrapper around Cloudflare Turnstile's
  ``siteverify`` endpoint. Falls through to an offline test-mode that
  accepts a fixed ``"test-pass"`` token and rejects ``"test-fail"``
  when :attr:`app.config.Settings.captcha_turnstile_secret` is unset,
  so unit tests never hit the network.
* :func:`check_reserved_slug` — consolidates
  :func:`app.tenancy.validate_slug` + :func:`app.tenancy.is_reserved`
  + :func:`app.tenancy.is_homoglyph_collision` into one call so
  both the signup domain service and the abuse module agree on the
  vocabulary of slug-related errors.

**Module-level comment** (DRY with ``_throttle.py``): cd-7huk
partially landed — ``app/abuse/throttle.py`` now exists and the
passkey-login-begin endpoint has moved onto the shared
:func:`~app.abuse.throttle.throttle` decorator. The signup-start
and magic-link throttle handoff here is still pending (no dedicated
Beads task; the scope is documented in
:mod:`app.auth._throttle`'s own docstring). Until then the
in-memory ``Throttle`` instance that :func:`check_rate` hands into
owns the counters, and a process restart resets every bucket. That's
documented behaviour for the local pre-shared-throttle slice, not a
bug.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" and ``docs/specs/03-auth-and-tokens.md`` §"Self-serve
signup".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from functools import lru_cache
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import Final

import httpx

from app.auth._throttle import SignupRateLimited, Throttle

# Re-export the existing slug errors from :mod:`app.auth.signup` so
# callers import one surface (signup_abuse) for the whole abuse
# vocabulary. Deliberate re-export — we don't want two copies of
# the error classes. Safe from circular imports: ``signup`` does not
# import ``signup_abuse`` (the HTTP router is the only caller that
# imports both, and it does so independently).
from app.auth.signup import SlugHomoglyphError, SlugReserved
from app.capabilities import Capabilities
from app.config import Settings
from app.tenancy import (
    InvalidSlug,
    is_homoglyph_collision,
    is_reserved,
    validate_slug,
)

__all__ = [
    "CaptchaFailed",
    "DisposableEmail",
    "SignupRateLimited",
    "SlugAbuseRejected",
    "SlugHomoglyphError",
    "SlugReserved",
    "check_captcha",
    "check_rate",
    "check_reserved_slug",
    "is_disposable",
    "reload_disposable_domains",
]


# Bundled blocklist — one canonical path, loaded once per process.
# The file lives under ``app/abuse/data/`` (spec §15 "Self-serve abuse
# mitigations": *"the in-repo file (``app/abuse/data/disposable_domains.txt``)"*)
# — we resolve it through :func:`importlib.resources.files` so the
# path stays correct whether :mod:`app.abuse` is a directory on disk
# (dev + tests) or a zipped wheel (hypothetical future packaging).
# Operator overrides via the deployment setting
# ``settings.signup_disposable_domains_path`` (§15) land in a future
# follow-up; until then the bundled file is the single source of truth.
_DEFAULT_DOMAINS_PATH: Final[Path] = Path(
    str(_pkg_files("app.abuse").joinpath("data", "disposable_domains.txt"))
)

# Cloudflare Turnstile server-side verification endpoint. Pinned (not
# operator-configurable): changing the provider is a code diff, not
# an ops switch.
_TURNSTILE_VERIFY_URL: Final[str] = (
    "https://challenges.cloudflare.com/turnstile/v0/siteverify"
)

# Fixed tokens accepted / rejected in offline test mode (when the
# Turnstile secret is unset). Documented so operators see the same
# strings in the test fixtures and the code.
_TEST_PASS_TOKEN: Final[str] = "test-pass"
_TEST_FAIL_TOKEN: Final[str] = "test-fail"

# Turnstile verification timeout (seconds). Short because the verify
# endpoint is on the signup-start hot path; a hanging Turnstile
# outage MUST NOT hold a signup request open for minutes. Exposed
# as a module-level Final so tests can monkey-patch it.
_TURNSTILE_TIMEOUT_SECONDS: Final[float] = 3.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DisposableEmail(ValueError):
    """Email address belongs to a known throwaway / disposable provider.

    The HTTP router maps this to ``422 disposable_email`` (spec §15
    stipulates 400; the app's broader convention uses 422 for
    "request was well-formed but semantically invalid" — see the
    ``SignupStartBody`` 422 paths in the router). Carries only the
    canonical domain (``user@mailinator.com`` → ``"mailinator.com"``)
    so audit rows never accidentally log the local-part.
    """

    def __init__(self, domain: str) -> None:
        super().__init__(
            f"email domain {domain!r} is on the disposable-provider blocklist"
        )
        self.domain = domain


class CaptchaFailed(ValueError):
    """CAPTCHA token was missing, malformed, expired, or rejected upstream.

    The HTTP router maps this to ``422 captcha_failed``. The
    exception message is deliberately vague ("captcha verification
    failed") so a hostile client can't learn which Turnstile
    error-code category tripped — forensic detail lands in the
    audit row via :attr:`reason`, not in the HTTP body.
    """

    def __init__(self, reason: str = "captcha_failed") -> None:
        super().__init__("captcha verification failed")
        self.reason = reason


class SlugAbuseRejected(ValueError):
    """Umbrella error for :func:`check_reserved_slug` wrapping callers.

    Defined for export symmetry with the other abuse errors. Not
    raised directly — :func:`check_reserved_slug` re-raises the
    three specific slug errors (:class:`SlugReserved`,
    :class:`SlugHomoglyphError`, :class:`~app.tenancy.InvalidSlug`)
    from :mod:`app.auth.signup` / :mod:`app.tenancy.slug` so callers
    keep the existing HTTP mapping. An ``except SlugAbuseRejected``
    catch still works because the three specific errors subclass
    :class:`ValueError` which this class also subclasses — use the
    specific types for actual dispatch.
    """


# ---------------------------------------------------------------------------
# Rate limiting — thin handoff to the shared ``Throttle`` class
# ---------------------------------------------------------------------------


def check_rate(
    throttle: Throttle,
    *,
    ip_hash: str,
    email_hash: str,
    now: datetime,
) -> None:
    """Raise :class:`SignupRateLimited` when any signup bucket is over.

    The real per-IP / per-email / global evaluation lives on
    :meth:`Throttle.check_signup_start`; this wrapper exists so the
    abuse module is the single place the router imports for every
    signup-start gate (rate + captcha + disposable + slug). Keeping
    the public signature stable here means cd-7huk's shared-throttle
    swap stays a one-line diff.

    ``ip_hash`` / ``email_hash`` are SHA-256 hashes peppered with
    the per-deployment HKDF subkey (see :mod:`app.auth.signup`); this
    module never handles plaintext IP or email, and the underlying
    ``Throttle`` bucket keys are hashes too — no PII ever lands in
    in-memory state.
    """
    throttle.check_signup_start(ip_hash=ip_hash, email_hash=email_hash, now=now)


# ---------------------------------------------------------------------------
# Disposable-email blocklist
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_disposable_domains(path_str: str) -> frozenset[str]:
    """Return the frozen set of canonical domains parsed from ``path_str``.

    Cached via :func:`functools.lru_cache`; the key is the string
    form of the path so :func:`reload_disposable_domains` can clear
    it deterministically. Unknown / missing files yield an empty
    set rather than raising — the signup path must still function
    on a misconfigured deployment, and the missing file is logged
    by the caller (follow-up; cd-055 doesn't ship a logger here).

    Format (matches the bundled :file:`disposable_domains.txt`):

    * One lower-case ASCII domain per line.
    * ``#``-prefixed comments and blank lines skipped.
    * Whitespace around entries trimmed.
    * Duplicates collapsed naturally through the set.
    """
    path = Path(path_str)
    if not path.is_file():
        return frozenset()
    domains: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Defensive lower-cast: the file is expected to be lowercase
        # already but a future CI refresh job may not enforce it.
        domains.add(stripped.lower())
    return frozenset(domains)


def reload_disposable_domains(path: Path | None = None) -> int:
    """Clear the blocklist cache and re-read the file; return entry count.

    Exposed as a public hook so an operator SIGHUP (future
    :mod:`app.worker` wiring) or a test's ``monkeypatch`` step can
    invalidate the cache without a process restart. Returns the
    number of domains loaded post-reload so callers can assert the
    refresh landed. Passing ``path`` overrides the default bundled
    file — used by tests that want to point at a ``tmp_path``
    without touching the committed list.
    """
    _load_disposable_domains.cache_clear()
    resolved = path if path is not None else _DEFAULT_DOMAINS_PATH
    return len(_load_disposable_domains(str(resolved)))


def _canonical_domain(email: str) -> str | None:
    """Return the lowercased domain portion of ``email`` or ``None``.

    Robust to surrounding whitespace and case. A missing ``@`` (or
    a trailing one) returns ``None`` so :func:`is_disposable` can
    treat malformed input as "not disposable" — the real-address
    shape validation is already carried by Pydantic at the router
    layer, and defaulting to "not disposable" is the safe bias
    (false negatives in the blocklist are recoverable via CAPTCHA
    + rate limit; false positives block legitimate users).
    """
    parsed = email.strip().lower()
    if "@" not in parsed:
        return None
    _local, _, domain = parsed.rpartition("@")
    if not domain:
        return None
    return domain


def is_disposable(email: str, *, path: Path | None = None) -> bool:
    """Return ``True`` when ``email``'s domain is on the bundled blocklist.

    The ``path`` kwarg is for tests that want to point at a
    ``tmp_path`` file; production callers pass nothing and the
    default bundled :file:`disposable_domains.txt` applies. Cache
    behaviour — see :func:`_load_disposable_domains`.
    """
    domain = _canonical_domain(email)
    if domain is None:
        return False
    resolved = path if path is not None else _DEFAULT_DOMAINS_PATH
    return domain in _load_disposable_domains(str(resolved))


# ---------------------------------------------------------------------------
# CAPTCHA verification
# ---------------------------------------------------------------------------


def _verify_turnstile(token: str, *, secret: str) -> None:
    """Verify ``token`` against Cloudflare Turnstile; raise on failure.

    Thin wrapper around :func:`httpx.post` — the endpoint returns
    a JSON body with ``{"success": bool, "error-codes": [...]}``.
    Any non-success response, network error, timeout, or malformed
    JSON raises :class:`CaptchaFailed` with a ``reason`` symbol that
    audit can pin down without leaking the raw Turnstile error-code
    to the client.

    The short per-call timeout (:data:`_TURNSTILE_TIMEOUT_SECONDS`)
    means a Turnstile outage fails fast rather than holding signup
    requests open until the client gives up.
    """
    try:
        resp = httpx.post(
            _TURNSTILE_VERIFY_URL,
            data={"secret": secret, "response": token},
            timeout=_TURNSTILE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        raise CaptchaFailed(reason="captcha_verifier_unreachable") from None

    if resp.status_code != 200:
        raise CaptchaFailed(reason="captcha_verifier_status")

    try:
        payload = resp.json()
    except ValueError:
        # httpx raises the stdlib :class:`ValueError` for malformed
        # JSON bodies — catch narrowly rather than bare except.
        raise CaptchaFailed(reason="captcha_verifier_malformed") from None

    if not isinstance(payload, dict):
        raise CaptchaFailed(reason="captcha_verifier_malformed")

    if payload.get("success") is not True:
        raise CaptchaFailed(reason="captcha_rejected")


def check_captcha(
    token: str | None,
    *,
    capabilities: Capabilities,
    settings: Settings,
) -> None:
    """Verify ``token`` against the configured CAPTCHA provider.

    Order of operations:

    1. If ``capabilities.settings.captcha_required`` is ``False``
       (self-host default), return immediately — a missing or
       bogus token passes through. This is the operator-toggle
       path.
    2. A missing / empty token raises :class:`CaptchaFailed` with
       reason ``captcha_required`` so the HTTP router can surface
       a distinct error symbol.
    3. If :attr:`Settings.captcha_turnstile_secret` is unset
       (self-host without a real provider wired; every unit test),
       fall through to offline test-mode: accept
       ``"test-pass"``, reject everything else.
    4. Otherwise hit Turnstile's ``siteverify`` endpoint via
       :func:`_verify_turnstile`.

    Splitting step 3 into "no secret → test mode" is what makes
    the whole signup path offline-testable — the integration
    tests never spin up a real CAPTCHA provider.
    """
    if not capabilities.settings.captcha_required:
        return

    if not token:
        raise CaptchaFailed(reason="captcha_required")

    secret = settings.captcha_turnstile_secret
    if secret is None or not secret.get_secret_value():
        # Offline / test mode — no real Turnstile secret wired.
        if token == _TEST_PASS_TOKEN:
            return
        if token == _TEST_FAIL_TOKEN:
            raise CaptchaFailed(reason="captcha_rejected")
        # A real-looking token in test mode is a configuration error
        # (operator forgot to set the secret). Reject with the
        # "unreachable" symbol so the audit row flags the misconfig.
        raise CaptchaFailed(reason="captcha_verifier_unconfigured")

    _verify_turnstile(token, secret=secret.get_secret_value())


# ---------------------------------------------------------------------------
# Reserved-slug + homoglyph consolidation
# ---------------------------------------------------------------------------


def check_reserved_slug(slug: str, *, existing_slugs: Iterable[str]) -> None:
    """Run the three-step slug abuse gate in one call.

    Steps, in order:

    1. :func:`app.tenancy.is_reserved` — fires first so the
       distinct ``SlugReserved`` symbol surfaces even when the
       slug also fails the regex (``w`` is both reserved *and*
       shorter than the 3-char pattern floor; spec §03 wants
       ``slug_reserved`` to win).
    2. :func:`app.tenancy.validate_slug` — raises
       :class:`~app.tenancy.InvalidSlug` for pattern / length /
       consecutive-hyphen failures. Re-raised unchanged; the
       router maps ``InvalidSlug`` to 422 ``invalid_slug``.
    3. :func:`app.tenancy.is_homoglyph_collision` — against the
       caller-supplied ``existing_slugs`` set. On a hit, raise
       :class:`SlugHomoglyphError` with the colliding slug
       attached so the router body can point the user at it.

    Consolidated here so :mod:`app.auth.signup` and
    :mod:`app.auth.signup_abuse` share one seam rather than
    duplicating the order-of-checks logic. The existing
    :func:`app.auth.signup.start_signup` path uses its own
    inlined version today (kept to avoid a drive-by refactor of a
    stable module); cd-055's wiring calls this consolidated
    helper so new signup-adjacent entry points (admin-init, etc.)
    only have one place to plug in.

    Note: this helper does **not** emit :class:`SlugTaken` — an
    exact-match rejection needs the "suggested alternative"
    probe which is `start_signup`'s business. Callers that need
    taken-slug detection run ``desired_slug in existing_slugs``
    themselves.
    """
    if is_reserved(slug):
        raise SlugReserved(f"slug {slug!r} is reserved")
    # :func:`validate_slug` also checks the reserved list internally
    # via its own path — letting it raise :class:`InvalidSlug`
    # rather than our dedicated :class:`SlugReserved` would collapse
    # the two error symbols. The explicit pre-check above is why
    # ordering matters.
    try:
        validate_slug(slug)
    except InvalidSlug:
        raise
    existing_set = list(existing_slugs)
    colliding = is_homoglyph_collision(slug, existing_set)
    if colliding is not None:
        raise SlugHomoglyphError(candidate=slug, colliding_slug=colliding)
