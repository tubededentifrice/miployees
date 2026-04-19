"""Capability registry — the single place features consult for availability.

See ``docs/specs/01-architecture.md`` §"Capability registry".

Features NEVER read env vars, sniff the DB URL, or branch on "SaaS vs
self-host". They ask ``capabilities.features.rls`` /
``capabilities.settings.signup_enabled`` and route accordingly.

Two halves:

* :class:`Features` — immutable probes computed once at boot from
  ``Settings`` + DB dialect + OS capabilities. Frozen so a drive-by
  mutation in a handler cannot silently change feature routing.
* :class:`DeploymentSettings` — operator-mutable subset backed by the
  ``deployment_setting`` table. Re-read by
  :meth:`Capabilities.refresh_settings` after any admin mutation.

The envelope :class:`Capabilities` is built once at boot via
:func:`probe` and stashed somewhere application state can reach it
(that wiring lives in the cd-leif / cd-jlms follow-ups — this module
is the seam they hang from).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select

from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.ports import DbSession
from app.config import Settings

__all__ = [
    "Capabilities",
    "DeploymentSettings",
    "Features",
    "probe",
]

_LOGGER: Final = logging.getLogger(__name__)

# Postgres URL prefixes we accept as "this is Postgres". Matches both
# the stdlib drivername form and the ``+psycopg`` / ``+asyncpg`` etc.
# variants the session factory normalises.
_POSTGRES_URL_PREFIXES: Final = (
    "postgresql://",
    "postgres://",
    "postgresql+",
    "postgres+",
)

# SQLite URL prefixes. Kept narrow so an unknown backend (mysql,
# oracle, …) falls through to "no known FTS" rather than a misleading
# probe of the interpreter's SQLite build. See ``_probe_features``.
_SQLITE_URL_PREFIXES: Final = (
    "sqlite://",
    "sqlite+",
)


@dataclass(frozen=True, slots=True)
class Features:
    """Immutable environment probes.

    Every field is computed at boot from ``Settings`` + live DB / OS /
    provider capabilities. Mutation after construction is a bug — the
    dataclass is frozen so attempting it raises ``FrozenInstanceError``.
    """

    rls: bool
    fulltext_search: bool
    concurrent_writers: bool
    object_storage: bool
    wildcard_subdomains: bool
    email_bounce_webhooks: bool
    llm_voice_input: bool


@dataclass(slots=True)
class DeploymentSettings:
    """Operator-mutable subset, refreshed from ``deployment_setting``.

    Defaults here are the "factory settings" — what a brand-new
    deployment sees before any admin override has been written. See
    ``docs/specs/01-architecture.md`` §"Capability registry" for the
    rationale behind each default.
    """

    signup_enabled: bool = True
    signup_throttle_overrides: dict[str, int] = field(default_factory=dict)
    require_passkey_attestation: bool = False
    # $5.00 default cap per workspace per 30d; seeds new ``quota_json``.
    llm_default_budget_cents_30d: int = 500


@dataclass(slots=True)
class Capabilities:
    """Envelope bundling immutable probes and mutable settings.

    Only :meth:`refresh_settings` is allowed to mutate — ``features``
    is frozen and any caller touching :attr:`settings` directly is a
    smell. The admin settings endpoint is the single writer: it
    persists through the ``deployment_setting`` repository and then
    calls :meth:`refresh_settings` so in-memory state matches the DB.
    """

    features: Features
    settings: DeploymentSettings

    def refresh_settings(self, session: DbSession) -> None:
        """Re-read the mutable subset from ``deployment_setting`` rows.

        Unknown keys are ignored so a future field can be rolled out
        by writing a row first and updating this code second — no
        crash window during deploy. Known keys are coerced to their
        declared type (``bool``/``int``/``dict``) so a bad JSON
        payload (e.g. ``"true"`` instead of ``true``) still lands in
        the right shape.

        Atomicity: every coercion runs first into local bindings; only
        after all four succeed are the fields assigned onto
        :attr:`settings`. A single bad JSON payload can't leave the
        registry half-updated. Fields are still mutated **in place**
        so callers holding a reference to :attr:`settings` observe the
        new values without a re-lookup.
        """
        rows = session.scalars(select(DeploymentSetting)).all()
        mapping = {row.key: row.value for row in rows}

        # Stage every coercion into locals before any write — if a bad
        # JSON payload lands on ``signup_throttle_overrides`` we don't
        # want ``signup_enabled`` to be half-applied.
        signup_enabled = self.settings.signup_enabled
        throttle_overrides = self.settings.signup_throttle_overrides
        require_attestation = self.settings.require_passkey_attestation
        budget_cents = self.settings.llm_default_budget_cents_30d
        if "signup_enabled" in mapping:
            signup_enabled = bool(mapping["signup_enabled"])
        if "signup_throttle_overrides" in mapping:
            throttle_overrides = dict(mapping["signup_throttle_overrides"])
        if "require_passkey_attestation" in mapping:
            require_attestation = bool(mapping["require_passkey_attestation"])
        if "llm_default_budget_cents_30d" in mapping:
            budget_cents = int(mapping["llm_default_budget_cents_30d"])

        # Every coercion succeeded — apply in place so existing
        # references to ``self.settings`` see the new values.
        self.settings.signup_enabled = signup_enabled
        self.settings.signup_throttle_overrides = throttle_overrides
        self.settings.require_passkey_attestation = require_attestation
        self.settings.llm_default_budget_cents_30d = budget_cents


def _is_postgres(database_url: str) -> bool:
    """Return ``True`` if ``database_url`` points at a Postgres backend."""
    return database_url.lower().startswith(_POSTGRES_URL_PREFIXES)


def _is_sqlite(database_url: str) -> bool:
    """Return ``True`` if ``database_url`` points at a SQLite backend."""
    return database_url.lower().startswith(_SQLITE_URL_PREFIXES)


def _sqlite_has_fts5() -> bool:
    """Return ``True`` if the interpreter's SQLite build compiled FTS5.

    The probe opens an in-memory DB and tries to create a virtual FTS5
    table; a build without FTS5 raises ``OperationalError``. The wider
    ``Exception`` fallback handles OS-level breakage (e.g. a sandbox
    refusing ``:memory:`` connections) — we log and treat those as
    "no FTS5" rather than crashing boot.
    """
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        # Can't even open an in-memory DB — treat FTS5 as unavailable.
        _LOGGER.warning("capabilities: sqlite3.connect failed during FTS5 probe")
        return False
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        except sqlite3.OperationalError:
            return False
        return True
    finally:
        conn.close()


def _probe_features(settings: Settings) -> Features:
    """Boot-time probe of every :class:`Features` field.

    Runs once in :func:`probe`. Each probe is independent so a failure
    in one (e.g. sqlite FTS5 not compiled) doesn't mask another.
    Fields stubbed to ``False`` for v1 carry a comment pointing at
    the follow-up Beads task that will light them up.
    """
    postgres = _is_postgres(settings.database_url)
    sqlite = _is_sqlite(settings.database_url)
    return Features(
        rls=postgres,
        # Postgres always has ``tsvector``; SQLite only when compiled
        # with FTS5 (the standard CPython build has it; Alpine images
        # sometimes don't). Unknown backends (mysql, oracle, …) fall
        # through to ``False`` — probing the interpreter's SQLite build
        # says nothing about FTS support on a different engine.
        fulltext_search=postgres or (sqlite and _sqlite_has_fts5()),
        # SQLite WAL concurrency is gated separately; treat only
        # Postgres as "concurrent writers" for now.
        concurrent_writers=postgres,
        object_storage=settings.storage_backend == "s3",
        # wildcard_subdomains will derive from CREWDAY_PUBLIC_URL + a
        # wildcard-cert sniff in a later task; stubbed false for v1.
        wildcard_subdomains=False,
        # email_bounce_webhooks is an SMTP-provider capability; the
        # Mailer port exposes it in the mail adapters task.
        email_bounce_webhooks=False,
        # llm_voice_input is an LLM-provider capability; routed via
        # the LLM adapter in §11 follow-ups.
        llm_voice_input=False,
    )


def probe(settings: Settings, session: DbSession | None = None) -> Capabilities:
    """Build a :class:`Capabilities` snapshot and log it.

    Called **once at boot** from application startup. A ``session``
    may be omitted (very early startup, unit tests) — in that case
    only the defaults in :class:`DeploymentSettings` apply and the
    mutable subset is refreshed later via
    :meth:`Capabilities.refresh_settings` as soon as the DB is reachable.
    """
    features = _probe_features(settings)
    caps = Capabilities(features=features, settings=DeploymentSettings())
    if session is not None:
        caps.refresh_settings(session)
    # Snapshot is secrets-free by construction: only booleans, ints,
    # and override dicts. Safe to log unmasked.
    _LOGGER.info(
        "capabilities snapshot: %s",
        {"features": features, "settings": caps.settings},
    )
    return caps
