"""SA-backed repositories implementing :mod:`app.domain.messaging.ports`.

The concrete class here adapts SQLAlchemy ``Session`` work to the
Protocol surface :mod:`app.domain.messaging.push_tokens` consumes
(cd-74pb):

* :class:`SqlAlchemyPushTokenRepository` — wraps the ``push_token``
  table and the per-workspace VAPID setting on
  ``workspace.settings_json``.

Reaches into both :mod:`app.adapters.db.messaging.models` (for
``push_token`` rows) and :mod:`app.adapters.db.workspace.models` (for
the ``Workspace.settings_json`` lookup that backs
:func:`~app.domain.messaging.push_tokens.get_vapid_public_key`).
Adapter-to-adapter imports are allowed by the import-linter — only
``app.domain → app.adapters`` is forbidden.

The repo carries an open ``Session`` and never commits or flushes
beyond what the underlying statements require — the caller's UoW
owns the transaction boundary (§01 "Key runtime invariants" #3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import PushToken
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.ports import (
    PushTokenRepository,
    PushTokenRow,
)

__all__ = [
    "SqlAlchemyPushTokenRepository",
]


def _to_row(row: PushToken) -> PushTokenRow:
    """Project an ORM ``PushToken`` into the seam-level row.

    Field-by-field copy — :class:`PushTokenRow` is frozen so the
    domain never mutates the ORM-managed instance through a shared
    reference.
    """
    return PushTokenRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


class SqlAlchemyPushTokenRepository(PushTokenRepository):
    """SA-backed concretion of :class:`PushTokenRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    or flushes outside what the underlying statements require — the
    caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def find_by_user_endpoint(
        self, *, workspace_id: str, user_id: str, endpoint: str
    ) -> PushTokenRow | None:
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one_or_none()
        return _to_row(row) if row is not None else None

    def list_for_user(
        self, *, workspace_id: str, user_id: str
    ) -> Sequence[PushTokenRow]:
        rows = self._session.scalars(
            select(PushToken)
            .where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
            )
            .order_by(PushToken.created_at.asc(), PushToken.id.asc())
        ).all()
        return [_to_row(row) for row in rows]

    def get_workspace_vapid_public_key(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        # ``settings_json`` is a flat dict — see
        # :class:`~app.adapters.db.workspace.models.Workspace` docstring.
        # We collapse "row missing", "settings not a dict", "key absent"
        # and "value not a non-empty string" into a single ``None``
        # return because they're operationally identical for the
        # caller (operator must provision the keypair). The defensive
        # ``isinstance`` mirrors the recovery-helper pattern in
        # ``app/auth/recovery.py``.
        payload = self._session.scalars(
            select(Workspace.settings_json).where(Workspace.id == workspace_id)
        ).one_or_none()
        if payload is None or not isinstance(payload, dict):
            return None
        value = payload.get(settings_key)
        if not isinstance(value, str) or not value:
            return None
        return value

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
        row = PushToken(
            id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
            created_at=created_at,
            last_used_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_row(row)

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
        # Pre-existing service contract: caller has just confirmed
        # the row exists via :meth:`find_by_user_endpoint`. Use the
        # same SELECT shape so the caller's UoW reuses the identity-
        # map entry rather than spawning a second instance for the
        # same primary key.
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one()

        # Mirror the prior service-layer change-detection so a benign
        # refresh (browser re-running its service worker against the
        # same row, with identical keys + UA) never marks the row
        # dirty. Keeps SQLAlchemy from issuing an UPDATE — which in
        # turn keeps the caller's "no audit row on benign refresh"
        # invariant intact.
        changed = False
        if p256dh is not None and row.p256dh != p256dh:
            row.p256dh = p256dh
            changed = True
        if auth is not None and row.auth != auth:
            row.auth = auth
            changed = True
        # ``user_agent`` follows the existing service rule of "only
        # refresh when the caller actually provided one" — a curl
        # caller passes ``None`` and we keep the prior snapshot.
        if user_agent is not None and row.user_agent != user_agent:
            row.user_agent = user_agent
            changed = True
        if changed:
            self._session.flush()
        return _to_row(row)

    def delete(self, *, workspace_id: str, user_id: str, endpoint: str) -> None:
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one_or_none()
        if row is None:
            # Idempotent: deleting a missing row is a no-op. The
            # caller's audit row still records the intent on a
            # successful prior find.
            return
        self._session.delete(row)
        self._session.flush()
