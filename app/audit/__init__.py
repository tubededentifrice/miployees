"""Append-only audit log writer.

Every domain mutation calls one of the two writer entry points
inside its open Unit-of-Work. The row lands in the same transaction
as the mutation — commit the UoW and the audit row lands; rollback
and it's gone. The writer never calls ``session.commit()``; the
caller's UoW owns transaction boundaries (§01 "Key runtime
invariants" #3).

``diff`` is funnelled through :func:`app.util.redact.redact` (scope
``"log"``) before persistence, so PII leaked into a ``before_json``
/ ``after_json`` payload (e.g. an email typed into a task title)
cannot survive into the on-disk log. Spec:
``docs/specs/15-security-privacy.md`` §"Audit log" guarantees that
both halves of the diff pass through the same redaction filter as
the JSON log stream.

Two writer entry points, **one redaction seam** (:func:`_redacted_diff`)
plus shared ULID + clock contracts:

* :func:`write_audit` — workspace-scoped. Takes a
  :class:`~app.tenancy.WorkspaceContext`; the row carries
  ``workspace_id = ctx.workspace_id`` and ``scope_kind = 'workspace'``.
  This is the path every workspace mutation under
  ``/api/v1/...`` and ``/w/<slug>/...`` already uses.
* :func:`write_deployment_audit` — deployment-scoped (cd-kgcc). Takes
  the actor identity and correlation id directly — there is no
  workspace context to draw them from. The row carries
  ``workspace_id = NULL`` and ``scope_kind = 'deployment'`` and
  feeds the ``GET /admin/api/v1/audit`` admin surface (§12
  "Admin surface"). Once the sibling cd-5tzf lands a typed
  ``DeploymentContext``, the two writers can be unified behind a
  single ``ctx: WorkspaceContext | DeploymentContext`` overload;
  the explicit-kwargs form is the cleanest API in the meantime
  because it does not require a synthetic context value at every
  call site.

See ``docs/specs/01-architecture.md`` §"Key runtime invariants" #3,
``docs/specs/02-domain-model.md`` §"audit_log", and
``docs/specs/15-security-privacy.md`` §"Audit log".
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.tenancy import ActorGrantRole, ActorKind, WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.redact import redact
from app.util.ulid import new_ulid

__all__ = ["write_audit", "write_deployment_audit"]


def _redacted_diff(
    diff: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | list[Any]:
    """Funnel ``diff`` through the canonical redactor.

    Returns ``{}`` for ``None`` so the column's NOT NULL contract
    (§02 "audit_log") holds. Preserves container type — a ``dict``
    in is a ``dict`` out, a ``list`` in is a ``list`` out — so
    downstream readers can keep relying on the shape callers passed.
    A future upstream change that funnelled a non-container value in
    here is treated as a programming error: we'd rather fail the
    NOT NULL write than persist something unexpected.
    """
    if diff is None:
        return {}
    # ``redact`` preserves container type (dict stays dict, list
    # stays list); the ``cast`` narrows for mypy. The runtime
    # ``isinstance`` below is defensive — should a future upstream
    # change land a different type in ``diff``, we would rather
    # fail the NOT NULL write than persist something unexpected.
    scrubbed = redact(diff, scope="log")
    if isinstance(scrubbed, dict | list):
        return cast("dict[str, Any] | list[Any]", scrubbed)
    raise TypeError(  # pragma: no cover - defensive
        f"redacted diff must be dict|list, got {type(scrubbed).__name__}"
    )


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
    """Append one workspace-scoped audit row to the caller's open ``session``.

    The row carries the caller's :class:`~app.tenancy.WorkspaceContext`
    fields verbatim (workspace, actor identity, grant role,
    owner-member flag, correlation id) and ``scope_kind = 'workspace'``.
    Persisting happens via ``session.add`` only — the function never
    flushes or commits, so the caller's UoW keeps full control of the
    transaction (:class:`~app.adapters.db.session.UnitOfWorkImpl`).

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

    For deployment-scope writes (admin mutations whose subject is
    the deployment itself), use :func:`write_deployment_audit`.
    """
    now = (clock if clock is not None else SystemClock()).now()
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
        diff=_redacted_diff(diff),
        correlation_id=ctx.audit_correlation_id,
        scope_kind="workspace",
        created_at=now,
    )
    session.add(row)
    return row


def write_deployment_audit(
    session: Session,
    *,
    actor_id: str,
    actor_kind: ActorKind,
    actor_grant_role: ActorGrantRole,
    actor_was_owner_member: bool,
    correlation_id: str,
    entity_kind: str,
    entity_id: str,
    action: str,
    diff: dict[str, Any] | list[Any] | None = None,
    clock: Clock | None = None,
) -> AuditLog:
    """Append one deployment-scoped audit row to the caller's open ``session``.

    The row carries ``workspace_id = NULL`` and ``scope_kind =
    'deployment'`` — exactly what the biconditional CHECK installed
    by the cd-kgcc migration enforces. The actor identity, grant
    role, owner-member flag, and correlation id are passed in
    directly because deployment-scope writes happen outside any
    :class:`~app.tenancy.WorkspaceContext`: the caller is acting on
    the deployment itself (token mint/revoke against an operator
    identity, ``deployment_setting`` edit, signup-policy change, …),
    not on workspace data.

    ``actor_grant_role`` for a deployment admin is conventionally
    ``'manager'`` — the v1 ``grant_role`` enum applies uniformly
    across both scope kinds (§02 "role_grants"; cd-wchi). The field
    is present so the column's NOT NULL contract holds (§02
    "audit_log"); the partitioning between deployment-admin and
    workspace-manager is read off ``scope_kind`` itself, not off
    the role.

    Same diff redaction, clock, and UoW invariants as
    :func:`write_audit` — the writer flushes nothing, commits
    nothing, and rolls back with the caller's transaction.

    Today this entry point is the deployment-scope sibling of the
    workspace-scope writer; once the typed ``DeploymentContext``
    lands (cd-5tzf), the two writers should be unified behind a
    single ``ctx: WorkspaceContext | DeploymentContext`` overload
    so call sites carry the scope as one immutable value rather
    than as a fan of named kwargs.
    """
    now = (clock if clock is not None else SystemClock()).now()
    row = AuditLog(
        id=new_ulid(),
        workspace_id=None,
        actor_id=actor_id,
        actor_kind=actor_kind,
        actor_grant_role=actor_grant_role,
        actor_was_owner_member=actor_was_owner_member,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        diff=_redacted_diff(diff),
        correlation_id=correlation_id,
        scope_kind="deployment",
        created_at=now,
    )
    session.add(row)
    return row
