"""Relying-party configuration + thin wrappers around ``py_webauthn``.

No other module in the codebase imports the upstream ``webauthn``
package directly — every registration / authentication ceremony goes
through the helpers exported here. Centralising the seam keeps
``rp_id``, expected origin, attestation policy, and algorithm list
consistent across every call site, and makes future library swaps a
single-file change.

See ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics",
§"Login", §"Privacy", and ``docs/specs/15-security-privacy.md``
§"Passkey specifics".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.authentication.verify_authentication_response import (
    VerifiedAuthentication,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from app.config import Settings, get_settings

__all__ = [
    "InvalidRegistrationResponse",
    "RelyingParty",
    "RelyingPartyMisconfigured",
    "VerifiedAuthentication",
    "VerifiedRegistration",
    "WebAuthnPolicy",
    "base64url_to_bytes",
    "bytes_to_base64url",
    "generate_authentication_challenge",
    "generate_registration_challenge",
    "make_relying_party",
    "options_to_dict",
    "policy",
    "verify_authentication",
    "verify_registration",
]

# ES256 and RS256 are the two algorithms the spec mandates for broad
# iOS/Android reach (§03 "WebAuthn specifics"). The COSE numeric ids
# are negative-space per RFC 8152.
_ALG_ES256: Final[int] = COSEAlgorithmIdentifier.ECDSA_SHA_256.value
_ALG_RS256: Final[int] = COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256.value
_DEFAULT_PUB_KEY_ALGS: Final[tuple[int, ...]] = (_ALG_ES256, _ALG_RS256)

# Matches py_webauthn's default; surfaced here so call sites read the
# timeout from policy() rather than hard-coding a magic number.
_DEFAULT_TIMEOUT_MS: Final[int] = 60_000

# Human-readable relying-party name surfaced to the authenticator UI
# during registration. The spec pins the product name here so a
# stray env var can't retitle the passkey prompt.
_RP_NAME: Final[str] = "crew.day"


class RelyingPartyMisconfigured(ValueError):
    """Raised when ``rp_id`` and ``origin`` disagree, or either is missing.

    A boot-time misconfiguration: either ``CREWDAY_PUBLIC_URL`` has no
    hostname, or the operator-supplied ``CREWDAY_WEBAUTHN_RP_ID`` isn't
    equal to or a registrable suffix of the origin's hostname. Either
    state would make every passkey ceremony fail at the browser level —
    we prefer crashing at boot to shipping a silently-broken deployment.
    """


@dataclass(frozen=True, slots=True)
class RelyingParty:
    """Process-wide WebAuthn relying-party identity.

    Frozen so a drive-by handler mutation can't retarget passkey
    ceremonies after boot. ``rp_id`` is the credential-scoping value
    every authenticator binds passkeys to; ``origin`` is the exact
    ``scheme://host[:port]`` the browser reports in ``clientDataJSON``.
    ``allowed_origins`` is the list verifications accept — today it
    always equals ``(origin,)`` but the field lets a future subdomain
    fanout (``app.example.com`` + ``www.example.com``) add entries
    without widening the call sites.
    """

    rp_id: str
    rp_name: str
    origin: str
    allowed_origins: tuple[str, ...]


_DEFAULT_USER_VERIFICATION: Final = UserVerificationRequirement.REQUIRED
_DEFAULT_ATTESTATION: Final = AttestationConveyancePreference.NONE
_DEFAULT_ATTACHMENT: Final = AuthenticatorAttachment.PLATFORM
_DEFAULT_RESIDENT_KEYS: Final = ResidentKeyRequirement.PREFERRED


@dataclass(frozen=True, slots=True)
class WebAuthnPolicy:
    """Policy knobs for registration + authentication ceremonies.

    Values match ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn
    specifics" exactly. Frozen so a stray handler can't quietly
    relax ``user_verification`` on one call site.
    """

    user_verification: UserVerificationRequirement = _DEFAULT_USER_VERIFICATION
    attestation: AttestationConveyancePreference = _DEFAULT_ATTESTATION
    attachment_preferred: AuthenticatorAttachment = _DEFAULT_ATTACHMENT
    attachment_allow_cross_platform: bool = True
    resident_keys: ResidentKeyRequirement = _DEFAULT_RESIDENT_KEYS
    # ES256 (-7), RS256 (-257) — tuple order is the RP's preference.
    pub_key_algs: tuple[int, ...] = _DEFAULT_PUB_KEY_ALGS
    timeout_ms: int = _DEFAULT_TIMEOUT_MS


# Schemes WebAuthn treats as a secure context. Anything else (``file``,
# ``ftp``, schemeless ``//host``) is rejected at boot — the browser
# would refuse the ceremony anyway, we just fail sooner and louder.
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


def _canonical_origin(public_url: str) -> str:
    """Return the RFC 6454 serialisation of ``public_url``.

    py_webauthn's ``expected_origin`` is compared **byte-for-byte**
    against ``clientDataJSON.origin`` (see
    ``webauthn/registration/verify_registration_response.py``). That
    makes any cosmetic drift a runtime failure: an uppercase host, a
    trailing path, a fragment, or a ``user:pass@`` userinfo section
    will each produce a mismatch the operator only discovers when a
    passkey ceremony silently fails in the browser.

    So we parse + recompose: lowercased scheme, lowercased host, port
    (only if present), nothing else. Anything ambiguous — missing
    scheme, unknown scheme, empty host — raises
    :class:`RelyingPartyMisconfigured` at boot instead.
    """
    parsed = urlparse(public_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise RelyingPartyMisconfigured(
            f"public_url must use http:// or https:// (got {public_url!r})"
        )
    host = parsed.hostname  # already lowercased + userinfo-stripped
    if not host:
        raise RelyingPartyMisconfigured(f"origin has no hostname: {public_url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RelyingPartyMisconfigured(
            f"public_url has an invalid port: {public_url!r}"
        ) from exc
    authority = f"{host}:{port}" if port is not None else host
    return f"{scheme}://{authority}"


def _derive_rp_id(origin: str) -> str:
    """Return ``origin``'s hostname (no port, no scheme, no path).

    ``origin`` here is assumed to already be a canonical origin produced
    by :func:`_canonical_origin`, so the hostname component is the
    lowercased DNS label we want to scope passkeys to. Browsers
    special-case ``localhost`` and ``127.0.0.1`` as valid ``rp_id``
    values under a secure-context exemption for dev loops; a public IP
    or a bare TLD (``com``) is **not** a legal ``rp_id`` per WebAuthn
    §5.1.3 step 7, but policing that here would block the dev fallback
    so we defer to the browser — the operator will see the failure
    immediately during first-passkey enrolment.
    """
    host = urlparse(origin).hostname
    if host is None:
        raise RelyingPartyMisconfigured(f"origin has no hostname: {origin!r}")
    return host


def _validate_rp_id_against_origin(rp_id: str, origin: str) -> None:
    """Fail fast if ``rp_id`` isn't the origin's host or a registrable suffix.

    WebAuthn requires ``rp_id`` to be equal to, or a registrable
    parent-domain suffix of, the origin's effective domain. We don't
    consult the Public Suffix List — the narrower "exact match or a
    dot-prefixed suffix" check rejects obvious misconfigurations while
    still letting operators pin ``example.com`` on a deployment served
    from ``app.example.com``.

    DNS labels are case-insensitive, so both sides are ``casefold``-ed
    before comparison; the stored ``rp_id`` on :class:`RelyingParty` is
    independently lowercased by :func:`make_relying_party`.
    """
    origin_host = urlparse(origin).hostname
    if origin_host is None:
        raise RelyingPartyMisconfigured(f"origin has no hostname: {origin!r}")
    rp_id_cf = rp_id.casefold()
    origin_host_cf = origin_host.casefold()
    if rp_id_cf != origin_host_cf and not origin_host_cf.endswith(f".{rp_id_cf}"):
        raise RelyingPartyMisconfigured(
            f"rp_id {rp_id!r} does not match origin host {origin_host!r}; "
            f"rp_id must equal the origin's hostname or be a registrable "
            f"suffix of it"
        )


def _dev_fallback_public_url(settings: Settings) -> str:
    """Synthesise a public URL from the bind address for dev loops.

    When ``CREWDAY_PUBLIC_URL`` is unset (typical for first-boot dev
    containers) we fall back to ``http://bind_host:bind_port``. We use
    ``http://`` unconditionally because the dev fallback exists only
    for loopback + Tailscale where TLS isn't terminated locally —
    browsers still allow WebAuthn over plaintext for ``localhost`` and
    ``127.0.0.1``. Not suitable for production: production deployments
    terminate TLS externally and MUST set ``CREWDAY_PUBLIC_URL`` —
    the bind-guard refuses public binds without it (see
    ``docs/specs/16-deployment-operations.md``).
    """
    return f"http://{settings.bind_host}:{settings.bind_port}"


def make_relying_party(settings: Settings | None = None) -> RelyingParty:
    """Build the process-wide :class:`RelyingParty` from :class:`Settings`.

    Order of precedence for ``rp_id``:

    1. ``CREWDAY_WEBAUTHN_RP_ID`` if set — but still validated against
       ``origin`` so a typo doesn't brick every login. Lowercased
       before use (DNS labels are case-insensitive).
    2. Otherwise, the hostname component of the effective origin
       (already lowercased by :func:`_canonical_origin`).

    ``origin`` is canonicalised before storage so it matches the
    browser's ``clientDataJSON.origin`` byte-for-byte — anything
    less would make every passkey verification fail at runtime.

    Raises :class:`RelyingPartyMisconfigured` on any mismatch.
    """
    settings = settings or get_settings()
    public_url = settings.public_url or _dev_fallback_public_url(settings)
    origin = _canonical_origin(public_url)
    override = settings.webauthn_rp_id
    rp_id = override.casefold() if override else _derive_rp_id(origin)
    _validate_rp_id_against_origin(rp_id, origin)
    return RelyingParty(
        rp_id=rp_id,
        rp_name=_RP_NAME,
        origin=origin,
        allowed_origins=(origin,),
    )


def policy() -> WebAuthnPolicy:
    """Return the default (and only) WebAuthn policy.

    Returned as a fresh instance per call so a caller that stashes a
    reference can never mutate state another caller will observe — the
    dataclass is frozen, but the habit keeps the seam obvious.
    """
    return WebAuthnPolicy()


def _authenticator_selection(pol: WebAuthnPolicy) -> AuthenticatorSelectionCriteria:
    """Translate our :class:`WebAuthnPolicy` into py_webauthn's shape.

    Attachment hint: the spec prefers ``platform`` (TouchID / Windows
    Hello) but wants cross-platform (YubiKey) available as a fallback.
    py_webauthn's ``authenticator_attachment`` is a single value, so
    we leave it unset when cross-platform is allowed — that lets the
    browser surface both options — and only pin a single attachment
    when cross-platform is explicitly forbidden.
    """
    attachment = (
        None if pol.attachment_allow_cross_platform else pol.attachment_preferred
    )
    return AuthenticatorSelectionCriteria(
        authenticator_attachment=attachment,
        resident_key=pol.resident_keys,
        user_verification=pol.user_verification,
    )


def _to_cose_algs(alg_ids: Sequence[int]) -> list[COSEAlgorithmIdentifier]:
    """Coerce a tuple of COSE ints into py_webauthn's enum type."""
    return [COSEAlgorithmIdentifier(alg) for alg in alg_ids]


def _to_credential_descriptors(
    credential_ids: Sequence[bytes],
) -> list[PublicKeyCredentialDescriptor]:
    """Wrap raw credential-id bytes as py_webauthn descriptors."""
    return [PublicKeyCredentialDescriptor(id=cid) for cid in credential_ids]


def options_to_dict(options_json: str) -> dict[str, Any]:
    """Parse py_webauthn's ``options_to_json`` output into a ``dict``.

    py_webauthn always serialises
    ``PublicKeyCredentialCreationOptions`` /
    ``PublicKeyCredentialRequestOptions`` as a JSON string because it
    knows the options travel the wire; callers that want structured
    data (to splice in extensions, stash the challenge alongside
    metadata, etc.) would otherwise re-parse the string locally. We
    expose the parse once so the single ``json`` dependency stays
    inside this seam module.
    """
    parsed: object = json.loads(options_json)
    if not isinstance(parsed, dict):
        raise RuntimeError("webauthn options_to_json returned non-object payload")
    return {str(k): v for k, v in parsed.items()}


def generate_registration_challenge(
    *,
    rp: RelyingParty,
    user_id: bytes,
    user_name: str,
    user_display_name: str,
    existing_credential_ids: Sequence[bytes] = (),
    pol: WebAuthnPolicy | None = None,
) -> tuple[str, bytes]:
    """Generate registration options for ``navigator.credentials.create()``.

    Returns ``(options_json, challenge)`` — JSON is what the handler
    sends to the browser, ``challenge`` is what the caller must persist
    alongside the pending registration so :func:`verify_registration`
    can cross-check it on the response.

    ``existing_credential_ids`` goes into ``excludeCredentials`` so the
    authenticator refuses to register a duplicate on a device the user
    already has enrolled (§03 "Additional passkeys": up to 5 per user).
    """
    pol = pol or policy()
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=rp.rp_name,
        user_id=user_id,
        user_name=user_name,
        user_display_name=user_display_name,
        attestation=pol.attestation,
        authenticator_selection=_authenticator_selection(pol),
        exclude_credentials=_to_credential_descriptors(existing_credential_ids),
        supported_pub_key_algs=_to_cose_algs(pol.pub_key_algs),
        timeout=pol.timeout_ms,
    )
    return options_to_json(options), options.challenge


def verify_registration(
    *,
    rp: RelyingParty,
    credential: dict[str, Any],
    expected_challenge: bytes,
    pol: WebAuthnPolicy | None = None,
) -> VerifiedRegistration:
    """Verify a ``navigator.credentials.create()`` response.

    ``credential`` is the raw JSON payload from the browser — it
    carries arbitrary client fields we do not model, so typing it as
    ``dict[str, Any]`` mirrors py_webauthn's own signature. Returns
    the library's :class:`VerifiedRegistration` unmodified: downstream
    ``passkey_credential`` writers (cd-8m4) pull ``credential_id``,
    ``credential_public_key``, ``sign_count``, and ``aaguid`` off it
    directly, which matches the spec's "we only store …" list
    (§03 "Privacy").
    """
    pol = pol or policy()
    return verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp.rp_id,
        expected_origin=list(rp.allowed_origins),
        require_user_verification=(
            pol.user_verification is UserVerificationRequirement.REQUIRED
        ),
        supported_pub_key_algs=_to_cose_algs(pol.pub_key_algs),
    )


def generate_authentication_challenge(
    *,
    rp: RelyingParty,
    allow_credential_ids: Sequence[bytes] = (),
    pol: WebAuthnPolicy | None = None,
) -> tuple[str, bytes]:
    """Generate request options for ``navigator.credentials.get()``.

    Empty ``allow_credential_ids`` is the norm on the login page:
    discoverable credentials let the authenticator pick the account
    and conditional UI (§03 "Login") surfaces a silent prompt. Non-
    empty lists are used for step-up / second-factor flows where the
    caller already knows which credentials are acceptable.
    """
    pol = pol or policy()
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        allow_credentials=_to_credential_descriptors(allow_credential_ids),
        user_verification=pol.user_verification,
        timeout=pol.timeout_ms,
    )
    return options_to_json(options), options.challenge


def verify_authentication(
    *,
    rp: RelyingParty,
    credential: dict[str, Any],
    expected_challenge: bytes,
    credential_public_key: bytes,
    credential_current_sign_count: int,
    pol: WebAuthnPolicy | None = None,
) -> VerifiedAuthentication:
    """Verify a ``navigator.credentials.get()`` response.

    Returns the library's :class:`VerifiedAuthentication` — the caller
    is responsible for persisting the new ``new_sign_count`` and for
    auto-revoking the credential on sign-count rollback
    (§15 "Passkey specifics"). This module intentionally stays out of
    the persistence path; it verifies the ceremony and hands back the
    facts.
    """
    pol = pol or policy()
    return verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp.rp_id,
        expected_origin=list(rp.allowed_origins),
        credential_public_key=credential_public_key,
        credential_current_sign_count=credential_current_sign_count,
        require_user_verification=(
            pol.user_verification is UserVerificationRequirement.REQUIRED
        ),
    )
