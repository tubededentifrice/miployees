"""Owner-only workspace settings — basics edit (cd-n6p).

Single public entry point :func:`update_basics` writes the four
identity-level base columns on a :class:`~app.adapters.db.workspace.models.Workspace`
row:

* ``name`` — display name shown in the UI.
* ``default_timezone`` — IANA tz database identifier (``Europe/Paris``,
  ``Pacific/Auckland``). Validated against
  :func:`zoneinfo.available_timezones`.
* ``default_locale`` — BCP-47 tag from the shipped locale list at
  :mod:`app.util.locales`.
* ``default_currency`` — ISO-4217 alpha-3 from the shipped allow-list
  at :mod:`app.util.currency`.

Authorisation is **owner-only** — even managers cannot rename the
workspace or change the default formatting. §05 "Surface grants at a
glance" pins the owners group as the governance anchor; the
capability matrix rolls everything else through the action catalog,
but the four base columns are the identity of the workspace itself,
restricted to the governance anchor. Non-owner callers raise
:class:`OwnersOnlyError`; the API layer maps this to 403.

**Validation.** Each invalid field raises a per-field
:class:`WorkspaceFieldInvalid` with the field path so the API layer
maps to 422 with field-specific errors. Validation runs **before**
any DB write so a single bad field does not leave a half-applied
update behind. The service signature is restrictive (only the four
named kwargs); extras are silently dropped at the API layer because
the DTO carries ``extra="forbid"`` and rejects unknown fields with a
422 there.

**Partial update.** Only the provided non-None fields are written.
Empty payloads (every kwarg ``None``) are a no-op — no audit row,
``updated_at`` not bumped — so the SSE invalidation seam never fires
for a write that changed nothing.

**Audit.** One audit row per call when at least one field changed,
carrying the old / new value per changed field. Diff shape mirrors
the convention used by :func:`app.services.employees.service.update_profile`:
``{"before": {...}, "after": {...}}`` with only the changed keys.

**Tenancy.** The service trusts ``ctx.workspace_id``; it never
accepts a workspace id from a caller payload (§01 v1 invariant).
The Workspace row itself is identity-scoped, so the lookup runs
through :func:`tenant_agnostic` — the ORM tenant filter does not
apply on a base table that **is** the workspace.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3).

**Timezone semantics.** Changing ``default_timezone`` does NOT
rewrite stored timestamps — UTC at rest is the v1 invariant (§02
"Time"). The new tz only changes display-time conversion on future
reads.

See ``docs/specs/02-domain-model.md`` §"workspaces" /
§"Settings cascade",
``docs/specs/05-employees-and-roles.md`` §"Surface grants at a
glance", ``docs/specs/14-web-frontend.md`` §"Workspace settings".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import available_timezones

from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import Workspace
from app.audit import write_audit
from app.authz.owners import is_owner_member
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.currency import is_valid_currency, normalise_currency
from app.util.locales import is_valid_locale, normalise_locale

__all__ = [
    "OwnersOnlyError",
    "WorkspaceBasics",
    "WorkspaceFieldInvalid",
    "update_basics",
]


# Cap matching the shape on :class:`Workspace.name` (free-form text
# but bounded so the audit / log surface does not balloon on a
# pasted blob). The DB column is uncapped; bounding here is a
# defence-in-depth UX gate.
_MAX_NAME_LEN = 200


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OwnersOnlyError(PermissionError):
    """Caller is not a member of the workspace ``owners`` group.

    403-equivalent. Raised by :func:`update_basics` when the actor
    fails the :func:`is_owner_member` check. The API layer maps this
    to ``403 owners_only``.
    """


class WorkspaceFieldInvalid(ValueError):
    """A submitted basics field failed validation.

    422-equivalent. ``field`` carries the field path
    (``"name"`` / ``"timezone"`` / ``"locale"`` / ``"currency"``) so
    the API layer can render a field-specific 422. ``reason`` is a
    short human string for the message body.
    """

    __slots__ = ("field", "reason")

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"workspace basics field {field!r} invalid: {reason}")
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceBasics:
    """Read projection of the four basics fields after an update.

    Returned by :func:`update_basics` so the caller (router) can echo
    the persisted values back to the client without a second round-
    trip. Carries the bumped ``updated_at`` so the SSE invalidation
    payload can include it.
    """

    workspace_id: str
    name: str
    default_timezone: str
    default_locale: str
    default_currency: str
    updated_at: datetime


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_name(value: str) -> str:
    """Return ``value`` stripped, or raise.

    Empty / whitespace-only strings collapse to a 422 — the column is
    NOT NULL and a blank workspace name would render as an empty UI
    label.
    """
    stripped = value.strip()
    if not stripped:
        raise WorkspaceFieldInvalid("name", "must be a non-blank string")
    if len(stripped) > _MAX_NAME_LEN:
        raise WorkspaceFieldInvalid("name", f"exceeds {_MAX_NAME_LEN} characters")
    return stripped


def _validate_timezone(value: str) -> str:
    """Return ``value`` if it is a known IANA tz, else raise.

    Membership check against :func:`zoneinfo.available_timezones`. The
    returned set is platform-dependent (Python ships its own tzdata on
    Windows, falls back to the OS db on POSIX); on every supported
    crew.day deployment target the set covers the standard IANA tz
    database. We do not normalise case — the IANA db is case-sensitive
    and ``Europe/paris`` is not the same key as ``Europe/Paris``.
    """
    if value not in available_timezones():
        raise WorkspaceFieldInvalid("timezone", f"unknown IANA timezone {value!r}")
    return value


def _validate_locale(value: str) -> str:
    """Return the normalised locale tag if valid, else raise.

    Two-step check: shape via :data:`app.util.locales.BCP_47_PATTERN`,
    then membership in :data:`app.util.locales.SHIPPED_LOCALES`. A
    well-shaped tag we don't ship (``ja-JP`` while we don't ship a
    Japanese bundle) raises a 422 — the remediation is to ship the
    bundle and add the entry, not to silently fall back.
    """
    normalised = normalise_locale(value)
    if not is_valid_locale(normalised):
        raise WorkspaceFieldInvalid(
            "locale",
            f"locale {value!r} is not in the shipped set",
        )
    return normalised


def _validate_currency(value: str) -> str:
    """Return the normalised ISO-4217 code if valid, else raise.

    Funnels through :func:`app.util.currency.normalise_currency` (caps
    + strip) and :func:`is_valid_currency` (3-letter alpha + allow-list).
    A code outside the allow-list raises a 422 — the same surface used
    by the property and expense services so a typo lands the same
    error everywhere.
    """
    normalised = normalise_currency(value)
    if not is_valid_currency(normalised):
        raise WorkspaceFieldInvalid(
            "currency",
            f"currency {value!r} is not in the ISO-4217 allow-list",
        )
    return normalised


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def update_basics(
    session: Session,
    ctx: WorkspaceContext,
    *,
    name: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    currency: str | None = None,
    actor_user_id: str,
    clock: Clock | None = None,
) -> WorkspaceBasics:
    """Owner-only partial update of the four workspace basics fields.

    Authorisation: the caller must be a member of the workspace's
    ``owners`` permission group. Non-owners raise
    :class:`OwnersOnlyError` (mapped to 403). The check uses
    :func:`app.authz.owners.is_owner_member` — the canonical
    "is U an ``owners@<workspace>`` member?" lookup — rather than
    re-reading ``ctx.actor_was_owner_member``: the context flag is set
    once at request entry and may go stale across long-running
    sessions, so we re-resolve at write time as defence-in-depth.

    Validation: every supplied field is validated **before** any DB
    write. A single bad field raises :class:`WorkspaceFieldInvalid`
    and leaves the DB untouched.

    Partial update: only fields with non-``None`` values are
    considered. A call with every kwarg ``None`` is a deliberate
    no-op — no audit row, ``updated_at`` not bumped, the SSE seam
    does not fire.

    Same-value writes: a field that matches the current row value
    is treated as a no-change (not in the diff, not in the audit
    row's ``before`` / ``after``). When every supplied field
    matches, the call collapses to the no-op shape above.

    Audit: one ``workspace.basics_updated`` row per call when at
    least one field actually changed, carrying
    ``{"before": {...}, "after": {...}}`` with only the changed
    keys. Mirrors the diff shape used by
    :func:`app.services.employees.service.update_profile`.

    Returns the persisted projection so the caller can echo it to
    the client without a second SELECT.
    """
    resolved_clock = clock if clock is not None else SystemClock()

    # 1. Authorisation. Re-resolve the owner check at write time —
    #    ctx.actor_was_owner_member could go stale on a long session.
    if not is_owner_member(
        session,
        workspace_id=ctx.workspace_id,
        user_id=actor_user_id,
    ):
        raise OwnersOnlyError(
            f"actor {actor_user_id!r} is not an owners-group member of "
            f"workspace {ctx.workspace_id!r}"
        )

    # 2. Validate every supplied field BEFORE the DB write so a bad
    #    field does not leave a half-applied update behind.
    cleaned: dict[str, str] = {}
    if name is not None:
        cleaned["name"] = _validate_name(name)
    if timezone is not None:
        cleaned["default_timezone"] = _validate_timezone(timezone)
    if locale is not None:
        cleaned["default_locale"] = _validate_locale(locale)
    if currency is not None:
        cleaned["default_currency"] = _validate_currency(currency)

    # 3. Load the workspace row. ``Workspace`` is the tenancy root
    #    table — it does not register with the ORM tenant filter
    #    (filtering by workspace_id on the workspaces table itself
    #    is circular). ``tenant_agnostic`` documents the intent and
    #    keeps the read explicit; the ``ctx.workspace_id`` predicate
    #    is supplied via :meth:`Session.get`.
    with tenant_agnostic():
        ws = session.get(Workspace, ctx.workspace_id)
    if ws is None:
        # The middleware must have resolved a real workspace_id into
        # the context — a missing row at this point is a programmer
        # error, not a 404. Surface it loudly rather than silently
        # short-circuiting.
        raise RuntimeError(
            f"workspace {ctx.workspace_id!r} present in ctx but absent in DB"
        )

    # 4. Compute the actual diff — fields the caller sent AND that
    #    differ from the current row value. A same-value write is a
    #    no-op so the audit trail does not record forensic noise.
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for column, new_value in cleaned.items():
        current = getattr(ws, column)
        if current != new_value:
            before[column] = current
            after[column] = new_value

    # 5. Empty-update / same-value-only-update no-op. Return the
    #    current projection unchanged; ``updated_at`` is NOT bumped
    #    because no state changed and SSE subscribers should not
    #    refresh on a write that changed nothing.
    if not after:
        return _project(ws)

    # 6. Apply the diff and bump ``updated_at``.
    for column, new_value in after.items():
        setattr(ws, column, new_value)
    now = resolved_clock.now()
    ws.updated_at = now

    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="workspace",
        entity_id=ws.id,
        action="workspace.basics_updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )

    return _project(ws)


def _project(ws: Workspace) -> WorkspaceBasics:
    """Map a :class:`Workspace` row into the return DTO."""
    return WorkspaceBasics(
        workspace_id=ws.id,
        name=ws.name,
        default_timezone=ws.default_timezone,
        default_locale=ws.default_locale,
        default_currency=ws.default_currency,
        updated_at=ws.updated_at,
    )
