"""``occurrence`` state-machine service (§06 "State machine").

Sibling of :mod:`app.domain.tasks.assignment` — where that module
decides **who** a task is on, this module decides **what** happens
to the task as it moves through its life cycle: ``pending →
in_progress → done``, with ``skipped`` / ``cancelled`` as
terminal branches and ``overdue`` as a soft-state detour.

## Public surface

* :func:`start` — drive ``pending → in_progress``.
* :func:`complete` — drive ``pending | in_progress → done``; runs
  the evidence-policy gate, the required-checklist gate, and the
  inventory + asset-action + tombstone side-effects.
* :func:`skip` — drive any non-terminal state to ``skipped`` when
  the resolved policy allows. Owners / managers bypass the policy
  gate; workers respect it.
* :func:`cancel` — drive any non-terminal state to ``cancelled``.
  Owners / managers only; workers are rejected with a typed error.
* :func:`revert_overdue` — drop the soft ``overdue`` state back to
  ``pending`` or ``in_progress`` with a dedicated audit action.

Each entry point returns a frozen :class:`TaskState` view
(``task_id`` + resolved ``state`` + canonical timestamps) so the
API layer can surface the new row without re-reading the table.

## Injectable hooks

Several §06 side-effects depend on tables that are not yet in the
schema, or on a settings-cascade resolver that has not landed. Rather
than paper over the gap with half-implementations, this module
exposes every such touchpoint as an injectable callable with a
default that is either permissive (for policy knobs) or no-op (for
absent tables). Concrete downstream migrations plug in real bodies
through the keyword-only parameters on the entry points.

* :data:`EvidencePolicyResolver` — resolves the photo policy per
  §06 "Evidence policy inheritance". Default reads
  :attr:`Occurrence.photo_evidence` directly (the narrow path that
  works today); the real cascade (workspace → property → unit →
  work-engagement → task, with ``forbid`` absolute) lands with
  cd-settings-cascade.
* :data:`ChecklistRequiredResolver` — resolves
  ``tasks.checklist_required``. Default ``True`` — safe: if no
  required items exist the gate is vacuous, and if they do the
  worker must tick them.
* :data:`SkipAllowedResolver` — resolves
  ``tasks.allow_skip_with_reason``. Default ``True`` (permissive);
  owners / managers bypass regardless.
* :data:`InventoryApplyHook` — reads
  ``Occurrence.inventory_consumption_json`` and writes one
  :class:`~app.adapters.db.inventory.models.Movement` row per SKU
  with a negative delta. Override with a no-op hook to suppress
  per ``inventory.apply_on_task = false``.
* :data:`AssetActionHook` — updates ``asset_action.last_performed_*``
  (§21) when the task has ``asset_action_id``. Default no-op —
  the ``asset_action`` table is not in the schema yet.
* :data:`TombstoneHook` — writes the ``task_completion`` tombstone
  (§06 "Completing a task" #5). Default no-op — the
  ``task_completion`` table is not in the schema yet.

## Concurrent completion

§06 "Concurrent completion": two writers landing ``complete()`` on
the same task in overlapping transactions both win on the field
updates — the later writer overwrites the earlier one — and the
audit log records **both** completions. The loser's transaction
still sees its 200 + final state; a ``task.complete_superseded``
audit row against the displaced ``completed_by_user_id`` carries
the displaced ``completed_at`` + ``completed_by_user_id`` in its
diff so reports can reconstruct the sequence.

The implementation loads the row, inspects its ``state`` +
``completed_*`` fields, writes the new completion, and — when the
row was already ``done`` — emits a second audit row
(``task.complete_superseded``) alongside the regular
``task.complete``. Locking is deliberately optimistic: the spec's
rationale is that usability wins over strict serialisation here,
and the displaced completion is never silently lost.

## Permission posture

* :func:`complete` / :func:`start`: caller must be the current
  ``assignee_user_id`` OR hold a workspace-level ``manager`` /
  ``owner`` grant (``ctx.actor_grant_role == "manager"`` or
  ``ctx.actor_was_owner_member``).
* :func:`skip`: when :data:`SkipAllowedResolver` returns ``False``,
  only owners / managers can skip — workers are rejected.
* :func:`cancel`: owners / managers only. Workers raise
  :class:`PermissionDenied`.

These checks are defence-in-depth; the API layer runs its
permission catalogue first (§05) and routes through to these
service calls after the 403/404 pass. Keeping the checks here
means a direct programmatic call (agent runtime, CLI, NL intake
committer) cannot sidestep the §06 gates.

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine",
§"Completing a task", §"Checklist items", §"Evidence",
§"Evidence policy inheritance", §"Skipping and cancellation",
§"Concurrent completion".
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Movement
from app.adapters.db.tasks.models import ChecklistItem, Evidence, Occurrence
from app.adapters.storage.mime import FiletypeMimeSniffer
from app.adapters.storage.ports import (
    MimeSniffer,
    Storage,
)
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import (
    TaskCancelled,
    TaskCompleted,
    TaskSkipped,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AssetActionHook",
    "ChecklistRequiredResolver",
    "EvidenceContentTypeNotAllowed",
    "EvidenceGpsPayloadInvalid",
    "EvidencePolicyResolver",
    "EvidenceRequired",
    "EvidenceTooLarge",
    "EvidenceView",
    "FileEvidenceKind",
    "InvalidStateTransition",
    "InventoryApplyHook",
    "PermissionDenied",
    "PhotoForbidden",
    "RequiredChecklistIncomplete",
    "SkipAllowedResolver",
    "SkipNotPermitted",
    "TaskNotFound",
    "TaskState",
    "TombstoneHook",
    "add_file_evidence",
    "add_note_evidence",
    "cancel",
    "complete",
    "list_evidence",
    "revert_overdue",
    "skip",
    "start",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


# The §06 enum, broken out so ``_assert_transition`` + the public
# view stay readable. ``overdue`` is accepted as an in-enum source
# for the revert path; the cd-hurw migration widened the DB CHECK
# to admit the value alongside the rest of the §06 set.
TaskStateName = Literal[
    "scheduled",
    "pending",
    "in_progress",
    "done",
    "skipped",
    "cancelled",
    "overdue",
]


# Shape of the photo-policy resolver. Returns one of three concrete
# values; ``inherit`` never reaches the service — the resolver
# collapses the cascade before answering.
EvidencePolicy = Literal["forbid", "require", "optional"]

EvidencePolicyResolver = Callable[
    [Session, WorkspaceContext, Occurrence], EvidencePolicy
]
"""Port: resolve the effective photo-evidence policy for a task.

Default :func:`_default_evidence_policy` reads
``Occurrence.photo_evidence`` directly (``disabled`` → ``forbid``;
``required`` → ``require``; ``optional`` → ``optional``). The real
cascade landing with cd-settings-cascade walks workspace → property
→ unit → work-engagement → task with ``forbid`` absolute.
"""

ChecklistRequiredResolver = Callable[[Session, WorkspaceContext, Occurrence], bool]
"""Port: resolve ``tasks.checklist_required`` for a task.

Default :func:`_default_checklist_required` returns ``True`` — if
the template seeded required items, they must be ticked to
complete. Override to ``False`` for workspaces that opt out via
the settings cascade (cd-settings-cascade).
"""

SkipAllowedResolver = Callable[[Session, WorkspaceContext, Occurrence], bool]
"""Port: resolve ``tasks.allow_skip_with_reason`` for a task.

Default :func:`_default_skip_allowed` returns ``True`` (permissive).
Owners / managers bypass the resolver entirely — a ``False`` result
only locks out workers.
"""

InventoryApplyHook = Callable[[Session, WorkspaceContext, Occurrence], None]
"""Port: apply ``inventory_consumption_json`` at completion time.

Default :func:`_default_inventory_apply` reads the task's
consumption map and writes one :class:`Movement` row per SKU with
a negative delta and ``reason='consume'``. Override with a no-op
to suppress per ``inventory.apply_on_task = false`` (§08).
"""

AssetActionHook = Callable[[Session, WorkspaceContext, Occurrence], None]
"""Port: stamp ``asset_action.last_performed_*`` at completion.

Default :func:`_default_asset_action` is a no-op — the
``asset_action`` table is not in the schema yet. The real body
plugs in with the cd-asset-action-v1 follow-up.
"""

TombstoneHook = Callable[[Session, WorkspaceContext, Occurrence], None]
"""Port: write the ``task_completion`` tombstone row.

Default :func:`_default_tombstone` is a no-op — the
``task_completion`` table is not in the schema yet. The real body
plugs in with the cd-task-completion-tombstone follow-up.
"""


@dataclass(frozen=True, slots=True)
class TaskState:
    """Public shape returned by every entry point.

    Frozen + slotted so the API layer can reflect it into a response
    without the risk of a service caller mutating the payload post
    hoc. Carries the canonical timestamps callers render: ``state``
    for the chip, ``completed_at`` / ``completed_by_user_id`` for
    the "Marked done by … at …" byline, ``reason`` for the skip /
    cancel copy.
    """

    task_id: str
    state: TaskStateName
    completed_at: datetime | None = None
    completed_by_user_id: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaskNotFound(LookupError):
    """The task id is unknown in the caller's workspace (404)."""


class InvalidStateTransition(ValueError):
    """Source → target transition is not allowed by §06 "State machine".

    Carries both states so the API layer can surface a clear 422.
    """

    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            f"illegal task state transition {current!r} → {target!r}; "
            "see §06 'State machine'."
        )
        self.current = current
        self.target = target


class PermissionDenied(PermissionError):
    """Caller's role is not allowed to drive this transition.

    403-equivalent. Raised when a worker attempts :func:`cancel`, a
    non-assignee worker attempts :func:`complete` / :func:`start`,
    or a worker hits :func:`skip` on a workspace that disables it
    via the settings cascade.
    """


class PhotoForbidden(ValueError):
    """A photo was supplied but the resolved policy is ``forbid``.

    422-equivalent. §06 "Evidence policy inheritance": ``forbid``
    is absolute — the UI hides the picker and the API rejects any
    payload that carries one.
    """


class EvidenceRequired(ValueError):
    """The resolved evidence policy is ``require`` but no photo landed.

    422-equivalent. The caller must upload a photo and link its
    ``evidence`` id before the state flips to ``done``.
    """


class RequiredChecklistIncomplete(ValueError):
    """``tasks.checklist_required`` is on but required items are unticked.

    422-equivalent. Carries the ids of the offending items so the
    API layer can surface "items 2, 5 must be checked" instead of
    a generic failure.
    """

    def __init__(self, unchecked_ids: Sequence[str]) -> None:
        super().__init__(f"required checklist items unchecked: {list(unchecked_ids)!r}")
        self.unchecked_ids = tuple(unchecked_ids)


class SkipNotPermitted(PermissionError):
    """The workspace's settings cascade disables worker-initiated skip.

    403-equivalent. Owners / managers bypass this check; only
    workers reach it. The API layer's permission pass returns a
    403 so workers learn the workspace is locked down.
    """


class EvidenceContentTypeNotAllowed(ValueError):
    """Server-sniffed ``content_type`` is outside the per-kind allow-list.

    415-equivalent. The §15 "Input validation" allow-lists are pinned
    per kind: photos accept the common phone-camera image set, voice
    notes accept the browser's MediaRecorder output set, and gps
    accepts ``application/json`` only. Anything else is a client bug
    or a malicious upload (an executable masquerading as media).

    The :attr:`content_type` carried on the exception is the
    **sniffed** type — the IANA media type the bytes themselves
    describe, as returned by :class:`MimeSniffer`. The header the
    multipart-form part advertised is informational; the rejection
    envelope MUST surface the sniff so an operator inspecting the
    audit row sees the actual shape (``application/x-msdownload``
    for a PE smuggled as ``image/png``) rather than the lie. A
    ``None`` value carries the "sniffer could not classify" verdict;
    spec §15 is explicit that the caller MUST reject in that case
    rather than fall back to the header.
    """

    def __init__(self, *, kind: str, content_type: str | None) -> None:
        super().__init__(
            f"content_type {content_type!r} is not allowed for evidence kind {kind!r}"
        )
        self.kind = kind
        self.content_type = content_type


class EvidenceTooLarge(ValueError):
    """Payload size exceeds the per-kind cap.

    413-equivalent. Caps are pinned per kind so a legitimate phone
    photo (10 MB), a short voice memo (25 MB) and a tiny GPS coordinate
    (4 KiB) each surface a clear rejection rather than collapsing to a
    generic body-too-large error.
    """

    def __init__(self, *, kind: str, size_bytes: int, cap_bytes: int) -> None:
        super().__init__(
            f"evidence kind {kind!r} payload of {size_bytes} bytes exceeds "
            f"the {cap_bytes}-byte cap"
        )
        self.kind = kind
        self.size_bytes = size_bytes
        self.cap_bytes = cap_bytes


class EvidenceGpsPayloadInvalid(ValueError):
    """The ``kind='gps'`` payload is not a valid coordinate JSON document.

    422-equivalent. GPS evidence rides through the same content-
    addressed pipeline as photo / voice but the payload format is
    constrained: a JSON object with numeric ``lat`` (-90..90), numeric
    ``lon`` (-180..180), optional non-negative numeric ``accuracy_m``
    (metres), and an optional ISO-8601 ``captured_at``. The narrow
    contract keeps the stored bytes auditable and prevents abuse of
    the gps kind as a generic JSON drop box.
    """


# ---------------------------------------------------------------------------
# Transition validator
# ---------------------------------------------------------------------------


_TERMINAL: frozenset[TaskStateName] = frozenset({"done", "skipped", "cancelled"})
"""§06 "State machine": states no transition can leave (except the
narrow ``overdue`` revert, which is not an edge **out** of a terminal
— ``overdue`` itself is a soft state that never terminates)."""


# Explicit edge-set encoding §06 "State machine". Keyed by the source
# state; each value is the set of legal targets from there. Kept as
# a literal map rather than a graph library call so the rule survives
# re-reading the spec side-by-side with the code.
#
# Key type is plain :class:`str` (not :data:`TaskStateName`) so
# ``_assert_transition`` can look up a raw DB column value without a
# cast; unknown keys collapse to ``None`` in ``.get()`` and raise
# :class:`InvalidStateTransition` cleanly.
_ALLOWED_EDGES: dict[str, frozenset[TaskStateName]] = {
    "scheduled": frozenset({"pending", "in_progress", "done", "skipped", "cancelled"}),
    "pending": frozenset({"in_progress", "done", "skipped", "cancelled"}),
    "in_progress": frozenset({"done", "skipped", "cancelled"}),
    # Terminal states — no outgoing transitions. Two writers racing
    # through ``complete()`` are handled by the concurrent-completion
    # path (second writer wins on field updates, audit records the
    # supersession), not by an allowed ``done → done`` edge.
    "done": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
    # §06: ``overdue`` is a soft state; the only valid exits are
    # manual state changes back to ``pending`` / ``in_progress`` or
    # forward to ``done`` / ``skipped`` / ``cancelled``. The spec's
    # bullet: "on any manual state change, ``state`` reverts to the
    # chosen value and ``overdue_since`` is cleared".
    "overdue": frozenset({"pending", "in_progress", "done", "skipped", "cancelled"}),
}


def _assert_transition(current: str, target: TaskStateName) -> None:
    """Reject any illegal edge per §06.

    The first argument is typed as plain :class:`str` because it is
    read straight from the DB column (where a future widening could
    introduce a value the Python ``Literal`` does not know about);
    the second is a :class:`TaskStateName` because callers inside
    this module always pass a typed constant.
    """
    allowed = _ALLOWED_EDGES.get(current)
    if allowed is None or target not in allowed:
        raise InvalidStateTransition(current, target)


# ---------------------------------------------------------------------------
# Default hook implementations
# ---------------------------------------------------------------------------


def _default_evidence_policy(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> EvidencePolicy:
    """Read ``Occurrence.photo_evidence`` directly (narrow default).

    The full cascade (§06 "Evidence policy inheritance") spans five
    layers with ``forbid`` absolute. Until cd-settings-cascade lands
    we can still honour the narrow task-scope rule because it is
    stored on the row itself. Values map: ``disabled → forbid``,
    ``required → require``, ``optional → optional``.
    """
    _ = session, ctx
    raw = task.photo_evidence
    if raw == "disabled":
        return "forbid"
    if raw == "required":
        return "require"
    # Any unknown value collapses to ``optional`` — the forgiving
    # default — rather than raising mid-completion. The CHECK
    # constraint on the column rules out new values in practice.
    return "optional"


def _default_checklist_required(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> bool:
    """Default to ``True`` — the safer answer.

    If the template seeded required items, the worker must tick them;
    if no required items exist the gate is vacuous. A ``False``
    override (``tasks.checklist_required = false``) is the opt-out.
    """
    _ = session, ctx, task
    return True


def _default_skip_allowed(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> bool:
    """Default to permissive.

    Owners / managers bypass this resolver entirely (see
    :func:`skip`); only workers read it. Until cd-settings-cascade
    lands the workspace-level policy defaults to "allow with
    reason" (§06 "Skipping and cancellation").
    """
    _ = session, ctx, task
    return True


def _default_inventory_apply(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> None:
    """Write one :class:`Movement` row per SKU in the consumption map.

    Reads :attr:`Occurrence.inventory_consumption_json` — a
    ``{sku: qty}`` mapping copied down from the template — and
    writes a ``reason='consume'`` ledger entry with a negative
    ``delta`` for each pair. SKUs that don't resolve to an
    :class:`Item` in the workspace are skipped (the v1 contract:
    unknown SKUs on a template are data hygiene problems surfaced
    by CRUD, not a completion-time hard error).

    Quantities land as :class:`Decimal` so fractional units
    (``0.25`` kg) survive round-trip on both SQLite (TEXT) and
    PostgreSQL (NUMERIC 18, 4) — the column type
    (:attr:`Movement.delta`) expects ``Decimal`` and a stray
    ``float`` would pin the precision to the backing storage.
    """
    payload = task.inventory_consumption_json or {}
    if not payload:
        return
    # Resolve SKUs → item ids in one query so we don't re-visit the
    # DB per SKU on a template that ships five items. ``sku`` is the
    # map key; ``Item.id`` is what the movement row points at.
    skus = [str(k) for k in payload]
    items = session.scalars(
        select(Item).where(
            Item.workspace_id == ctx.workspace_id,
            Item.sku.in_(skus),
        )
    ).all()
    sku_to_id = {row.sku: row.id for row in items}

    now = task.completed_at or task.created_at
    for sku, qty in payload.items():
        item_id = sku_to_id.get(str(sku))
        if item_id is None:
            # Unknown SKU — silently skip; the CRUD service validates
            # template shape at write time, so this is a stale template
            # referencing an archived item rather than a user error.
            continue
        try:
            delta = Decimal(str(qty))
        except (ArithmeticError, ValueError):
            # Non-numeric value in the consumption map — skip for the
            # same reason as unknown-sku above. Data hygiene is a CRUD
            # concern, not a completion-time hard error.
            continue
        session.add(
            Movement(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                item_id=item_id,
                delta=-abs(delta),
                reason="consume",
                occurrence_id=task.id,
                note_md=None,
                created_at=now,
                created_by=ctx.actor_id,
            )
        )


def _default_asset_action(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> None:
    """No-op: the ``asset_action`` table is not in the schema yet.

    Filed as a spec-drift follow-up alongside cd-7am7 — the real
    body stamps ``asset_action.last_performed_at`` and
    ``asset_action.last_performed_task_id`` per §21.
    """
    _ = session, ctx, task
    return None


def _default_tombstone(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> None:
    """No-op: the ``task_completion`` table is not in the schema yet.

    Filed as a spec-drift follow-up alongside cd-7am7. The real
    body writes a :class:`task_completion` row per §06 "Completing
    a task" #5 so reports can reconstruct history even if the task
    is later edited.
    """
    _ = session, ctx, task
    return None


# ---------------------------------------------------------------------------
# Loading + permission helpers
# ---------------------------------------------------------------------------


def _load_task(session: Session, ctx: WorkspaceContext, task_id: str) -> Occurrence:
    row = session.scalar(
        select(Occurrence).where(
            Occurrence.id == task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise TaskNotFound(f"task {task_id!r} not visible in workspace")
    return row


def _is_manager_or_owner(ctx: WorkspaceContext) -> bool:
    """Manager or owner — the two roles that can cancel / bypass skip.

    ``actor_grant_role == "manager"`` covers managers directly;
    ``actor_was_owner_member`` covers owners (who are implicitly a
    superset of manager per §05). Workers land on ``worker``;
    clients and guests land on their own roles and are never
    permitted.
    """
    return ctx.actor_grant_role == "manager" or ctx.actor_was_owner_member


def _can_drive_completion(ctx: WorkspaceContext, task: Occurrence) -> bool:
    """Gate for :func:`start` and :func:`complete`.

    Per §06 "State transitions and who may trigger them": workers
    drive **their own** assigned tasks; owners / managers drive any
    task. Clients / guests are never permitted.
    """
    if _is_manager_or_owner(ctx):
        return True
    return ctx.actor_grant_role == "worker" and task.assignee_user_id == ctx.actor_id


# ---------------------------------------------------------------------------
# Checklist + evidence gate helpers
# ---------------------------------------------------------------------------


def _unchecked_required_items(session: Session, task: Occurrence) -> tuple[str, ...]:
    """Return the ids of required :class:`ChecklistItem` rows still unchecked.

    The v1 schema uses :attr:`ChecklistItem.requires_photo` as the
    "required" marker — the spec's ``required`` field in
    ``checklist_template_json`` seeds this column. ``checked`` is
    the tick marker. A row with ``requires_photo=True`` and
    ``checked=False`` is an incomplete required item.

    Returns an empty tuple when the gate is clear — callers use the
    truthiness of the result for their 422 branch.
    """
    rows = session.scalars(
        select(ChecklistItem).where(
            ChecklistItem.occurrence_id == task.id,
            ChecklistItem.requires_photo.is_(True),
            ChecklistItem.checked.is_(False),
        )
    ).all()
    return tuple(r.id for r in rows)


def _has_photo_evidence(session: Session, task: Occurrence) -> bool:
    """True when at least one ``kind='photo'`` evidence row is linked."""
    row = session.scalar(
        select(Evidence.id)
        .where(
            Evidence.occurrence_id == task.id,
            Evidence.kind == "photo",
        )
        .limit(1)
    )
    return row is not None


def _write_note_evidence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task: Occurrence,
    note_md: str,
    clock: Clock,
) -> None:
    """Persist ``note_md`` as an :class:`Evidence` row of kind ``note``.

    The spec places ``completion_note_md`` on the task row; until
    the column lands (filed as a spec-drift follow-up) this service
    writes the note as an :class:`Evidence` row so the information
    survives and surfaces in reports. The kind/note pairing is
    enforced by the Evidence table's CHECK constraint.
    """
    session.add(
        Evidence(
            id=new_ulid(),
            workspace_id=ctx.workspace_id,
            occurrence_id=task.id,
            kind="note",
            blob_hash=None,
            note_md=note_md,
            created_at=clock.now(),
            created_by_user_id=ctx.actor_id,
        )
    )


# ---------------------------------------------------------------------------
# Audit + event helpers
# ---------------------------------------------------------------------------


def _state_view(
    task: Occurrence, *, state: TaskStateName, reason: str | None = None
) -> TaskState:
    return TaskState(
        task_id=task.id,
        state=state,
        completed_at=task.completed_at,
        completed_by_user_id=task.completed_by_user_id,
        reason=reason,
    )


def _audit(
    session: Session,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task: Occurrence,
    action: str,
    diff: dict[str, Any],
) -> None:
    write_audit(
        session,
        ctx,
        entity_kind="task",
        entity_id=task.id,
        action=action,
        diff=diff,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def start(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> TaskState:
    """Drive ``pending → in_progress``.

    The spec lets workers go straight to ``done`` (see §06 "State
    machine" — "``pending`` → ``in_progress`` is optional"); this
    entry point is the explicit "I am working on this now"
    signal the worker PWA sends when the user taps into the task
    detail page. Audit only — no event is emitted (the assignment
    service already fired ``task.assigned`` at creation-time and
    nothing downstream of ``start`` invalidates a workspace cache).
    """
    _ = event_bus  # reserved for a future ``task.started`` fanout.
    resolved_clock = clock if clock is not None else SystemClock()

    task = _load_task(session, ctx, task_id)
    if not _can_drive_completion(ctx, task):
        raise PermissionDenied(
            f"actor {ctx.actor_id!r} cannot drive start on task {task_id!r}"
        )
    previous = task.state
    _assert_transition(previous, "in_progress")

    task.state = "in_progress"
    # §06 "State machine": "On any manual state change ...
    # overdue_since is cleared." A worker pushing ``start`` after the
    # sweeper had flipped the row is the canonical path back from
    # ``overdue → in_progress``; clear the marker so the next sweeper
    # tick does not see the row.
    task.overdue_since = None
    session.flush()

    _audit(
        session,
        ctx,
        resolved_clock,
        task=task,
        action="task.start",
        diff={"before": {"state": previous}, "after": {"state": "in_progress"}},
    )
    return _state_view(task, state="in_progress")


def complete(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    note_md: str | None = None,
    photo_evidence_ids: Sequence[str] = (),
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    evidence_policy: EvidencePolicyResolver | None = None,
    checklist_required: ChecklistRequiredResolver | None = None,
    inventory_apply: InventoryApplyHook | None = None,
    asset_action: AssetActionHook | None = None,
    tombstone: TombstoneHook | None = None,
) -> TaskState:
    """Drive ``pending | in_progress → done``.

    Flow:

    1. **Load + permission.** Caller must be the assignee or a
       manager / owner.
    2. **Evidence gate.** Resolve the photo policy. ``forbid`` +
       non-empty ``photo_evidence_ids`` → :class:`PhotoForbidden`.
       ``require`` + no photo (neither in the payload nor already
       linked via :class:`Evidence`) → :class:`EvidenceRequired`.
    3. **Checklist gate.** When :data:`ChecklistRequiredResolver`
       returns ``True`` and any required :class:`ChecklistItem` is
       still unchecked → :class:`RequiredChecklistIncomplete`.
    4. **Write the completion.** Set ``state='done'``,
       ``completed_at``, ``completed_by_user_id``. When ``note_md``
       is supplied, persist as :class:`Evidence` of kind ``note``
       (bridge until the ``completion_note_md`` column lands — see
       the module docstring and the spec-drift follow-up).
    5. **Side-effects.** Run :data:`InventoryApplyHook`,
       :data:`AssetActionHook`, :data:`TombstoneHook` in order.
    6. **Audit + event.** Emit :class:`TaskCompleted`; write the
       ``task.complete`` audit row. When the pre-state was already
       ``done`` (concurrent completion), also write
       ``task.complete_superseded`` carrying the displaced
       ``completed_by_user_id`` + ``completed_at`` in its diff.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    resolve_evidence = (
        evidence_policy if evidence_policy is not None else _default_evidence_policy
    )
    resolve_checklist = (
        checklist_required
        if checklist_required is not None
        else _default_checklist_required
    )
    apply_inventory = (
        inventory_apply if inventory_apply is not None else _default_inventory_apply
    )
    stamp_asset_action = (
        asset_action if asset_action is not None else _default_asset_action
    )
    write_tombstone = tombstone if tombstone is not None else _default_tombstone

    task = _load_task(session, ctx, task_id)
    if not _can_drive_completion(ctx, task):
        raise PermissionDenied(
            f"actor {ctx.actor_id!r} cannot drive complete on task {task_id!r}"
        )

    # Capture pre-state for the concurrent-completion branch. The
    # two writers racing through ``complete()`` both land; the later
    # one overwrites the fields and emits an extra audit row.
    was_already_done = task.state == "done"
    displaced_completed_at = task.completed_at
    displaced_completed_by = task.completed_by_user_id
    previous_state = task.state

    # Only run the transition validator for the non-racing path —
    # ``done → done`` is not in the allowed-edge map, and the second
    # writer needs to land anyway per §06 "Concurrent completion".
    if not was_already_done:
        _assert_transition(previous_state, "done")

    # --- Gate 1: evidence policy. ---------------------------------
    policy = resolve_evidence(session, ctx, task)
    if policy == "forbid" and photo_evidence_ids:
        raise PhotoForbidden(
            f"task {task_id!r} has evidence policy 'forbid' but "
            f"{len(photo_evidence_ids)} photo evidence id(s) supplied"
        )
    if (
        policy == "require"
        and not photo_evidence_ids
        and not _has_photo_evidence(session, task)
    ):
        raise EvidenceRequired(
            f"task {task_id!r} requires photo evidence; none supplied"
        )

    # --- Gate 2: required checklist items. ------------------------
    if resolve_checklist(session, ctx, task):
        unchecked = _unchecked_required_items(session, task)
        if unchecked:
            raise RequiredChecklistIncomplete(unchecked)

    # --- Write the completion row. --------------------------------
    now = resolved_clock.now()
    task.state = "done"
    task.completed_at = now
    task.completed_by_user_id = ctx.actor_id
    # §06 "State machine": clear ``overdue_since`` on any manual
    # transition. The completion path catches the most common
    # "overdue → done" exit; the column reverts to NULL alongside
    # the state flip so reports never see a "done" row that still
    # claims to be overdue.
    task.overdue_since = None
    session.flush()

    if note_md is not None and note_md.strip():
        _write_note_evidence(
            session, ctx, task=task, note_md=note_md, clock=resolved_clock
        )

    # --- Side-effects. --------------------------------------------
    apply_inventory(session, ctx, task)
    stamp_asset_action(session, ctx, task)
    write_tombstone(session, ctx, task)

    # --- Audit + event. -------------------------------------------
    diff: dict[str, Any] = {
        "before": {
            "state": previous_state,
            "completed_at": displaced_completed_at.isoformat()
            if displaced_completed_at
            else None,
            "completed_by_user_id": displaced_completed_by,
        },
        "after": {
            "state": "done",
            "completed_at": now.isoformat(),
            "completed_by_user_id": ctx.actor_id,
        },
    }
    _audit(session, ctx, resolved_clock, task=task, action="task.complete", diff=diff)

    if was_already_done:
        # The displaced completion row's audit footprint — carries
        # **only** the displaced fields so reporting can reconstruct
        # the sequence without confusing it with the superseding
        # ``task.complete`` row above.
        _audit(
            session,
            ctx,
            resolved_clock,
            task=task,
            action="task.complete_superseded",
            diff={
                "displaced": {
                    "completed_at": displaced_completed_at.isoformat()
                    if displaced_completed_at
                    else None,
                    "completed_by_user_id": displaced_completed_by,
                },
                "superseded_by": {
                    "completed_at": now.isoformat(),
                    "completed_by_user_id": ctx.actor_id,
                },
            },
        )

    resolved_bus.publish(
        TaskCompleted(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            task_id=task.id,
            completed_by=ctx.actor_id,
        )
    )
    return _state_view(task, state="done")


def skip(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    reason: str,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    skip_allowed: SkipAllowedResolver | None = None,
) -> TaskState:
    """Drive a non-terminal task to ``skipped``.

    Workers may skip their own assigned tasks **only** when the
    settings cascade resolves ``tasks.allow_skip_with_reason = true``
    (:data:`SkipAllowedResolver` returns ``True``). Owners / managers
    bypass the resolver; they can always skip.

    ``reason`` is stored in :attr:`Occurrence.cancellation_reason`
    (the only free-text column the v1 schema carries — the dedicated
    ``skipped_reason`` column is part of the §06 spec-drift
    follow-up). The column name is misleading in this bridge form;
    the audit trail makes the distinction explicit via the
    ``task.skip`` action.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    resolve_allowed = (
        skip_allowed if skip_allowed is not None else _default_skip_allowed
    )

    task = _load_task(session, ctx, task_id)

    # Permission: owners / managers always allowed; workers gated
    # on the settings cascade + own-task rule. Clients / guests never.
    if _is_manager_or_owner(ctx):
        pass
    elif ctx.actor_grant_role == "worker":
        if task.assignee_user_id != ctx.actor_id:
            raise PermissionDenied(
                f"worker {ctx.actor_id!r} cannot skip task they are not assigned to"
            )
        if not resolve_allowed(session, ctx, task):
            raise SkipNotPermitted(
                "workspace disables worker-initiated skip "
                "(setting 'tasks.allow_skip_with_reason' resolves to false)"
            )
    else:
        raise PermissionDenied(f"actor role {ctx.actor_grant_role!r} cannot skip tasks")

    previous = task.state
    _assert_transition(previous, "skipped")

    task.state = "skipped"
    task.cancellation_reason = reason
    # §06 "State machine": clear ``overdue_since`` on any manual
    # transition. A worker / manager skipping an overdue task moves
    # it out of the sweeper's purview; the marker reverts to NULL
    # alongside the state flip.
    task.overdue_since = None
    session.flush()

    _audit(
        session,
        ctx,
        resolved_clock,
        task=task,
        action="task.skip",
        diff={
            "before": {"state": previous},
            "after": {"state": "skipped", "reason": reason},
        },
    )
    resolved_bus.publish(
        TaskSkipped(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            task_id=task.id,
            skipped_by=ctx.actor_id,
            reason=reason,
        )
    )
    return _state_view(task, state="skipped", reason=reason)


def cancel(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    reason: str,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> TaskState:
    """Drive a non-terminal task to ``cancelled``.

    Owners / managers only (§06 "Skipping and cancellation":
    ``cancel`` is an owner or manager action only). Workers /
    clients / guests are rejected with :class:`PermissionDenied`.

    ``reason`` is stored in :attr:`Occurrence.cancellation_reason`
    and emitted on :class:`TaskCancelled`. The event's validator
    enforces the identifier-shaped code contract — free-text
    managerial notes are rejected at publish time.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    task = _load_task(session, ctx, task_id)
    if not _is_manager_or_owner(ctx):
        raise PermissionDenied(
            f"actor {ctx.actor_id!r} (role {ctx.actor_grant_role!r}) "
            "cannot cancel tasks; owners / managers only"
        )

    previous = task.state
    _assert_transition(previous, "cancelled")

    task.state = "cancelled"
    task.cancellation_reason = reason
    # §06 "State machine": clear ``overdue_since`` on any manual
    # transition. An owner / manager cancelling an overdue task moves
    # it out of the sweeper's purview; the marker reverts to NULL
    # alongside the state flip.
    task.overdue_since = None
    session.flush()

    _audit(
        session,
        ctx,
        resolved_clock,
        task=task,
        action="task.cancel",
        diff={
            "before": {"state": previous},
            "after": {"state": "cancelled", "reason": reason},
        },
    )
    resolved_bus.publish(
        TaskCancelled(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            task_id=task.id,
            cancelled_by=ctx.actor_id,
            reason=reason,
        )
    )
    return _state_view(task, state="cancelled", reason=reason)


def revert_overdue(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    target_state: Literal["pending", "in_progress"],
    clock: Clock | None = None,
) -> TaskState:
    """Flip a soft-``overdue`` task back to ``pending`` or ``in_progress``.

    Per §06 "State machine": ``overdue`` is a soft state, set by
    the sweeper worker (:mod:`app.worker.tasks.overdue`) when
    ``ends_at + grace`` is past. On any manual state change the row
    reverts to the chosen value and ``overdue_since`` is cleared
    so the next sweeper tick will not see the row in its result set.

    Owners / managers / workers (on their own task) may call; the
    same permission rule as :func:`complete` / :func:`start`.
    """
    resolved_clock = clock if clock is not None else SystemClock()

    task = _load_task(session, ctx, task_id)
    if not _can_drive_completion(ctx, task):
        raise PermissionDenied(
            f"actor {ctx.actor_id!r} cannot revert overdue on task {task_id!r}"
        )

    previous = task.state
    previous_overdue_since = task.overdue_since
    _assert_transition(previous, target_state)

    task.state = target_state
    task.overdue_since = None
    session.flush()

    _audit(
        session,
        ctx,
        resolved_clock,
        task=task,
        action="task.revert_overdue",
        diff={
            "before": {
                "state": previous,
                "overdue_since": previous_overdue_since.isoformat()
                if previous_overdue_since is not None
                else None,
            },
            "after": {"state": target_state, "overdue_since": None},
        },
    )
    return _state_view(task, state=target_state)


# ---------------------------------------------------------------------------
# Evidence reads + ad-hoc writes (HTTP-layer seams for cd-sn26)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """Immutable read projection of one :class:`Evidence` row.

    Surfaced by :func:`list_evidence` and :func:`add_note_evidence` so
    the HTTP router can reflect a row without reaching into the ORM
    class. Carries every column the §06 "Evidence" section mentions;
    ``blob_hash`` is ``NULL`` for ``kind='note'`` rows (the body lives
    in ``note_md``) and vice-versa for ``photo`` / ``voice`` / ``gps``.
    """

    id: str
    workspace_id: str
    occurrence_id: str
    kind: Literal["photo", "note", "voice", "gps"]
    blob_hash: str | None
    note_md: str | None
    created_at: datetime
    created_by_user_id: str | None


def _narrow_evidence_kind(value: str) -> Literal["photo", "note", "voice", "gps"]:
    """Narrow a loaded DB string to the :class:`EvidenceView.kind` literal.

    Parity with :func:`app.domain.tasks.templates._narrow_priority` and
    friends: a CHECK constraint on the DB column rules out new values
    in practice, but mypy's ``Literal`` narrowing requires a per-value
    return so the projection escapes without a ``cast`` or
    ``# type: ignore``.
    """
    if value == "photo":
        return "photo"
    if value == "note":
        return "note"
    if value == "voice":
        return "voice"
    if value == "gps":
        return "gps"
    raise ValueError(f"unknown evidence.kind {value!r} on loaded row")


def _evidence_row_to_view(row: Evidence) -> EvidenceView:
    """Project a loaded :class:`Evidence` row into an :class:`EvidenceView`."""
    return EvidenceView(
        id=row.id,
        workspace_id=row.workspace_id,
        occurrence_id=row.occurrence_id,
        kind=_narrow_evidence_kind(row.kind),
        blob_hash=row.blob_hash,
        note_md=row.note_md,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


def list_evidence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
) -> tuple[EvidenceView, ...]:
    """Return every :class:`Evidence` row anchored to ``task_id``.

    The tenant filter is applied by the ORM; we re-assert
    ``workspace_id`` defensively so a mis-wired caller cannot leak
    cross-tenant rows. :class:`TaskNotFound` fires when the parent
    occurrence is unknown — the HTTP caller wants a 404 on the task,
    not an empty list on a missing id.
    """
    task = _load_task(session, ctx, task_id)
    rows = session.scalars(
        select(Evidence)
        .where(
            Evidence.workspace_id == ctx.workspace_id,
            Evidence.occurrence_id == task.id,
        )
        .order_by(Evidence.created_at.asc(), Evidence.id.asc())
    ).all()
    return tuple(_evidence_row_to_view(row) for row in rows)


def add_note_evidence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
    note_md: str,
    clock: Clock | None = None,
) -> EvidenceView:
    """Persist ``note_md`` as a free-standing ``kind='note'`` evidence row.

    Mirrors the inline :func:`_write_note_evidence` helper that runs
    at completion time, exposed as a public service call so the
    ``POST /tasks/{id}/evidence`` route can accept a note without
    going through the full completion flow. The pipeline for
    ``kind='photo'`` / ``voice`` / ``gps`` uploads goes through the
    asset pipeline (cd-assets); that seam is not yet wired end-to-end
    for tasks, and the router documents the gap (see cd-sn26 for the
    follow-up).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    task = _load_task(session, ctx, task_id)
    trimmed = note_md.strip()
    if not trimmed:
        raise ValueError("note_md must be non-empty for kind='note' evidence")
    row = Evidence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        occurrence_id=task.id,
        kind="note",
        blob_hash=None,
        note_md=trimmed,
        created_at=resolved_clock.now(),
        created_by_user_id=ctx.actor_id,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        ctx,
        resolved_clock,
        task=task,
        action="task.evidence.note.add",
        diff={"after": {"evidence_id": row.id, "note_md": trimmed}},
    )
    return _evidence_row_to_view(row)


# ---------------------------------------------------------------------------
# File-bearing evidence (cd-jl0g — photo / voice / gps)
# ---------------------------------------------------------------------------


# The §02 / §06 evidence taxonomy minus ``note`` — the file-bearing
# kinds that route through :class:`Storage`. ``FileEvidenceKind`` is
# the public alias so callers can type-narrow without re-declaring.
FileEvidenceKind = Literal["photo", "voice", "gps"]


# Per-kind MIME allow-list. Pinned per spec §15 "Input validation":
#
# * ``photo`` — the common phone-camera + screenshot set. No SVG
#   (script-execution vector), no GIF (no legitimate task evidence
#   is animated), no generic ``application/octet-stream``.
# * ``voice`` — the browser MediaRecorder defaults plus the common
#   container types a worker recording from a native app might
#   produce. WAV is doubly-spelled because Safari emits
#   ``audio/x-wav`` while every other engine emits ``audio/wav``.
#   ``video/webm`` and ``video/mp4`` are listed alongside the
#   ``audio/*`` siblings because magic-byte sniffers (the §15
#   :class:`MimeSniffer` seam) match the container's EBML / ftyp
#   signature and cannot introspect the codec — a Chrome / Firefox
#   MediaRecorder Opus-in-WebM voice memo sniffs as ``video/webm``,
#   not ``audio/webm``, even though the stream is audio-only. Same
#   reasoning for ``video/mp4`` (a generic ``ftyp/isom`` box).
#   Excluding the container labels would reject every browser-
#   recorded WebM / MP4 voice upload.
# * ``gps`` — ``application/json`` only. The payload is a small
#   coordinate document, not a binary file; pinning JSON keeps the
#   contract auditable and the parser narrow.
_PHOTO_ALLOWED_MIME: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic"}
)
_VOICE_ALLOWED_MIME: frozenset[str] = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/mpeg",  # MP3
        "audio/mp4",
        "audio/aac",
        "audio/wav",
        "audio/x-wav",
        # Container labels the magic-byte sniffer returns for an
        # audio-only payload (cd-ba5c). See module-comment above.
        "video/webm",
        "video/mp4",
    }
)
_GPS_ALLOWED_MIME: frozenset[str] = frozenset({"application/json"})

_MIME_ALLOWLIST_BY_KIND: dict[FileEvidenceKind, frozenset[str]] = {
    "photo": _PHOTO_ALLOWED_MIME,
    "voice": _VOICE_ALLOWED_MIME,
    "gps": _GPS_ALLOWED_MIME,
}


# Per-kind size cap. Pinned per spec §15 "Input validation": "default
# 10 MB images, 25 MB PDFs". Voice uses the PDF tier (a few minutes of
# voice memo at modest bitrate); GPS is a small coordinate JSON so
# 4 KiB is plenty (a maximalist payload with timestamps + accuracy +
# heading is well under 1 KiB).
_PHOTO_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MiB
_VOICE_MAX_BYTES: int = 25 * 1024 * 1024  # 25 MiB
_GPS_MAX_BYTES: int = 4 * 1024  # 4 KiB

_MAX_BYTES_BY_KIND: dict[FileEvidenceKind, int] = {
    "photo": _PHOTO_MAX_BYTES,
    "voice": _VOICE_MAX_BYTES,
    "gps": _GPS_MAX_BYTES,
}


def _validate_gps_payload(payload: bytes) -> dict[str, Any]:
    """Parse + validate a ``kind='gps'`` JSON document.

    Returns the parsed coordinate dict. Raises
    :class:`EvidenceGpsPayloadInvalid` for any structural or numeric
    problem — the bytes never reach the storage layer when the format
    is wrong, so an attacker can't seed the blob store with arbitrary
    JSON under the gps kind.
    """
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceGpsPayloadInvalid(
            f"gps payload is not valid UTF-8 JSON: {exc!s}"
        ) from exc
    if not isinstance(document, dict):
        raise EvidenceGpsPayloadInvalid(
            f"gps payload must be a JSON object; got {type(document).__name__}"
        )
    if "lat" not in document or "lon" not in document:
        raise EvidenceGpsPayloadInvalid(
            "gps payload must carry 'lat' and 'lon' numeric fields"
        )
    lat_raw = document["lat"]
    lon_raw = document["lon"]
    # Reject bool first — ``isinstance(True, int)`` is True in Python,
    # which would silently accept ``{"lat": true, "lon": false}``.
    if isinstance(lat_raw, bool) or not isinstance(lat_raw, int | float):
        raise EvidenceGpsPayloadInvalid(
            f"gps payload 'lat' must be numeric; got {type(lat_raw).__name__}"
        )
    if isinstance(lon_raw, bool) or not isinstance(lon_raw, int | float):
        raise EvidenceGpsPayloadInvalid(
            f"gps payload 'lon' must be numeric; got {type(lon_raw).__name__}"
        )
    lat = float(lat_raw)
    lon = float(lon_raw)
    if not -90.0 <= lat <= 90.0:
        raise EvidenceGpsPayloadInvalid(f"gps payload 'lat'={lat!r} out of range")
    if not -180.0 <= lon <= 180.0:
        raise EvidenceGpsPayloadInvalid(f"gps payload 'lon'={lon!r} out of range")
    accuracy = document.get("accuracy_m")
    if accuracy is not None:
        if isinstance(accuracy, bool) or not isinstance(accuracy, int | float):
            raise EvidenceGpsPayloadInvalid(
                f"gps payload 'accuracy_m' must be numeric; "
                f"got {type(accuracy).__name__}"
            )
        if float(accuracy) < 0:
            raise EvidenceGpsPayloadInvalid(
                f"gps payload 'accuracy_m'={accuracy!r} must be non-negative"
            )
    return document


def add_file_evidence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
    kind: FileEvidenceKind,
    payload: bytes,
    content_type: str,
    storage: Storage,
    mime_sniffer: MimeSniffer | None = None,
    clock: Clock | None = None,
) -> EvidenceView:
    """Persist a file-bearing evidence row (photo / voice / gps).

    Drives the asset pipeline end-to-end:

    1. Loads the task through the ``ctx.workspace_id``-scoped seam
       (mirrors :func:`add_note_evidence` for tenant isolation).
    2. Validates ``kind`` is in :data:`FileEvidenceKind`.
    3. Validates ``len(payload)`` against the per-kind cap.
    4. **Sniffs** the payload via the injectable :class:`MimeSniffer`
       and validates the **sniffed** type against the per-kind
       allow-list (§15 "Input validation": "MIME sniffed server-side;
       we trust the sniff, not the header"). The multipart-declared
       ``content_type`` is informational — an attacker who claims
       ``image/png`` for a Windows PE executable is rejected here
       because the bytes sniff to ``application/x-msdownload``.
       A ``None`` sniff verdict (bytes the sniffer can't classify) is
       a hard reject — falling back to the declared header is exactly
       the vector the seam closes.
    5. For ``kind='gps'``, parses + validates the JSON coordinate
       document so the stored bytes carry a known shape.
    6. SHA-256 the bytes, hand them to :meth:`Storage.put` with the
       **sniffed** type (idempotent; same hash → same blob). The
       declared header never reaches storage — only the sniff does,
       so the persisted ``content_type`` matches what's actually in
       the blob.
    7. Inserts the :class:`Evidence` row with the resolved
       ``blob_hash``, flushes, audits ``task.evidence.<kind>.add``.

    The audit row carries the **sniffed** ``content_type``, the
    declared header (for forensic comparison), and ``size_bytes`` so
    a later walk can reconstruct what landed without re-reading the
    blob.

    ``mime_sniffer`` defaults to :class:`FiletypeMimeSniffer` (cd-ba5c)
    — the pure-Python magic-byte sniff backed by ``filetype`` + a
    narrow JSON structural check for the GPS branch. Tests override
    with a fake to pin the verdict.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_sniffer = (
        mime_sniffer if mime_sniffer is not None else FiletypeMimeSniffer()
    )

    if kind not in _MIME_ALLOWLIST_BY_KIND:
        raise ValueError(
            f"kind {kind!r} is not a file-bearing evidence kind; expected one of "
            f"{sorted(_MIME_ALLOWLIST_BY_KIND)!r}"
        )

    task = _load_task(session, ctx, task_id)

    cap = _MAX_BYTES_BY_KIND[kind]
    size_bytes = len(payload)
    if size_bytes == 0:
        # Empty file is always a client bug — a zero-byte photo / voice
        # memo / GPS payload carries no information. Same envelope as
        # :func:`add_note_evidence`'s empty-note rejection.
        raise ValueError(f"kind={kind!r} evidence payload must not be empty")
    if size_bytes > cap:
        raise EvidenceTooLarge(kind=kind, size_bytes=size_bytes, cap_bytes=cap)

    # Spec §15: sniff the bytes, validate the sniff (not the header)
    # against the per-kind allow-list. The declared ``content_type``
    # is passed as a hint only so the JSON structural-check fallback
    # is gated on a JSON-shaped declaration — it is **never** the
    # decision-maker.
    sniffed_type = resolved_sniffer.sniff(payload, hint=content_type)
    allowed = _MIME_ALLOWLIST_BY_KIND[kind]
    if sniffed_type is None or sniffed_type not in allowed:
        # Either the sniffer could not classify the bytes (None) or
        # the sniff disagrees with the allow-list. Either way the
        # upload never lands in storage. The exception carries the
        # sniffed type so the operator inspecting the audit envelope
        # sees the actual shape, not the multipart-form lie.
        raise EvidenceContentTypeNotAllowed(kind=kind, content_type=sniffed_type)

    if kind == "gps":
        # Parse + validate BEFORE storage so a malformed payload never
        # lands in the blob store (and the audit row carries the
        # rejection reason instead of an opaque blob hash).
        _validate_gps_payload(payload)

    # Hash + store. The Storage port is idempotent: the same hash on a
    # repeat upload returns the existing blob's metadata without
    # re-writing. We do NOT short-circuit the audit row on a dedupe —
    # the Evidence row is a per-task pointer, not a per-blob one, so
    # a worker who attaches the same photo to two tasks gets two rows.
    # The persisted ``content_type`` is the sniffed verdict, not the
    # declared header — what the bytes are, not what the client claimed.
    blob_hash = hashlib.sha256(payload).hexdigest()
    storage.put(blob_hash, io.BytesIO(payload), content_type=sniffed_type)

    row = Evidence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        occurrence_id=task.id,
        kind=kind,
        blob_hash=blob_hash,
        note_md=None,
        created_at=resolved_clock.now(),
        created_by_user_id=ctx.actor_id,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        ctx,
        resolved_clock,
        task=task,
        action=f"task.evidence.{kind}.add",
        diff={
            "after": {
                "evidence_id": row.id,
                "blob_hash": blob_hash,
                # Persist the sniffed verdict on the audit row so a
                # later forensic walk knows what bytes actually landed.
                "content_type": sniffed_type,
                # Declared header preserved alongside for the
                # "client claimed X, sniff said Y" forensic case —
                # useful when the two disagree on a non-rejected
                # upload (e.g. ``audio/wav`` declared, ``audio/x-wav``
                # sniffed; both in the allow-list).
                "declared_content_type": content_type,
                "size_bytes": size_bytes,
            }
        },
    )
    return _evidence_row_to_view(row)
