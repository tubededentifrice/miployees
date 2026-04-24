"""Task-scoped comments — the agent inbox (cd-cfe4, §06).

The previous v0 "comments" surface is replaced by a
**workspace-agent-mediated conversation** scoped to each task
occurrence: every note is a row in :class:`~app.adapters.db.tasks.models.Comment`
ordered by ``created_at``, typed ``user | agent | system``, with
``@mentions`` resolving to workspace members and the offline-mention
email fanout consumed by §10 messaging.

## Public surface

* **DTOs** — :class:`CommentCreate` (request body) and
  :class:`CommentView` (read projection).
* **Service functions** — :func:`post_comment`, :func:`list_comments`,
  :func:`edit_comment`, :func:`delete_comment`, :func:`get_comment`.
  Every function takes a :class:`~app.tenancy.WorkspaceContext` as its
  first argument; the ``workspace_id`` is resolved from the context,
  never from the caller's payload (v1 invariant §01).
* **Errors** — :class:`CommentNotFound` / :class:`CommentKindForbidden`
  / :class:`CommentEditWindowExpired` / :class:`CommentNotEditable` /
  :class:`CommentMentionInvalid` / :class:`CommentMentionAmbiguous` /
  :class:`CommentAttachmentInvalid`. Each subclasses the stdlib parent
  the router's error map points at (``LookupError`` → 404,
  ``PermissionError`` → 403, ``ValueError`` → 409 / 422).

## Kind gating

``kind`` narrows at the write boundary, not downstream:

* ``user`` — human author. Gated by the ``tasks.comment`` capability
  (new in cd-cfe4; owners + managers + all_workers by default).
* ``agent`` — the embedded workspace agent speaking in the thread
  (§06 "Task notes are the agent inbox"). Only callable when
  ``ctx.actor_kind == 'agent'`` (the embedded agent token). A
  non-agent caller raises :class:`CommentKindForbidden`. ``agent``
  rows carry the ``llm_call_id`` of the call that produced them so
  reports can reconstruct the prompt / completion pair.
* ``system`` — internal state-change markers emitted by the
  completion / assignment services. Only callable via the explicit
  ``internal_caller=True`` keyword; an external caller raises
  :class:`CommentKindForbidden`. Never editable or deletable.

## Personal-task visibility

The Occurrence model carries an :attr:`Occurrence.is_personal` flag
(§06 "Self-created and personal tasks"). When the flag is set, the
task is visible only to its creator and to workspace owners — the
shift the agent inbox honours at read / write time. The service
enforces the gate here as defence-in-depth; the §15 read layer carries
the same rule for list surfaces.

## @mention resolution

``@<slug>`` patterns in ``body_md`` resolve at write time against
workspace members (users carrying a :class:`UserWorkspace` row for
``ctx.workspace_id``). The slug is the user's ``display_name``
normalised to lowercase alphanumerics + ``-`` / ``_``, capped at 40
chars — a pragmatic v1 that survives until a proper
``User.display_name_slug`` column lands. The textual ``@slug``
survives verbatim in ``body_md``; the resolved user ids ride on the
:attr:`Comment.mentioned_user_ids` column for the §10 fanout.

Mentions of users who are not members raise
:class:`CommentMentionInvalid` (422); a mention that matches nobody
in the workspace is a payload error, not a silent drop. A slug that
matches **more than one** workspace member (the
``display_name`` → slug normalisation collapses two handles)
raises :class:`CommentMentionAmbiguous` (422) — the service
refuses to silently pick one so the §10 fanout cannot deliver to
the wrong user. The ambiguous branch goes away once
:attr:`User.display_name_slug` lands with a per-workspace
uniqueness constraint.

## Transaction boundary

Every mutation writes one :mod:`app.audit` row in the same
transaction, then publishes a
:class:`~app.events.types.TaskCommentAdded` event AFTER the audit
write (so a failed publish still leaves the audit row in the UoW).
The service never calls ``session.commit()``; the caller's
Unit-of-Work owns transaction boundaries.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task notes are the
agent inbox", §"Comments"; ``docs/specs/02-domain-model.md``
§"comment".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.tasks.models import Comment, Evidence, Occurrence
from app.adapters.db.workspace.models import UserWorkspace
from app.audit import write_audit
from app.authz import (
    EmptyPermissionRuleRepository,
    PermissionDenied,
    PermissionRuleRepository,
    require,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import TaskCommentAdded
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "EDIT_WINDOW",
    "CommentAttachmentInvalid",
    "CommentCreate",
    "CommentEditWindowExpired",
    "CommentKind",
    "CommentKindForbidden",
    "CommentMentionAmbiguous",
    "CommentMentionInvalid",
    "CommentNotEditable",
    "CommentNotFound",
    "CommentView",
    "delete_comment",
    "edit_comment",
    "get_comment",
    "list_comments",
    "post_comment",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


CommentKind = Literal["user", "agent", "system"]
"""Typed alias for the three ``Comment.kind`` values (§02 / §06)."""


#: §06 "Task notes are the agent inbox": authors can amend their own
#: messages "within a small grace window". We pin that window at 5
#: minutes here — long enough to fix a typo after re-reading the
#: rendered message, short enough that later edits flow through the
#: moderator path (``tasks.comment_moderate``) with its audit row.
EDIT_WINDOW: Final[timedelta] = timedelta(minutes=5)


#: Cap on ``body_md`` — bounds audit + DB payload without being
#: restrictive in practice. Parity with the sibling shifts and
#: task-template DTOs.
_MAX_BODY_LEN: Final[int] = 20_000


#: Cap on attachment count per comment — bounds the attachment-resolve
#: SELECT to a small, bounded fan-out. The UI today shows at most a
#: half-dozen thumbnails; 20 is well above that.
_MAX_ATTACHMENTS: Final[int] = 20


#: Regex that extracts ``@<slug>`` tokens from ``body_md``. The slug
#: body is lowercased alphanumerics + ``-`` / ``_``, 1-40 chars —
#: parity with the §05 username-slug shape. Anchored behind a
#: non-word boundary so ``email@foo.com`` does NOT match the ``@foo``
#: substring; Python's ``\b`` would misfire on the leading ``@``.
_MENTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?<=^)|(?<=[^\w@]))@([a-z0-9][a-z0-9_-]{0,39})",
)


#: Maximum slug length the ``_normalise_slug`` helper produces. Matches
#: the regex cap above so a normalised ``display_name`` that exceeds
#: the cap simply won't match any author mention in the body.
_MAX_SLUG_LEN: Final[int] = 40


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CommentNotFound(LookupError):
    """The comment id is unknown in the caller's workspace (404).

    Also raised on the personal-task gate: if the target occurrence's
    :attr:`Occurrence.is_personal` is set and the caller is neither
    the creator nor a workspace owner, every read / write path
    collapses to a 404 so the mere existence of the comment is never
    leaked to an outsider.
    """


class CommentKindForbidden(PermissionError):
    """The caller is not allowed to post this comment ``kind`` (403).

    Raised when a non-agent caller attempts ``kind='agent'`` (only
    the embedded workspace agent token may speak as the agent), or
    when an external caller attempts ``kind='system'`` (system
    markers are internal-only — the completion / assignment services
    call through with ``internal_caller=True``).
    """


class CommentEditWindowExpired(ValueError):
    """The 5-minute author grace window has elapsed (409).

    §06 "Task notes are the agent inbox": authors can amend their
    own messages "within a small grace window". Past that window
    the edit path routes through the moderator capability
    (``tasks.comment_moderate``) — callers that need to amend later
    go through :func:`delete_comment` + :func:`post_comment`, or a
    future moderator-edit path.
    """


class CommentNotEditable(ValueError):
    """The comment can never be edited: agent / system kind, or already
    deleted (409).

    §06 pins the rule: agent messages carry their ``llm_call_id`` and
    must not drift from the prompt/completion pair; system markers
    are internal state-change artefacts and the audit log is their
    canonical store. Already-soft-deleted rows short-circuit here
    so a caller never re-edits a deleted message.
    """


class CommentMentionInvalid(ValueError):
    """A ``@mention`` target is not a workspace member (422).

    Raised at write time so the caller learns loudly instead of
    the §10 fanout silently dropping the delivery. Carries the
    offending slug(s) so the router can surface
    "``@unknown_user`` is not in this workspace" verbatim.
    """

    def __init__(self, unknown_slugs: Sequence[str]) -> None:
        super().__init__(
            f"comment mentions non-member slug(s): {list(unknown_slugs)!r}"
        )
        self.unknown_slugs: tuple[str, ...] = tuple(unknown_slugs)


class CommentMentionAmbiguous(ValueError):
    """A ``@mention`` slug resolves to more than one workspace member (422).

    The v1 slug is derived from ``User.display_name`` by
    :func:`_normalise_slug` — two members whose display names normalise
    to the same token ("maya-p" and "Maya P") collide. Silently picking
    one would mis-route the §10 offline-mention email and leave an
    audit trail that names the wrong recipient, so the service refuses
    the write and surfaces every offending slug. The caller (or a
    future :attr:`User.display_name_slug` column with a uniqueness
    constraint) must disambiguate before retrying.

    This is the defensive pair of :class:`CommentMentionInvalid`: the
    unknown-slug case says "that handle matches no one"; the ambiguous
    case says "that handle matches several people — pick one".
    """

    def __init__(self, ambiguous_slugs: Sequence[str]) -> None:
        super().__init__(
            f"comment mentions ambiguous slug(s) (multiple workspace members "
            f"share the normalised handle): {list(ambiguous_slugs)!r}"
        )
        self.ambiguous_slugs: tuple[str, ...] = tuple(ambiguous_slugs)


class CommentAttachmentInvalid(ValueError):
    """An attachment file_id is unknown or foreign to this task (422).

    The payload's ``attachments`` list carries :class:`Evidence`
    ids. Each must resolve to a row in the caller's workspace AND
    anchored to the same occurrence — an evidence id from a
    different task (or a different workspace) is rejected here so
    the agent inbox never cross-links attachments across threads.
    """

    def __init__(self, unknown_ids: Sequence[str]) -> None:
        super().__init__(
            f"comment references unknown / foreign evidence id(s): "
            f"{list(unknown_ids)!r}"
        )
        self.unknown_ids: tuple[str, ...] = tuple(unknown_ids)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class CommentCreate(BaseModel):
    """Request body for ``POST /tasks/{occurrence_id}/chat``.

    ``attachments`` is a list of :class:`Evidence` ids (not blob
    hashes) — the same pipeline the §06 evidence surface uses, so the
    upload path stays identical (one file pipeline, two consumers).
    """

    model_config = ConfigDict(extra="forbid")

    body_md: str = Field(..., min_length=1, max_length=_MAX_BODY_LEN)
    attachments: list[str] = Field(default_factory=list, max_length=_MAX_ATTACHMENTS)


@dataclass(frozen=True, slots=True)
class CommentView:
    """Immutable read projection of a ``comment`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry soft-state
    timestamps (``edited_at``, ``deleted_at``) that are managed by
    the service, not the caller's payload — the same reasoning as
    :class:`~app.domain.tasks.templates.TaskTemplateView`.

    ``attachments`` round-trips the denormalised list persisted on
    :attr:`Comment.attachments_json` (one ``{evidence_id, blob_hash,
    kind}`` dict per resolved file); callers rendering the thread
    walk it directly without re-visiting :class:`Evidence`.
    """

    id: str
    occurrence_id: str
    kind: CommentKind
    author_user_id: str | None
    body_md: str
    mentioned_user_ids: tuple[str, ...]
    attachments: tuple[dict[str, Any], ...]
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    llm_call_id: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    SQLite's ``DateTime(timezone=True)`` column type strips tzinfo on
    read (the dialect has no native TZ support); Postgres preserves
    the offset. Tagging a naive value as UTC is safe under the
    cross-backend invariant "time is UTC at rest"
    (see ``AGENTS.md`` §"Application-specific notes"). Mirrors the
    sibling helper in :mod:`app.domain.time.shifts`; duplicated here
    to keep the tasks context independent of the time context's
    import graph.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _narrow_kind(value: str) -> CommentKind:
    """Narrow a loaded DB string to the :data:`CommentKind` literal.

    The DB CHECK constraint already rejects anything else; this
    helper exists purely to satisfy mypy's strict-Literal reading
    without a ``cast``. An unexpected value indicates schema drift —
    raise rather than silently downgrade.
    """
    if value == "user":
        return "user"
    if value == "agent":
        return "agent"
    if value == "system":
        return "system"
    raise ValueError(f"unknown comment.kind {value!r} on loaded row")


def _row_to_view(row: Comment) -> CommentView:
    """Project a loaded :class:`Comment` row into a read view.

    Datetime columns pass through :func:`_ensure_utc` so SQLite-
    stripped rows come back comparable to ``clock.now()`` without
    forcing the comparison sites to re-stamp by hand.
    """
    return CommentView(
        id=row.id,
        occurrence_id=row.occurrence_id,
        kind=_narrow_kind(row.kind),
        author_user_id=row.author_user_id,
        body_md=row.body_md,
        mentioned_user_ids=tuple(row.mentioned_user_ids),
        attachments=tuple(dict(item) for item in row.attachments_json),
        created_at=_ensure_utc(row.created_at),
        edited_at=(_ensure_utc(row.edited_at) if row.edited_at is not None else None),
        deleted_at=(
            _ensure_utc(row.deleted_at) if row.deleted_at is not None else None
        ),
        llm_call_id=row.llm_call_id,
    )


def _normalise_slug(display_name: str) -> str:
    """Lowercase + alphanum-plus-dashes, capped at :data:`_MAX_SLUG_LEN`.

    Bridge form until ``User.display_name_slug`` lands: derive the
    mention slug from ``display_name`` so the author's handle tracks
    their profile without a separate write path. Non-slug characters
    collapse to ``-``; leading / trailing ``-`` are trimmed so
    ``"  Maya  "`` matches ``@maya`` (not ``@-maya-``).

    A display name that normalises to an empty string (e.g. all
    non-ASCII) produces an empty slug — the mention resolver treats
    that as "cannot be mentioned by slug" rather than a wildcard.
    """
    lowered = display_name.lower()
    collapsed = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    trimmed = collapsed.strip("-")
    return trimmed[:_MAX_SLUG_LEN]


def _extract_mention_slugs(body_md: str) -> tuple[str, ...]:
    """Return the ordered, unique set of ``@slug`` tokens in ``body_md``.

    Duplicates collapse — mentioning ``@maya`` twice in one message
    still resolves to a single ``mentioned_user_ids`` entry (the
    downstream messaging fanout deduplicates at delivery time
    anyway; doing it here keeps the audit diff small).
    """
    seen: dict[str, None] = {}
    for match in _MENTION_RE.finditer(body_md):
        slug = match.group(1)
        if slug not in seen:
            seen[slug] = None
    return tuple(seen.keys())


def _resolve_mentions(
    session: Session,
    ctx: WorkspaceContext,
    slugs: Sequence[str],
) -> tuple[list[str], tuple[str, ...], tuple[str, ...]]:
    """Resolve slugs → (mentioned user ids, unknown slugs, ambiguous slugs).

    Walks every :class:`UserWorkspace` in ``ctx.workspace_id`` and
    matches on the normalised ``display_name`` slug. Returns the
    ordered list of resolved user ids (preserving the first-mention
    order from the body), the tuple of slugs that didn't match
    anyone (caller raises :class:`CommentMentionInvalid`), and the
    tuple of slugs that matched **more than one** workspace member
    (caller raises :class:`CommentMentionAmbiguous`).

    Collisions are a real hazard in the v1 bridge: two members whose
    ``display_name`` normalises to the same token (e.g. "Maya P" and
    "maya-p") would otherwise be resolved by insertion order — the
    first walking match wins, and the §10 offline-mention email goes
    to the wrong user. Surfacing collisions as an explicit 422 makes
    the failure loud instead of silent; once
    :attr:`User.display_name_slug` lands with a per-workspace unique
    constraint, this branch becomes unreachable.
    """
    if not slugs:
        return [], (), ()
    rows = session.execute(
        select(User.id, User.display_name)
        .join(UserWorkspace, UserWorkspace.user_id == User.id)
        .where(UserWorkspace.workspace_id == ctx.workspace_id)
    ).all()
    # Collect *every* user id that normalises to each slug, preserving
    # insertion order for deterministic error messages. A slug mapping
    # to more than one id is a collision the service refuses to
    # silently disambiguate.
    slug_to_user_ids: dict[str, list[str]] = {}
    for user_id, display_name in rows:
        slug = _normalise_slug(display_name)
        if not slug:
            continue
        slug_to_user_ids.setdefault(slug, []).append(user_id)
    resolved: list[str] = []
    unknown: list[str] = []
    ambiguous: list[str] = []
    for slug in slugs:
        candidates = slug_to_user_ids.get(slug)
        if candidates is None:
            unknown.append(slug)
            continue
        if len(candidates) > 1:
            if slug not in ambiguous:
                ambiguous.append(slug)
            continue
        user_id = candidates[0]
        if user_id not in resolved:
            resolved.append(user_id)
    return resolved, tuple(unknown), tuple(ambiguous)


def _resolve_attachments(
    session: Session,
    ctx: WorkspaceContext,
    *,
    occurrence_id: str,
    evidence_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Resolve a list of evidence ids to persisted attachment payloads.

    Each id must resolve to an :class:`Evidence` row in the caller's
    workspace AND the target occurrence; an unknown or foreign id
    raises :class:`CommentAttachmentInvalid`. The returned list
    carries one ``{evidence_id, blob_hash, kind}`` dict per resolved
    file, in the caller's original order.

    Keeping the persisted payload denormalised (instead of a plain
    id list) means a later evidence soft-delete does not break the
    thread view — the comment still knows what was attached even if
    the evidence row is archived.
    """
    if not evidence_ids:
        return []
    rows = session.scalars(
        select(Evidence).where(
            Evidence.workspace_id == ctx.workspace_id,
            Evidence.occurrence_id == occurrence_id,
            Evidence.id.in_(list(evidence_ids)),
        )
    ).all()
    by_id: dict[str, Evidence] = {row.id: row for row in rows}
    unknown: list[str] = [fid for fid in evidence_ids if fid not in by_id]
    if unknown:
        raise CommentAttachmentInvalid(unknown)
    # Preserve caller order so the thread renders attachments in the
    # order the author dropped them into the composer.
    return [
        {
            "evidence_id": by_id[fid].id,
            "blob_hash": by_id[fid].blob_hash,
            "kind": by_id[fid].kind,
        }
        for fid in evidence_ids
    ]


def _load_occurrence(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
) -> Occurrence:
    """Load ``occurrence_id`` scoped to the caller's workspace.

    The ORM tenant filter already constrains SELECTs to
    ``ctx.workspace_id``; the explicit predicate below is
    defence-in-depth.
    """
    row = session.scalar(
        select(Occurrence).where(
            Occurrence.id == occurrence_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise CommentNotFound(f"task {occurrence_id!r} not visible in workspace")
    return row


def _personal_task_gate(
    ctx: WorkspaceContext,
    occurrence: Occurrence,
) -> None:
    """Enforce §06 "Self-created and personal tasks" visibility.

    A personal task is readable + writable only by:

    * the user who created it (``Occurrence.created_by_user_id ==
      ctx.actor_id``), OR
    * a workspace owner (``ctx.actor_was_owner_member``).

    Any other caller hits :class:`CommentNotFound` (404) so the mere
    existence of the task is not leaked — the spec's posture on
    personal-task visibility.
    """
    if not occurrence.is_personal:
        return
    if ctx.actor_was_owner_member:
        return
    if occurrence.created_by_user_id == ctx.actor_id:
        return
    raise CommentNotFound(
        f"task {occurrence.id!r} is personal and not visible to caller"
    )


def _load_comment(
    session: Session,
    ctx: WorkspaceContext,
    comment_id: str,
) -> tuple[Comment, Occurrence]:
    """Load a comment + its occurrence with the personal-task gate.

    Returns both rows so call sites (edit / delete / get) avoid a
    second round-trip. Raises :class:`CommentNotFound` on any
    workspace mismatch, missing comment, or personal-task gate
    refusal — the three shapes all collapse to 404 to keep the
    existence of a personal-task comment opaque.
    """
    row = session.scalar(
        select(Comment).where(
            Comment.id == comment_id,
            Comment.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise CommentNotFound(f"comment {comment_id!r} not visible in workspace")
    occurrence = _load_occurrence(session, ctx, row.occurrence_id)
    _personal_task_gate(ctx, occurrence)
    return row, occurrence


def _view_to_diff_dict(view: CommentView) -> dict[str, Any]:
    """Flatten a :class:`CommentView` into a JSON-safe dict for audit.

    Stringifies the three ``datetime`` columns so the audit row's
    ``diff`` JSON payload stays portable (SQLite JSON1 + PG JSONB
    both accept plain strings but reject native ``datetime``
    objects). Mirrors the helper in :mod:`app.domain.time.shifts`.
    """
    return {
        "id": view.id,
        "occurrence_id": view.occurrence_id,
        "kind": view.kind,
        "author_user_id": view.author_user_id,
        "body_md": view.body_md,
        "mentioned_user_ids": list(view.mentioned_user_ids),
        "attachments": [dict(item) for item in view.attachments],
        "created_at": view.created_at.isoformat(),
        "edited_at": view.edited_at.isoformat() if view.edited_at is not None else None,
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
        "llm_call_id": view.llm_call_id,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def post_comment(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
    payload: CommentCreate,
    *,
    kind: CommentKind = "user",
    llm_call_id: str | None = None,
    internal_caller: bool = False,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> CommentView:
    """Append a new comment to ``occurrence_id`` and return the fresh view.

    Flow:

    1. **Kind gate.** ``kind='agent'`` requires ``ctx.actor_kind ==
       'agent'`` (the embedded workspace agent token). ``kind='system'``
       requires ``internal_caller=True`` (a domain service on the
       inside of the boundary). Any other combination raises
       :class:`CommentKindForbidden`.
    2. **Load + personal-task gate.** Load the target occurrence,
       enforce the personal-task visibility rule.
    3. **Resolve mentions.** Parse ``@slug`` tokens, match against
       :class:`UserWorkspace` rows. Any unknown slug raises
       :class:`CommentMentionInvalid`.
    4. **Resolve attachments.** Each id in ``payload.attachments``
       must match an :class:`Evidence` row anchored to the target
       occurrence. Unknown / foreign ids raise
       :class:`CommentAttachmentInvalid`.
    5. **Write.** Insert the :class:`Comment` row; flush so the id
       is visible to the subsequent audit + event writes.
    6. **Audit + event.** ``task_comment.create`` with the full
       view as the ``after`` diff; then publish
       :class:`TaskCommentAdded` on the bus.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    # --- Kind gate. -----------------------------------------------
    # ``system`` kind is internal-only — the completion / assignment
    # services call through with ``internal_caller=True``; any other
    # caller gets a 403 so a compromised HTTP payload cannot forge a
    # system marker.
    if kind == "system" and not internal_caller:
        raise CommentKindForbidden(
            "kind='system' is internal-only; call through with "
            "internal_caller=True from a domain service"
        )
    # ``agent`` kind is reserved for the embedded workspace agent
    # token. The tenancy middleware sets ``actor_kind='agent'`` on
    # that path (§11); any other caller hits the 403.
    if kind == "agent" and ctx.actor_kind != "agent":
        raise CommentKindForbidden(
            f"kind='agent' requires actor_kind='agent' (got {ctx.actor_kind!r})"
        )

    occurrence = _load_occurrence(session, ctx, occurrence_id)
    _personal_task_gate(ctx, occurrence)

    # --- Mention resolution. --------------------------------------
    # Skip for non-user kinds — agent / system messages don't mention
    # humans in the current surface. A future enhancement could let
    # the agent @-tag workspace members to request follow-up; until
    # then the cleanest contract is "no mentions on non-user rows".
    mentioned_user_ids: list[str] = []
    if kind == "user":
        slugs = _extract_mention_slugs(payload.body_md)
        resolved, unknown, ambiguous = _resolve_mentions(session, ctx, slugs)
        if unknown:
            raise CommentMentionInvalid(unknown)
        if ambiguous:
            raise CommentMentionAmbiguous(ambiguous)
        mentioned_user_ids = resolved

    # --- Attachment resolution. -----------------------------------
    attachments_payload = _resolve_attachments(
        session,
        ctx,
        occurrence_id=occurrence_id,
        evidence_ids=payload.attachments,
    )

    # --- Author resolution. ---------------------------------------
    # ``system`` rows carry NULL author — the audit log is their
    # canonical "who did this" record. ``user`` and ``agent`` rows
    # carry the caller's ``actor_id`` (an agent token's actor_id is
    # the agent's own user row per §11).
    author_user_id: str | None = None if kind == "system" else ctx.actor_id

    now = resolved_clock.now()
    row = Comment(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        occurrence_id=occurrence_id,
        author_user_id=author_user_id,
        body_md=payload.body_md,
        created_at=now,
        attachments_json=attachments_payload,
        kind=kind,
        mentioned_user_ids=mentioned_user_ids,
        edited_at=None,
        deleted_at=None,
        llm_call_id=llm_call_id,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="task_comment",
        entity_id=row.id,
        action="task_comment.create",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    resolved_bus.publish(
        TaskCommentAdded(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            task_id=occurrence_id,
            comment_id=row.id,
            kind=kind,
            author_user_id=author_user_id,
            mentioned_user_ids=list(mentioned_user_ids),
        )
    )
    return view


def edit_comment(
    session: Session,
    ctx: WorkspaceContext,
    comment_id: str,
    body_md: str,
    *,
    clock: Clock | None = None,
) -> CommentView:
    """Rewrite ``body_md`` on ``comment_id`` within the author grace window.

    Rules:

    * Only the original author may edit (no moderator-edit path yet;
      moderators delete + re-post).
    * Only ``kind='user'`` rows are editable — agent / system rows
      carry :class:`CommentNotEditable`.
    * Soft-deleted rows are not editable — the soft-delete is
      terminal for the author.
    * The edit must land within :data:`EDIT_WINDOW` of the row's
      ``created_at``; past that window, :class:`CommentEditWindowExpired`.
    * Mentions re-resolve from the new body; unknown slugs raise
      :class:`CommentMentionInvalid`.

    The audit row carries before / after shapes so reports can
    reconstruct the edit.
    """
    resolved_clock = clock if clock is not None else SystemClock()

    row, _occurrence = _load_comment(session, ctx, comment_id)

    if row.kind != "user":
        raise CommentNotEditable(
            f"comment {comment_id!r} kind={row.kind!r} is not editable"
        )
    if row.deleted_at is not None:
        raise CommentNotEditable(
            f"comment {comment_id!r} is already deleted; edits refused"
        )
    if row.author_user_id != ctx.actor_id:
        # Author-only edit path — moderators use
        # ``tasks.comment_moderate`` for deletes; a moderator-edit
        # surface is not part of the v1 slice.
        raise CommentKindForbidden(
            f"only the original author may edit comment {comment_id!r}"
        )
    created_at = _ensure_utc(row.created_at)
    if resolved_clock.now() - created_at > EDIT_WINDOW:
        raise CommentEditWindowExpired(
            f"comment {comment_id!r} is past the {EDIT_WINDOW} edit window"
        )
    if not body_md or len(body_md) > _MAX_BODY_LEN:
        raise ValueError(
            f"edited body_md must be 1..{_MAX_BODY_LEN} chars (got {len(body_md)})"
        )

    before_view = _row_to_view(row)

    slugs = _extract_mention_slugs(body_md)
    resolved_mentions, unknown, ambiguous = _resolve_mentions(session, ctx, slugs)
    if unknown:
        raise CommentMentionInvalid(unknown)
    if ambiguous:
        raise CommentMentionAmbiguous(ambiguous)

    row.body_md = body_md
    row.mentioned_user_ids = resolved_mentions
    row.edited_at = resolved_clock.now()
    session.flush()

    after_view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="task_comment",
        entity_id=row.id,
        action="task_comment.edit",
        diff={
            "before": _view_to_diff_dict(before_view),
            "after": _view_to_diff_dict(after_view),
        },
        clock=resolved_clock,
    )
    return after_view


def delete_comment(
    session: Session,
    ctx: WorkspaceContext,
    comment_id: str,
    *,
    clock: Clock | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> CommentView:
    """Soft-delete ``comment_id`` (sets ``deleted_at``).

    Permission:

    * The author may delete their own comment at any time (no
      grace-window gate — deletions are cheaper than edits for the
      reader, and a late delete still honours the author's intent).
    * Every other caller flows through :func:`app.authz.require` on
      the ``tasks.comment_moderate`` capability (§05 rule-driven
      actions). The check resolves the occurrence's property scope
      when available (so a property-scoped rule can grant /
      revoke moderation per property) and falls back to the
      workspace scope otherwise. Owners pass via their
      ``owners`` group membership; the catalog's default_allow
      extends that to ``managers``; future permission_rule rows
      can widen or narrow.
    * Permission-denied fallbacks collapse to
      :class:`CommentKindForbidden` (403) so callers and tests
      receive a stable domain error shape regardless of whether the
      denial came from the authz enforcer or the author shortcut.

    ``rule_repo`` defaults to :class:`EmptyPermissionRuleRepository`
    (v1 has no ``permission_rule`` table yet). Tests pin this
    explicitly when they want to assert a rule-driven branch; the
    REST router (cd-sn26) will inject the production repo.

    Already-deleted rows raise :class:`CommentNotEditable` — a
    second delete would be a no-op, but silently accepting it would
    mask a buggy client. The row is returned in its deleted shape.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_repo = (
        rule_repo if rule_repo is not None else EmptyPermissionRuleRepository()
    )

    row, occurrence = _load_comment(session, ctx, comment_id)

    if row.deleted_at is not None:
        raise CommentNotEditable(f"comment {comment_id!r} is already deleted")

    is_author = row.author_user_id is not None and row.author_user_id == ctx.actor_id
    if not is_author and not ctx.actor_was_owner_member:
        # Non-author, non-owner. Defence-in-depth: call the authz
        # enforcer on ``tasks.comment_moderate``. The service is the
        # domain truth, so this re-check protects the CLI / agent /
        # integration-test entry points that don't flow through the
        # REST dependency chain. Owners short-circuit above via
        # ``actor_was_owner_member`` (a cached mirror of the
        # ``owners@<workspace>`` group membership that the middleware
        # populated) so the common "workspace owner moderating in
        # the chat" path doesn't require seeded ``role_grants`` in
        # test fixtures. Property scope when available, so a
        # property-scoped allow / deny rule is honoured for
        # managers / contractors.
        scope_kind = "property" if occurrence.property_id is not None else "workspace"
        scope_id = (
            occurrence.property_id
            if occurrence.property_id is not None
            else ctx.workspace_id
        )
        try:
            require(
                session,
                ctx,
                action_key="tasks.comment_moderate",
                scope_kind=scope_kind,
                scope_id=scope_id,
                rule_repo=resolved_repo,
            )
        except PermissionDenied as exc:
            # Collapse to the domain's 403 shape. The enforcer already
            # logged the structured denial; the caller doesn't need
            # the capability name.
            raise CommentKindForbidden(
                f"caller {ctx.actor_id!r} may not moderate comment {comment_id!r}"
            ) from exc

    before_view = _row_to_view(row)
    row.deleted_at = resolved_clock.now()
    session.flush()

    after_view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="task_comment",
        entity_id=row.id,
        action="task_comment.delete",
        diff={
            "before": _view_to_diff_dict(before_view),
            "after": _view_to_diff_dict(after_view),
        },
        clock=resolved_clock,
    )
    return after_view


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_comment(
    session: Session,
    ctx: WorkspaceContext,
    comment_id: str,
) -> CommentView:
    """Return the single-row view for ``comment_id``.

    Enforces the personal-task gate on the parent occurrence; the
    comment is 404 for any caller who can't see the task. A soft-
    deleted row is returned to owners but 404's for everyone else,
    parity with :func:`list_comments`.
    """
    row, _occurrence = _load_comment(session, ctx, comment_id)
    if row.deleted_at is not None and not ctx.actor_was_owner_member:
        raise CommentNotFound(
            f"comment {comment_id!r} is deleted; not visible to non-owner"
        )
    return _row_to_view(row)


def list_comments(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
    *,
    after: datetime | None = None,
    after_id: str | None = None,
    limit: int = 100,
) -> list[CommentView]:
    """Return every live comment on ``occurrence_id``, oldest-first.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are hidden for
    every reader except workspace owners, so moderation history
    survives without bleeding into the worker / manager thread view.

    Cursor semantics: ``after`` is an exclusive lower bound on the
    last-seen comment's ``created_at``; ``after_id`` is the
    last-seen comment's ULID. The pair forms a compound tuple
    cursor — ``(created_at, id) > (after, after_id)`` — so two
    comments sharing the same ``created_at`` (possible when an
    agent batch-posts within one clock tick) are paginated in ULID
    order without skipping any. Passing ``after`` alone keeps the
    coarser "strictly later than this instant" semantics for the
    initial page.

    ``limit`` is capped at 1000 as a defence-in-depth against a
    caller that forgets to paginate; the REST router (cd-sn26) will
    re-clamp on the way in.

    Personal-task gate applies as usual.
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive (got {limit})")
    if after_id is not None and after is None:
        raise ValueError(
            "after_id requires a corresponding after= timestamp; cursors "
            "are (created_at, id) tuples."
        )
    effective_limit = min(limit, 1000)

    occurrence = _load_occurrence(session, ctx, occurrence_id)
    _personal_task_gate(ctx, occurrence)

    stmt = select(Comment).where(
        Comment.workspace_id == ctx.workspace_id,
        Comment.occurrence_id == occurrence_id,
    )
    if not ctx.actor_was_owner_member:
        stmt = stmt.where(Comment.deleted_at.is_(None))
    if after is not None:
        if after_id is None:
            stmt = stmt.where(Comment.created_at > after)
        else:
            # Tuple-cursor: strictly greater than (after, after_id).
            # Emulated as a disjunction for portability across SQLite
            # (no row-value tuple comparison) and Postgres (supports
            # it, but the disjunction is equally readable in the
            # query plan and avoids a dialect-specific path).
            stmt = stmt.where(
                (Comment.created_at > after)
                | ((Comment.created_at == after) & (Comment.id > after_id))
            )
    stmt = stmt.order_by(Comment.created_at.asc(), Comment.id.asc()).limit(
        effective_limit
    )
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]
