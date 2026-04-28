"""Unit tests for :mod:`app.domain.tasks.evidence`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import ChecklistItem, Evidence, Occurrence
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.evidence import (
    EvidenceGpsPayloadInvalid,
    EvidencePolicyError,
    EvidenceUpload,
    PermissionDenied,
    delete_evidence,
    list_evidence,
    snapshot_checklist,
    upload_evidence,
)
from app.events.bus import EventBus
from app.events.types import TaskEvidenceAdded
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

_PINNED = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


class _PinnedSniffer:
    def __init__(self, verdict: str | None) -> None:
        self.verdict = verdict

    def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
        _ = payload, hint
        return self.verdict


def _load_all_models() -> None:
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def _ctx(
    workspace_id: str,
    *,
    actor_id: str | None = None,
    role: str = "manager",
    owner: bool = True,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id if actor_id is not None else new_ulid(),
        actor_kind="user",
        actor_grant_role=role,  # type: ignore[arg-type]
        actor_was_owner_member=owner,
        audit_correlation_id=new_ulid(),
    )


def _workspace(session: Session) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Workspace",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _property(session: Session) -> str:
    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return property_id


def _user(session: Session) -> str:
    user_id = new_ulid()
    session.add(
        User(
            id=user_id,
            email=f"{user_id}@example.com",
            email_lower=f"{user_id}@example.com".lower(),
            display_name=user_id,
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    session.flush()
    return user_id


def _task(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    photo_evidence: str = "optional",
) -> str:
    task_id = new_ulid()
    session.add(
        Occurrence(
            id=task_id,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=None,
            starts_at=_PINNED,
            ends_at=_PINNED + timedelta(minutes=30),
            scheduled_for_local="2026-04-28T14:00",
            originally_scheduled_for="2026-04-28T14:00",
            state="pending",
            cancellation_reason=None,
            title="Pool clean",
            description_md="",
            priority="normal",
            photo_evidence=photo_evidence,
            duration_minutes=30,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return task_id


def test_upload_photo_uses_storage_strips_exif_audits_and_emits_event(
    session: Session, clock: FrozenClock, bus: EventBus
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(session, workspace_id=ws, property_id=prop)
    ctx = _ctx(ws, actor_id=_user(session))
    seen: list[TaskEvidenceAdded] = []
    bus.subscribe(TaskEvidenceAdded)(seen.append)
    storage = InMemoryStorage()
    exif = b"Exif\x00\x00camera-gps"
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xe1"
        + (len(exif) + 2).to_bytes(2, "big")
        + exif
        + b"\xff\xda\x00\x08scan"
        + b"pixels"
        + b"\xff\xd9"
    )

    view = upload_evidence(
        session,
        ctx,
        task_id,
        EvidenceUpload(kind="photo", bytes=jpeg, mime="image/jpeg"),
        storage=storage,
        mime_sniffer=_PinnedSniffer("image/jpeg"),
        clock=clock,
        event_bus=bus,
    )

    assert view.kind == "photo"
    assert view.blob_hash is not None
    with storage.get(view.blob_hash) as fh:
        stored = fh.read()
    assert b"Exif\x00\x00" not in stored
    assert b"pixels" in stored

    actions = session.scalars(select(AuditLog.action)).all()
    assert "task_evidence.upload" in actions
    assert "task.evidence.photo.add" in actions
    assert [event.evidence_id for event in seen] == [view.id]


def test_forbid_policy_rejects_photo_before_storage(
    session: Session, clock: FrozenClock
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(
        session, workspace_id=ws, property_id=prop, photo_evidence="disabled"
    )
    storage = InMemoryStorage()

    with pytest.raises(EvidencePolicyError):
        upload_evidence(
            session,
            _ctx(ws),
            task_id,
            EvidenceUpload(kind="photo", bytes=b"jpeg", mime="image/jpeg"),
            storage=storage,
            mime_sniffer=_PinnedSniffer("image/jpeg"),
            clock=clock,
        )

    assert storage._blobs == {}


def test_required_policy_allows_partial_non_photo_save(
    session: Session, clock: FrozenClock
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(
        session, workspace_id=ws, property_id=prop, photo_evidence="required"
    )

    view = upload_evidence(
        session,
        _ctx(ws, actor_id=_user(session)),
        task_id,
        EvidenceUpload(kind="note", text="missing filter, needs follow-up"),
        clock=clock,
    )

    assert view.kind == "note"
    assert view.note_md == "missing filter, needs follow-up"


def test_gps_requires_complete_valid_coordinates(
    session: Session, clock: FrozenClock
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(session, workspace_id=ws, property_id=prop)

    with pytest.raises(EvidenceGpsPayloadInvalid):
        upload_evidence(
            session,
            _ctx(ws),
            task_id,
            EvidenceUpload(kind="gps", gps_lat=48.8566, mime="application/json"),
            storage=InMemoryStorage(),
            mime_sniffer=_PinnedSniffer("application/json"),
            clock=clock,
        )

    with pytest.raises(EvidenceGpsPayloadInvalid):
        upload_evidence(
            session,
            _ctx(ws),
            task_id,
            EvidenceUpload(
                kind="gps",
                gps_lat=91.0,
                gps_lon=2.3522,
                mime="application/json",
            ),
            storage=InMemoryStorage(),
            mime_sniffer=_PinnedSniffer("application/json"),
            clock=clock,
        )


def test_gps_upload_stores_json_via_storage(
    session: Session, clock: FrozenClock
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(session, workspace_id=ws, property_id=prop)
    storage = InMemoryStorage()

    view = upload_evidence(
        session,
        _ctx(ws, actor_id=_user(session)),
        task_id,
        EvidenceUpload(
            kind="gps",
            gps_lat=48.8566,
            gps_lon=2.3522,
            mime="application/json",
        ),
        storage=storage,
        mime_sniffer=_PinnedSniffer("application/json"),
        clock=clock,
    )

    assert view.gps_lat == 48.8566
    assert view.gps_lon == 2.3522
    assert view.blob_hash is not None
    with storage.get(view.blob_hash) as fh:
        assert fh.read() == b'{"lat":48.8566,"lon":2.3522}'


def test_snapshot_captures_live_checklist_rows(
    session: Session, clock: FrozenClock, bus: EventBus
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(session, workspace_id=ws, property_id=prop)
    item = ChecklistItem(
        id=new_ulid(),
        workspace_id=ws,
        occurrence_id=task_id,
        label="Wipe counters",
        position=0,
        requires_photo=True,
        checked=True,
        checked_at=_PINNED,
        evidence_blob_hash="abc",
    )
    session.add(item)
    session.flush()

    view = snapshot_checklist(
        session,
        _ctx(ws, actor_id=_user(session)),
        task_id,
        clock=clock,
        event_bus=bus,
    )
    item.label = "Edited later"
    session.flush()

    assert view.kind == "checklist_snapshot"
    assert view.checklist_snapshot_json is not None
    assert view.checklist_snapshot_json[0]["label"] == "Wipe counters"
    row = session.get(Evidence, view.id)
    assert row is not None
    assert row.checklist_snapshot_json[0]["label"] == "Wipe counters"


def test_delete_soft_deletes_and_hides_from_list(
    session: Session, clock: FrozenClock
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(session, workspace_id=ws, property_id=prop)
    uploader = _user(session)
    stranger = _user(session)
    view = upload_evidence(
        session,
        _ctx(ws, actor_id=uploader, role="worker", owner=False),
        task_id,
        EvidenceUpload(kind="note", text="done"),
        clock=clock,
    )

    with pytest.raises(PermissionDenied):
        delete_evidence(
            session,
            _ctx(ws, actor_id=stranger, role="worker", owner=False),
            view.id,
            clock=clock,
        )

    delete_evidence(
        session,
        _ctx(ws, actor_id=uploader, role="worker", owner=False),
        view.id,
        clock=clock,
    )

    row = session.get(Evidence, view.id)
    assert row is not None
    assert row.deleted_at == _PINNED.replace(tzinfo=None)
    assert list_evidence(session, _ctx(ws), task_id=task_id) == ()
    assert "task_evidence.delete" in session.scalars(select(AuditLog.action)).all()


def test_dedup_by_sha256_reuses_existing_row(
    session: Session, clock: FrozenClock
) -> None:
    ws = _workspace(session)
    prop = _property(session)
    task_id = _task(session, workspace_id=ws, property_id=prop)
    storage = InMemoryStorage()
    payload = b"RIFF\x34\x00\x00\x00WAVEfmt " + b"\x00" * 32
    upload = EvidenceUpload(kind="voice", bytes=payload, mime="audio/wav")

    first = upload_evidence(
        session,
        _ctx(ws, actor_id=_user(session)),
        task_id,
        upload,
        storage=storage,
        mime_sniffer=_PinnedSniffer("audio/wav"),
        clock=clock,
    )
    second = upload_evidence(
        session,
        _ctx(ws, actor_id=_user(session)),
        task_id,
        upload,
        storage=storage,
        mime_sniffer=_PinnedSniffer("audio/wav"),
        clock=clock,
    )

    assert second.id == first.id
    rows = session.scalars(select(Evidence).where(Evidence.occurrence_id == task_id))
    assert len(rows.all()) == 1
