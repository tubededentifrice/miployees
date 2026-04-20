"""Pydantic-settings config loader.

Every adapter, worker, and API router imports ``get_settings`` (or the
module-level ``settings`` proxy) from here; nothing else reads
``os.environ`` directly. Values come from process environment variables
prefixed ``CREWDAY_`` with an optional ``.env`` file at the repo root
— see ``.env.example`` for the full template.

See ``docs/specs/01-architecture.md`` §"Runtime invariants" and
``docs/specs/16-deployment-operations.md`` §"Environment variables".
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_SECRET_MASK = "***"


class Settings(BaseSettings):
    """Process-wide configuration, loaded from env + optional ``.env``.

    Secrets are wrapped in :class:`pydantic.SecretStr` so they never
    appear in ``repr()`` or default serialisation. Use
    :meth:`safe_dump` when emitting settings to logs.
    """

    model_config = SettingsConfigDict(
        env_prefix="CREWDAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required ---
    database_url: str

    # --- Paths ---
    data_dir: Path = Path("./data")

    # --- Bind guard (see docs/specs/01 §"Runtime invariants" + §16) ---
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    trusted_interfaces: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["tailscale*"],
    )
    allow_public_bind: bool = False

    # --- Public URL ---
    public_url: str | None = None

    # --- WebAuthn (optional override; derived from public_url otherwise) ---
    # Only needed when the rp_id should differ from the origin's hostname —
    # e.g. hosting on ``app.example.com`` but scoping passkeys to the parent
    # ``example.com`` so they work on sibling subdomains too. See
    # ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics".
    webauthn_rp_id: str | None = None

    # --- SMTP (optional; see §10 messaging-notifications) ---
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    # Envelope sender for every outgoing message. ``None`` when SMTP
    # isn't configured; the :class:`app.adapters.mail.smtp.SMTPMailer`
    # requires it at construction time and will refuse to start without
    # one — the spec (§10) treats a message with no From as a bug.
    smtp_from: str | None = None
    # Whether STARTTLS (port 587) or implicit TLS (port 465) is attempted.
    # Plain-port 25 always skips TLS regardless. Operators who front the
    # relay over a trusted socket (localhost, unix socket bridge) can
    # flip this off; in every other deployment it must stay ``True``.
    smtp_use_tls: bool = True
    # Socket timeout (seconds) passed to ``smtplib.SMTP`` / ``SMTP_SSL``.
    # Applies to the initial connection and every subsequent I/O.
    smtp_timeout: int = 10
    # Domain used to build the ``Return-Path: bounce+<token>@<domain>``
    # header for future bounce-webhook correlation (§10). When ``None``,
    # the SMTPMailer falls back to the domain parsed from ``smtp_from``.
    smtp_bounce_domain: str | None = None

    # --- LLM (optional; see §11 llm-and-agents) ---
    openrouter_api_key: SecretStr | None = None

    # --- Signing / tokens ---
    root_key: SecretStr | None = None

    # --- Sessions (§03 "Sessions"; cd-cyq) ---
    # Session lifetime (days) for users who hold a ``manager`` surface
    # grant on any scope **or** are members of any ``owners`` permission
    # group — the "has_owner_grant" population. Everyone else gets the
    # longer :attr:`session_user_ttl_days` window. Recomputed on login,
    # not mid-session: a user who gains a manager grant mid-session keeps
    # their longer lifetime until the next sign-in. Mid-request refreshes
    # extend the existing value past half-life; see
    # :mod:`app.auth.session`.
    session_owner_ttl_days: int = 7
    # Session lifetime (days) for worker / client / guest users who hold
    # no manager surface grant and no owners-group membership anywhere.
    session_user_ttl_days: int = 30

    # --- Signup abuse mitigations (§15 "Self-serve abuse mitigations"; cd-055) ---
    # Cloudflare Turnstile server-side secret. ``None`` means "test /
    # offline mode": the CAPTCHA verifier accepts the fixed token
    # ``"test-pass"`` and rejects ``"test-fail"`` so unit tests never
    # hit the network. Operators running on the SaaS deployment set
    # this to the real Turnstile secret; the deployment setting
    # ``captcha_required`` then governs whether a token is mandatory
    # at all (spec §15 "Self-serve abuse mitigations"). The Turnstile
    # endpoint URL is pinned (not configurable) — changing the
    # provider is a code diff, not an ops switch.
    captcha_turnstile_secret: SecretStr | None = None

    # --- Runtime ---
    demo_mode: bool = False
    worker: Literal["internal", "external"] = "internal"
    storage_backend: Literal["localfs", "s3"] = "localfs"

    # --- Tenancy (cd-iwsv, cd-9il) ---
    # Gates the Phase-0 ``X-Test-Workspace-Id`` header path inside
    # :mod:`app.tenancy.middleware`. Default **off** in every
    # production deployment — a client that supplies the header on a
    # binary with the flag on can mint any :class:`WorkspaceContext`,
    # so the flag exists purely for the unit-test seam while the real
    # resolver lands (cd-9il keeps the header path for the rare test
    # that needs to bypass DB lookups). Set to ``True`` only in a
    # sandbox where every caller is trusted.
    phase0_stub_enabled: bool = False

    @field_validator("trusted_interfaces", mode="before")
    @classmethod
    def _split_trusted_interfaces(cls, value: object) -> object:
        """Parse comma-separated env input into a list.

        ``pydantic-settings`` would otherwise try to decode the raw
        env value as JSON for a ``list[str]`` field, which makes the
        natural ``CREWDAY_TRUSTED_INTERFACES=tailscale*,wg*`` form
        fail. Whitespace-only entries are dropped so a trailing comma
        doesn't turn into an empty-string glob.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def safe_dump(self) -> dict[str, Any]:
        """Return a dict with every :class:`SecretStr` masked.

        ``"***"`` for populated secrets, ``None`` for unset ones;
        non-secret fields pass through unchanged. Safe to log.
        """
        out: dict[str, Any] = {}
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[name] = _SECRET_MASK if value.get_secret_value() else None
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    Cached so repeated calls are free and every caller sees the same
    object. Tests that mutate env between cases must call
    ``get_settings.cache_clear()``.
    """
    return Settings()


def __getattr__(name: str) -> Any:
    """Lazy module attribute for ``from app.config import settings``.

    Deferring construction until first access keeps the module
    importable in test collection even when required env vars haven't
    been set yet — mirrors the ``get_settings()`` contract.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
