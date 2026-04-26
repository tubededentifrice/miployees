"""Messaging context — repository port for the web-push subscription seam.

Defines :class:`PushTokenRepository`, the seam
:mod:`app.domain.messaging.push_tokens` uses to read and write the
``push_token`` rows + the per-workspace VAPID public key in
``workspace.settings_json`` — without importing SQLAlchemy model
classes (cd-74pb).

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``) and a SQLAlchemy adapter under
``app/adapters/db/<context>/`` (cd-jzfc reconciled the placement
introduced by cd-duv6). The SA-backed concretion lives in
:mod:`app.adapters.db.messaging.repositories`; tests substitute fakes.

The repo carries an open SQLAlchemy ``Session`` so the audit writer
(:func:`app.audit.write_audit`) — which still takes a concrete
``Session`` today — can ride the same Unit of Work without forcing
callers to thread a second seam. Drops once the audit writer gains
its own Protocol.

The repo-shaped value object :class:`PushTokenRow` mirrors the domain's
:class:`~app.domain.messaging.push_tokens.PushTokenView`. It lives on
the seam so the SA adapter has a domain-owned shape to project ORM
rows into without importing the service module that produces the view
(which would create a circular dependency between ``push_tokens`` and
this module).

Protocol is deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this Protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "PushTokenRepository",
    "PushTokenRow",
]


# ---------------------------------------------------------------------------
# Row shape (value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PushTokenRow:
    """Immutable projection of a ``push_token`` row.

    Mirrors the shape of
    :class:`app.domain.messaging.push_tokens.PushTokenView`; declared
    here so the Protocol surface does not depend on the service module
    (which itself imports this seam).
    """

    id: str
    workspace_id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime | None


# ---------------------------------------------------------------------------
# PushTokenRepository
# ---------------------------------------------------------------------------


class PushTokenRepository(Protocol):
    """Read + write seam for ``push_token`` plus the workspace VAPID setting.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits or flushes outside what the underlying
    statements require — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3).
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its
        own Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def find_by_user_endpoint(
        self, *, workspace_id: str, user_id: str, endpoint: str
    ) -> PushTokenRow | None:
        """Return the ``(workspace_id, user_id, endpoint)`` row or ``None``.

        Drives both the idempotent ``register`` upsert and the
        ``unregister`` lookup. Scoped to ``workspace_id`` for tenant
        hygiene even though the ORM tenant filter already applies —
        defence-in-depth matches the rest of the messaging service.
        """
        ...

    def list_for_user(
        self, *, workspace_id: str, user_id: str
    ) -> Sequence[PushTokenRow]:
        """Return every push token for ``user_id`` ordered by creation.

        Stable secondary sort on ``id`` so callers that page or diff
        the response see a deterministic order across calls. Returns
        an empty sequence when the user holds no rows in the
        workspace — the ``/me`` surface treats "no devices" as a
        normal state, not an error.
        """
        ...

    def get_workspace_vapid_public_key(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        """Return the VAPID public-key value at ``settings_key`` or ``None``.

        Reads from ``workspace.settings_json[settings_key]``. Returns
        ``None`` for any of:

        * the workspace row is missing (defensive — the tenancy
          middleware should have resolved it);
        * the ``settings_json`` payload is not a dict (corruption);
        * the key is absent;
        * the value is not a non-empty string.

        The caller maps every miss to a single
        :class:`~app.domain.messaging.push_tokens.VapidNotConfigured`
        — the four shapes are operationally identical (the operator
        needs to provision the keypair) and a unified return surface
        keeps the domain service free of model imports.
        """
        ...

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        token_id: str,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
        created_at: datetime,
    ) -> PushTokenRow:
        """Insert a fresh ``push_token`` row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        FK reference to ``entity_id``) sees the new row.
        """
        ...

    def update_keys(
        self,
        *,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str | None = None,
        auth: str | None = None,
        user_agent: str | None = None,
    ) -> PushTokenRow:
        """Refresh the encryption material on an existing row.

        Used by the idempotent re-subscribe path in :func:`register`:
        a browser that re-runs its service worker against the same
        ``(user_id, endpoint)`` may have rotated ``p256dh`` / ``auth``
        and may carry a new ``user_agent``. Each kwarg is applied
        only when not ``None``; ``user_agent`` follows the existing
        service rule of "only refresh when the caller actually
        provided one" (a curl caller passes ``None`` and we keep the
        prior snapshot).

        The SA concretion mirrors the prior service-layer change-
        detection so a no-op refresh never marks the row dirty —
        keeps the audit "no row written on benign refresh" invariant
        intact.

        Flushes when something actually changed.
        """
        ...

    def delete(self, *, workspace_id: str, user_id: str, endpoint: str) -> None:
        """Hard-delete the named row.

        Caller is responsible for the existence check via
        :meth:`find_by_user_endpoint` — the SA concretion treats a
        missing row as a no-op so a stale "remove me again" doesn't
        trip an :class:`~sqlalchemy.orm.exc.UnmappedInstanceError` at
        flush. The caller's audit row still records the intent on a
        successful prior find.
        """
        ...
