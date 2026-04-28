"""Task evidence upload, listing, deletion, and checklist snapshots."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.tasks.models import ChecklistItem, Evidence, Occurrence
from app.adapters.storage.mime import FiletypeMimeSniffer
from app.adapters.storage.ports import MimeSniffer, Storage
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import TaskEvidenceAdded
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

EvidenceKind = Literal["photo", "voice", "note", "checklist_snapshot", "gps"]
FileEvidenceKind = Literal["photo", "voice", "gps"]
EvidencePolicy = Literal["forbid", "require", "optional"]
EvidencePolicyResolver = Callable[
    [Session, WorkspaceContext, Occurrence], EvidencePolicy
]
PhotoNormalizer = Callable[[bytes, str], bytes]

_PHOTO_ALLOWED_MIME: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic"}
)
_VOICE_ALLOWED_MIME: frozenset[str] = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/wav",
        "audio/x-wav",
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

_MAX_BYTES_BY_KIND: dict[FileEvidenceKind, int] = {
    "photo": 10 * 1024 * 1024,
    "voice": 25 * 1024 * 1024,
    "gps": 4 * 1024,
}


@dataclass(frozen=True, slots=True)
class EvidenceUpload:
    kind: EvidenceKind
    bytes: bytes | None = None
    mime: str | None = None
    text: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


@dataclass(frozen=True, slots=True)
class EvidenceView:
    id: str
    workspace_id: str
    occurrence_id: str
    kind: EvidenceKind
    blob_hash: str | None
    note_md: str | None
    gps_lat: float | None
    gps_lon: float | None
    checklist_snapshot_json: tuple[dict[str, object | None], ...] | None
    created_at: datetime
    created_by_user_id: str | None
    deleted_at: datetime | None


class TaskNotFound(LookupError):
    """The task id is unknown in the caller's workspace."""


class EvidenceNotFound(LookupError):
    """The evidence id is unknown in the caller's workspace."""


class PermissionDenied(PermissionError):
    """Caller cannot mutate the evidence row."""


class EvidencePolicyError(ValueError):
    """The resolved evidence policy rejects this upload."""


class PhotoForbidden(EvidencePolicyError):
    """Photo evidence is forbidden by the effective evidence policy."""


class EvidenceRequired(ValueError):
    """Completion requires photo evidence, but none exists."""


class EvidenceContentTypeNotAllowed(ValueError):
    """Server-sniffed content type is outside the per-kind allow-list."""

    def __init__(self, *, kind: str, content_type: str | None) -> None:
        super().__init__(
            f"content_type {content_type!r} is not allowed for evidence kind {kind!r}"
        )
        self.kind = kind
        self.content_type = content_type


class EvidenceTooLarge(ValueError):
    """Payload size exceeds the per-kind cap."""

    def __init__(self, *, kind: str, size_bytes: int, cap_bytes: int) -> None:
        super().__init__(
            f"evidence kind {kind!r} payload of {size_bytes} bytes exceeds "
            f"the {cap_bytes}-byte cap"
        )
        self.kind = kind
        self.size_bytes = size_bytes
        self.cap_bytes = cap_bytes


class EvidenceGpsPayloadInvalid(ValueError):
    """The gps evidence payload is partial, invalid, or out of range."""


def upload_evidence(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    payload: EvidenceUpload,
    *,
    storage: Storage | None = None,
    mime_sniffer: MimeSniffer | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    evidence_policy: EvidencePolicyResolver | None = None,
    photo_normalizer: PhotoNormalizer | None = None,
) -> EvidenceView:
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    task = _load_task(session, ctx, task_id)

    if payload.kind == "note":
        row = _insert_note(session, ctx, task, payload, clock=resolved_clock)
        _audit_upload(session, ctx, resolved_clock, task=task, row=row, extra={})
        _emit_added(resolved_bus, ctx, resolved_clock, task=task, row=row)
        return _evidence_row_to_view(row)

    if payload.kind == "checklist_snapshot":
        raise ValueError("use snapshot_checklist() for checklist_snapshot evidence")

    if payload.kind == "photo":
        resolver = evidence_policy if evidence_policy is not None else _default_policy
        if resolver(session, ctx, task) == "forbid":
            raise PhotoForbidden("photo evidence is forbidden for this task")

    if storage is None:
        raise ValueError(f"kind={payload.kind!r} evidence requires a storage port")

    content, sniffed_type, original_size = _prepare_file_payload(
        payload,
        mime_sniffer=mime_sniffer,
        photo_normalizer=photo_normalizer,
    )
    blob_hash = hashlib.sha256(content).hexdigest()

    existing = _find_existing_blob_evidence(
        session,
        ctx,
        task_id=task.id,
        kind=payload.kind,
        blob_hash=blob_hash,
    )
    if existing is not None:
        if not storage.exists(blob_hash):
            storage.put(blob_hash, io.BytesIO(content), content_type=sniffed_type)
        return _evidence_row_to_view(existing)

    storage.put(blob_hash, io.BytesIO(content), content_type=sniffed_type)
    row = Evidence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        occurrence_id=task.id,
        kind=payload.kind,
        blob_hash=blob_hash,
        note_md=None,
        gps_lat=payload.gps_lat if payload.kind == "gps" else None,
        gps_lon=payload.gps_lon if payload.kind == "gps" else None,
        checklist_snapshot_json=None,
        created_at=resolved_clock.now(),
        created_by_user_id=ctx.actor_id,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    _audit_upload(
        session,
        ctx,
        resolved_clock,
        task=task,
        row=row,
        extra={
            "blob_hash": blob_hash,
            "content_type": sniffed_type,
            "declared_content_type": payload.mime,
            "size_bytes": len(content),
            "original_size_bytes": original_size,
        },
    )
    _emit_added(resolved_bus, ctx, resolved_clock, task=task, row=row)
    return _evidence_row_to_view(row)


def list_evidence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
) -> tuple[EvidenceView, ...]:
    task = _load_task(session, ctx, task_id)
    rows = session.scalars(
        select(Evidence)
        .where(
            Evidence.workspace_id == ctx.workspace_id,
            Evidence.occurrence_id == task.id,
            Evidence.deleted_at.is_(None),
        )
        .order_by(Evidence.created_at.asc(), Evidence.id.asc())
    ).all()
    return tuple(_evidence_row_to_view(row) for row in rows)


def delete_evidence(
    session: Session,
    ctx: WorkspaceContext,
    evidence_id: str,
    *,
    clock: Clock | None = None,
) -> None:
    resolved_clock = clock if clock is not None else SystemClock()
    row = session.scalar(
        select(Evidence).where(
            Evidence.workspace_id == ctx.workspace_id,
            Evidence.id == evidence_id,
            Evidence.deleted_at.is_(None),
        )
    )
    if row is None:
        raise EvidenceNotFound(f"evidence {evidence_id!r} not visible in workspace")
    if not _is_manager_or_owner(ctx) and row.created_by_user_id != ctx.actor_id:
        raise PermissionDenied(
            f"actor {ctx.actor_id!r} cannot delete evidence {evidence_id!r}"
        )

    row.deleted_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="task_evidence",
        entity_id=row.id,
        action="task_evidence.delete",
        diff={
            "before": {"deleted_at": None},
            "after": {"deleted_at": row.deleted_at.isoformat()},
        },
        clock=resolved_clock,
    )


def snapshot_checklist(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> EvidenceView:
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    task = _load_task(session, ctx, task_id)
    items = session.scalars(
        select(ChecklistItem)
        .where(
            ChecklistItem.workspace_id == ctx.workspace_id,
            ChecklistItem.occurrence_id == task.id,
        )
        .order_by(ChecklistItem.position.asc(), ChecklistItem.id.asc())
    ).all()
    snapshot = [_checklist_item_snapshot(item) for item in items]
    row = Evidence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        occurrence_id=task.id,
        kind="checklist_snapshot",
        blob_hash=None,
        note_md=None,
        gps_lat=None,
        gps_lon=None,
        checklist_snapshot_json=snapshot,
        created_at=resolved_clock.now(),
        created_by_user_id=ctx.actor_id,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="task_evidence",
        entity_id=row.id,
        action="task_evidence.snapshot",
        diff={"after": {"task_id": task.id, "item_count": len(snapshot)}},
        clock=resolved_clock,
    )
    _emit_added(resolved_bus, ctx, resolved_clock, task=task, row=row)
    return _evidence_row_to_view(row)


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


def _default_policy(
    session: Session, ctx: WorkspaceContext, task: Occurrence
) -> EvidencePolicy:
    _ = session, ctx
    if task.photo_evidence == "disabled":
        return "forbid"
    if task.photo_evidence == "required":
        return "require"
    return "optional"


def _is_manager_or_owner(ctx: WorkspaceContext) -> bool:
    return ctx.actor_grant_role == "manager" or ctx.actor_was_owner_member


def _insert_note(
    session: Session,
    ctx: WorkspaceContext,
    task: Occurrence,
    payload: EvidenceUpload,
    *,
    clock: Clock,
) -> Evidence:
    text = (payload.text or "").strip()
    if not text:
        raise ValueError("text must be non-empty for kind='note' evidence")
    row = Evidence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        occurrence_id=task.id,
        kind="note",
        blob_hash=None,
        note_md=text,
        gps_lat=None,
        gps_lon=None,
        checklist_snapshot_json=None,
        created_at=clock.now(),
        created_by_user_id=ctx.actor_id,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _prepare_file_payload(
    payload: EvidenceUpload,
    *,
    mime_sniffer: MimeSniffer | None,
    photo_normalizer: PhotoNormalizer | None,
) -> tuple[bytes, str, int]:
    if payload.kind not in _MIME_ALLOWLIST_BY_KIND:
        raise ValueError(f"kind {payload.kind!r} is not file-bearing")
    raw = _gps_payload_bytes(payload) if payload.kind == "gps" else payload.bytes or b""
    if not raw:
        raise ValueError(f"kind={payload.kind!r} evidence payload must not be empty")

    cap = _MAX_BYTES_BY_KIND[payload.kind]
    if len(raw) > cap:
        raise EvidenceTooLarge(kind=payload.kind, size_bytes=len(raw), cap_bytes=cap)

    sniffer = mime_sniffer if mime_sniffer is not None else FiletypeMimeSniffer()
    sniffed_type = sniffer.sniff(raw, hint=payload.mime)
    allowed = _MIME_ALLOWLIST_BY_KIND[payload.kind]
    if sniffed_type is None or sniffed_type not in allowed:
        raise EvidenceContentTypeNotAllowed(
            kind=payload.kind, content_type=sniffed_type
        )

    if payload.kind == "gps":
        _validate_gps(payload.gps_lat, payload.gps_lon)
        return raw, sniffed_type, len(raw)

    if payload.kind == "photo":
        normalizer = (
            photo_normalizer if photo_normalizer is not None else normalize_photo_bytes
        )
        normalized = normalizer(raw, sniffed_type)
        return normalized, sniffed_type, len(raw)

    return raw, sniffed_type, len(raw)


def _gps_payload_bytes(payload: EvidenceUpload) -> bytes:
    if payload.gps_lat is None or payload.gps_lon is None:
        raise EvidenceGpsPayloadInvalid("gps evidence requires gps_lat and gps_lon")
    _validate_gps(payload.gps_lat, payload.gps_lon)
    if payload.bytes is not None:
        parsed_lat, parsed_lon = _parse_gps_json(payload.bytes)
        if parsed_lat != float(payload.gps_lat) or parsed_lon != float(payload.gps_lon):
            raise EvidenceGpsPayloadInvalid(
                "gps bytes do not match gps_lat and gps_lon"
            )
        return payload.bytes
    return json.dumps(
        {"lat": payload.gps_lat, "lon": payload.gps_lon},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_gps_json(payload: bytes) -> tuple[float, float]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceGpsPayloadInvalid(
            f"gps payload is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise EvidenceGpsPayloadInvalid("gps payload must be a JSON object")
    return _validate_gps(document.get("lat"), document.get("lon"))


def _validate_gps(lat_raw: object, lon_raw: object) -> tuple[float, float]:
    if lat_raw is None or lon_raw is None:
        raise EvidenceGpsPayloadInvalid(
            "gps evidence requires both gps_lat and gps_lon"
        )
    if isinstance(lat_raw, bool) or not isinstance(lat_raw, int | float):
        raise EvidenceGpsPayloadInvalid("gps_lat must be numeric")
    if isinstance(lon_raw, bool) or not isinstance(lon_raw, int | float):
        raise EvidenceGpsPayloadInvalid("gps_lon must be numeric")
    lat = float(lat_raw)
    lon = float(lon_raw)
    if not -90.0 <= lat <= 90.0:
        raise EvidenceGpsPayloadInvalid(f"gps_lat={lat!r} out of range")
    if not -180.0 <= lon <= 180.0:
        raise EvidenceGpsPayloadInvalid(f"gps_lon={lon!r} out of range")
    return lat, lon


def normalize_photo_bytes(payload: bytes, content_type: str) -> bytes:
    """Return photo bytes with metadata stripped before storage."""
    if content_type == "image/jpeg":
        return _strip_jpeg_exif(payload)
    if content_type == "image/png":
        return _strip_png_exif(payload)
    return payload


def _strip_jpeg_exif(payload: bytes) -> bytes:
    if not payload.startswith(b"\xff\xd8"):
        return payload
    out = bytearray(payload[:2])
    pos = 2
    length = len(payload)
    while pos + 4 <= length and payload[pos] == 0xFF:
        marker = payload[pos + 1]
        if marker == 0xDA:
            out.extend(payload[pos:])
            return bytes(out)
        if marker == 0xD9:
            out.extend(payload[pos:])
            return bytes(out)
        segment_len = int.from_bytes(payload[pos + 2 : pos + 4], "big")
        if segment_len < 2 or pos + 2 + segment_len > length:
            return payload
        segment = payload[pos : pos + 2 + segment_len]
        data = payload[pos + 4 : pos + 2 + segment_len]
        if not (marker == 0xE1 and data.startswith(b"Exif\x00\x00")):
            out.extend(segment)
        pos += 2 + segment_len
    out.extend(payload[pos:])
    return bytes(out)


def _strip_png_exif(payload: bytes) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    if not payload.startswith(signature):
        return payload
    out = bytearray(signature)
    pos = len(signature)
    while pos + 12 <= len(payload):
        length = int.from_bytes(payload[pos : pos + 4], "big")
        chunk_type = payload[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(payload):
            return payload
        if chunk_type != b"eXIf":
            out.extend(payload[pos:crc_end])
        pos = crc_end
        if chunk_type == b"IEND":
            out.extend(payload[pos:])
            return bytes(out)
    return payload


def _find_existing_blob_evidence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
    kind: str,
    blob_hash: str,
) -> Evidence | None:
    return session.scalar(
        select(Evidence)
        .where(
            Evidence.workspace_id == ctx.workspace_id,
            Evidence.occurrence_id == task_id,
            Evidence.kind == kind,
            Evidence.blob_hash == blob_hash,
            Evidence.deleted_at.is_(None),
        )
        .order_by(Evidence.created_at.asc(), Evidence.id.asc())
        .limit(1)
    )


def _audit_upload(
    session: Session,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task: Occurrence,
    row: Evidence,
    extra: dict[str, object | None],
) -> None:
    after: dict[str, object | None] = {
        "task_id": task.id,
        "evidence_id": row.id,
        "kind": row.kind,
        "note_md": row.note_md,
        "gps_lat": row.gps_lat,
        "gps_lon": row.gps_lon,
    }
    after.update(extra)
    write_audit(
        session,
        ctx,
        entity_kind="task_evidence",
        entity_id=row.id,
        action="task_evidence.upload",
        diff={"after": after},
        clock=clock,
    )
    write_audit(
        session,
        ctx,
        entity_kind="task",
        entity_id=task.id,
        action=f"task.evidence.{row.kind}.add",
        diff={"after": after},
        clock=clock,
    )


def _emit_added(
    event_bus: EventBus,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task: Occurrence,
    row: Evidence,
) -> None:
    event_bus.publish(
        TaskEvidenceAdded(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=clock.now(),
            task_id=task.id,
            evidence_id=row.id,
            kind=_narrow_evidence_kind(row.kind),
        )
    )


def _checklist_item_snapshot(item: ChecklistItem) -> dict[str, object | None]:
    return {
        "id": item.id,
        "label": item.label,
        "position": item.position,
        "required": item.requires_photo,
        "checked": item.checked,
        "checked_at": item.checked_at.isoformat() if item.checked_at else None,
        "evidence_blob_hash": item.evidence_blob_hash,
    }


def _narrow_evidence_kind(value: str) -> EvidenceKind:
    if value == "photo":
        return "photo"
    if value == "voice":
        return "voice"
    if value == "note":
        return "note"
    if value == "checklist_snapshot":
        return "checklist_snapshot"
    if value == "gps":
        return "gps"
    raise ValueError(f"unknown evidence.kind {value!r} on loaded row")


def _evidence_row_to_view(row: Evidence) -> EvidenceView:
    snapshot = row.checklist_snapshot_json
    return EvidenceView(
        id=row.id,
        workspace_id=row.workspace_id,
        occurrence_id=row.occurrence_id,
        kind=_narrow_evidence_kind(row.kind),
        blob_hash=row.blob_hash,
        note_md=row.note_md,
        gps_lat=row.gps_lat,
        gps_lon=row.gps_lon,
        checklist_snapshot_json=tuple(snapshot) if snapshot is not None else None,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
        deleted_at=row.deleted_at,
    )
