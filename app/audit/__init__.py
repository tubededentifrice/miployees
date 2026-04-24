"""Append-only audit log writer.

Every domain mutation calls :func:`write_audit` inside its open
Unit-of-Work. The row lands in the same transaction as the
mutation — commit the UoW and the audit row lands; rollback and
it's gone. The writer never calls ``session.commit()``; the
caller's UoW owns transaction boundaries (§01 "Key runtime
invariants" #3).

The ``diff`` argument is funnelled through
:func:`app.util.redact.redact` (scope ``"log"``) before persistence,
so PII leaked into a ``before_json`` / ``after_json`` payload (e.g.
an email typed into a task title) cannot survive into the on-disk
log. Spec: ``docs/specs/15-security-privacy.md`` §"Audit log"
guarantees that both halves of the diff pass through the same
redaction filter as the JSON log stream.

See ``docs/specs/01-architecture.md`` §"Key runtime invariants" #3,
``docs/specs/02-domain-model.md`` §"audit_log", and
``docs/specs/15-security-privacy.md`` §"Audit log".
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.redact import redact
from app.util.ulid import new_ulid

__all__ = ["write_audit"]


def write_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    entity_kind: str,
    entity_id: str,
    action: str,
    diff: dict[str, Any] | list[Any] | None = None,
    clock: Clock | None = None,
) -> AuditLog:
    """Append one audit row to the caller's open ``session``.

    The row carries the caller's :class:`~app.tenancy.WorkspaceContext`
    fields verbatim (workspace, actor identity, grant role,
    owner-member flag, correlation id). Persisting happens via
    ``session.add`` only — the function never flushes or commits, so
    the caller's UoW keeps full control of the transaction
    (:class:`~app.adapters.db.session.UnitOfWorkImpl`).

    ``diff`` is JSON-serialisable: a ``dict`` for structured changes,
    a ``list`` for bulk events, ``None`` for shape-free actions
    (``deleted``, ``archived``). ``None`` is persisted as an empty
    dict so downstream readers can rely on the column's non-null
    contract (§02 "audit_log"). The writer performs no pre-flight
    serialisation check — SQLAlchemy's ``JSON`` column raises at
    flush time if the payload is not JSON-compatible, and that
    surface is enough for the current call sites. Callers holding
    ``datetime`` / ``Decimal`` / ``UUID`` values must stringify
    themselves before calling.

    ``clock`` is optional; tests pin ``created_at`` via a
    :class:`~app.util.clock.FrozenClock`.
    """
    now = (clock if clock is not None else SystemClock()).now()
    # Run the diff through the canonical redactor so a stray email,
    # phone, IBAN, PAN, or credential blob that found its way into
    # ``before_json`` / ``after_json`` never reaches on-disk storage.
    # ``None`` keeps the existing ``{}`` default so the column's
    # NOT NULL contract (§02) holds.
    redacted_diff: dict[str, Any] | list[Any]
    if diff is None:
        redacted_diff = {}
    else:
        # ``redact`` preserves container type (dict stays dict, list
        # stays list); the ``cast`` narrows for mypy. The runtime
        # ``isinstance`` below is defensive — should a future upstream
        # change land a different type in ``diff``, we would rather
        # fail the NOT NULL write than persist something unexpected.
        scrubbed = redact(diff, scope="log")
        if isinstance(scrubbed, dict | list):
            redacted_diff = cast("dict[str, Any] | list[Any]", scrubbed)
        else:  # pragma: no cover - defensive
            raise TypeError(
                f"redacted diff must be dict|list, got {type(scrubbed).__name__}"
            )
    row = AuditLog(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        actor_kind=ctx.actor_kind,
        actor_grant_role=ctx.actor_grant_role,
        actor_was_owner_member=ctx.actor_was_owner_member,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        diff=redacted_diff,
        correlation_id=ctx.audit_correlation_id,
        created_at=now,
    )
    session.add(row)
    return row
