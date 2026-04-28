"""Privacy export, purge, and operational-log retention services."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    Executable,
    Table,
    delete,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.integrations.models import WebhookDelivery
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.messaging.models import EmailDelivery
from app.adapters.db.payroll.models import PayoutDestination, Payslip
from app.adapters.db.privacy.models import PrivacyExport
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.tasks.models import Comment, Occurrence
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement, Workspace
from app.adapters.storage.ports import BlobNotFound, Storage
from app.audit import write_audit
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

EXPORT_TTL = timedelta(days=7)
REDACTED_TEXT = "[redacted by privacy purge]"
RETENTION_DEFAULT_DAYS: dict[str, int] = {
    "audit_log": 730,
    "session": 90,
    "llm_usage": 90,
    "email_delivery": 90,
    "webhook_delivery": 90,
}


@dataclass(frozen=True, slots=True)
class ExportResult:
    id: str
    status: str
    poll_url: str
    download_url: str | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class PurgeResult:
    person_id: str
    workspace_ids: tuple[str, ...]
    anonymized_users: int
    scrubbed_occurrences: int
    scrubbed_comments: int
    scrubbed_expenses: int
    scrubbed_payout_destinations: int
    scrubbed_payslips: int
    deleted_secret_envelopes: int


@dataclass(frozen=True, slots=True)
class RetentionResult:
    table: str
    workspace_id: str | None
    archived_rows: int


def request_user_export(
    session: Session,
    storage: Storage,
    *,
    user_id: str,
    poll_base_path: str = "/api/v1/me/export",
    clock: Clock | None = None,
) -> ExportResult:
    """Create and complete an access-export job for ``user_id``.

    The worker queue is deliberately in-process for this slice: the
    persistent job row, poll URL, storage bundle, signed URL, and audit
    trail are the contract. A later external worker can move
    ``_build_export_bundle`` behind an async dispatcher without changing
    the API surface.
    """
    now = _now(clock)
    job = PrivacyExport(
        id=new_ulid(),
        user_id=user_id,
        status="running",
        blob_hash=None,
        error=None,
        requested_at=now,
        completed_at=None,
        expires_at=None,
    )
    with tenant_agnostic():
        session.add(job)
        payload = _build_export_bundle(session, storage, user_id=user_id, now=now)
        content_hash = hashlib.sha256(payload).hexdigest()
        storage.put(content_hash, io.BytesIO(payload), content_type="application/zip")
        job.status = "completed"
        job.blob_hash = content_hash
        job.completed_at = now
        job.expires_at = now + EXPORT_TTL
        _audit_export_requested(session, user_id=user_id, job_id=job.id, now=now)
    return ExportResult(
        id=job.id,
        status=job.status,
        poll_url=f"{poll_base_path}/{job.id}",
        download_url=storage.sign_url(
            content_hash,
            ttl_seconds=int(EXPORT_TTL.total_seconds()),
        ),
        expires_at=job.expires_at,
    )


def get_user_export(
    session: Session,
    storage: Storage,
    *,
    user_id: str,
    export_id: str,
) -> ExportResult | None:
    with tenant_agnostic():
        job = session.get(PrivacyExport, export_id)
    if job is None or job.user_id != user_id:
        return None
    download_url = None
    if job.status == "completed" and job.blob_hash is not None:
        download_url = storage.sign_url(
            job.blob_hash,
            ttl_seconds=int(EXPORT_TTL.total_seconds()),
        )
    return ExportResult(
        id=job.id,
        status=job.status,
        poll_url=f"/api/v1/me/export/{job.id}",
        download_url=download_url,
        expires_at=job.expires_at,
    )


def purge_person(
    session: Session,
    *,
    person_id: str,
    workspace_id: str | None = None,
    actor_id: str = "system:privacy-purge",
    dry_run: bool = False,
    clock: Clock | None = None,
) -> PurgeResult:
    """Anonymise one person and scrub dependent free-text/routing data."""
    now = _now(clock)
    with tenant_agnostic():
        workspace_ids = _workspace_ids_for_person(session, person_id, workspace_id)
        payout_destinations = session.scalars(
            select(PayoutDestination).where(
                PayoutDestination.user_id == person_id,
                PayoutDestination.workspace_id.in_(workspace_ids),
            )
        ).all()
        secret_ids = tuple(
            row.secret_ref_id for row in payout_destinations if row.secret_ref_id
        )
        payslip_ids = tuple(
            row.id
            for row in session.scalars(
                select(Payslip).where(
                    Payslip.user_id == person_id,
                    Payslip.workspace_id.in_(workspace_ids),
                )
            )
        )

        counts = _count_purge_targets(
            session,
            person_id,
            workspace_ids,
            secret_ids,
            payslip_ids,
        )
        if dry_run:
            return PurgeResult(
                person_id=person_id,
                workspace_ids=workspace_ids,
                **counts,
            )

        anonymized_users = _rowcount(
            session.execute(
                update(User)
                .where(User.id == person_id)
                .values(
                    email=f"purged+{person_id}@privacy.invalid",
                    email_lower=f"purged+{person_id}@privacy.invalid",
                    display_name="Purged user",
                    locale=None,
                    timezone=None,
                    avatar_blob_hash=None,
                    archived_at=now,
                )
            )
        )
        scrubbed_occurrences = _rowcount(
            session.execute(
                update(Occurrence)
                .where(
                    Occurrence.workspace_id.in_(workspace_ids),
                    Occurrence.created_by_user_id == person_id,
                )
                .values(title=REDACTED_TEXT, description_md=REDACTED_TEXT)
            )
        )
        scrubbed_comments = _rowcount(
            session.execute(
                update(Comment)
                .where(
                    Comment.workspace_id.in_(workspace_ids),
                    Comment.author_user_id == person_id,
                )
                .values(body_md=REDACTED_TEXT, attachments_json=[], edited_at=now)
            )
        )
        scrubbed_expenses = _rowcount(
            session.execute(
                update(ExpenseClaim)
                .where(
                    ExpenseClaim.workspace_id.in_(workspace_ids),
                    ExpenseClaim.work_engagement_id.in_(
                        select(WorkEngagement.id).where(
                            WorkEngagement.user_id == person_id
                        )
                    ),
                )
                .values(
                    vendor=REDACTED_TEXT, note_md=REDACTED_TEXT, llm_autofill_json=None
                )
            )
        )
        deleted_secret_envelopes = 0
        if secret_ids:
            deleted_secret_envelopes = _rowcount(
                session.execute(
                    delete(SecretEnvelope).where(SecretEnvelope.id.in_(secret_ids))
                )
            )
        scrubbed_payout_destinations = _rowcount(
            session.execute(
                update(PayoutDestination)
                .where(
                    PayoutDestination.user_id == person_id,
                    PayoutDestination.workspace_id.in_(workspace_ids),
                )
                .values(
                    display_stub=None,
                    secret_ref_id=None,
                    country=None,
                    label=None,
                    updated_at=now,
                )
            )
        )
        scrubbed_payslips = _scrub_payslips(session, payslip_ids=payslip_ids, now=now)
        for target_workspace_id in workspace_ids:
            write_audit(
                session,
                _system_ctx(target_workspace_id, actor_id=actor_id),
                entity_kind="user",
                entity_id=person_id,
                action="audit.privacy.purge.complete",
                diff={
                    "person_id": person_id,
                    "payout_destinations": scrubbed_payout_destinations,
                    "payslips": scrubbed_payslips,
                },
            )
    return PurgeResult(
        person_id=person_id,
        workspace_ids=workspace_ids,
        anonymized_users=anonymized_users,
        scrubbed_occurrences=scrubbed_occurrences,
        scrubbed_comments=scrubbed_comments,
        scrubbed_expenses=scrubbed_expenses,
        scrubbed_payout_destinations=scrubbed_payout_destinations,
        scrubbed_payslips=scrubbed_payslips,
        deleted_secret_envelopes=deleted_secret_envelopes,
    )


def payout_manifest_available(
    session: Session,
    *,
    payslip_id: str,
    workspace_id: str,
) -> bool:
    with tenant_agnostic():
        payslip = session.get(Payslip, payslip_id)
    return (
        payslip is not None
        and payslip.workspace_id == workspace_id
        and payslip.payout_manifest_purged_at is None
    )


def rotate_operational_logs(
    session: Session,
    *,
    data_dir: Path,
    clock: Clock | None = None,
) -> tuple[RetentionResult, ...]:
    now = _now(clock)
    results: list[RetentionResult] = []
    with tenant_agnostic():
        workspaces = session.scalars(select(Workspace)).all()
        for table_name in RETENTION_DEFAULT_DAYS:
            if table_name == "session":
                results.append(
                    _archive_and_delete(
                        session,
                        table=cast("Table", SessionRow.__table__),
                        workspace_id=None,
                        cutoff=now - timedelta(days=RETENTION_DEFAULT_DAYS[table_name]),
                        cutoff_column=cast(
                            "ColumnElement[datetime | None]",
                            SessionRow.invalidated_at,
                        ),
                        data_dir=data_dir,
                        where=SessionRow.invalidated_at.is_not(None),
                    )
                )
                continue
            for workspace in workspaces:
                days = _retention_days(workspace, table_name)
                table = _retention_table(table_name)
                results.append(
                    _archive_and_delete(
                        session,
                        table=table,
                        workspace_id=workspace.id,
                        cutoff=now - timedelta(days=days),
                        data_dir=data_dir,
                    )
                )
    return tuple(result for result in results if result.archived_rows > 0)


def _build_export_bundle(
    session: Session, storage: Storage, *, user_id: str, now: datetime
) -> bytes:
    records: dict[str, list[dict[str, object]]] = {}
    blob_hashes: set[str] = set()
    for table in _export_tables():
        filters = _subject_filters(table, user_id)
        if not filters:
            continue
        rows = session.execute(select(table).where(or_(*filters))).mappings().all()
        if not rows:
            continue
        records[table.name] = [_jsonable_dict(row) for row in rows]
        for row in rows:
            blob_hashes.update(_blob_hashes(row))

    manifest = {
        "generated_at": now.isoformat(),
        "subject_user_id": user_id,
        "tables": records,
        "attachments": sorted(blob_hashes),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export.json", json.dumps(manifest, sort_keys=True, indent=2))
        for blob_hash in sorted(blob_hashes):
            try:
                with storage.get(blob_hash) as blob:
                    archive.writestr(f"attachments/{blob_hash}", blob.read())
            except BlobNotFound:
                continue
    return output.getvalue()


def _export_tables() -> tuple[Table, ...]:
    return tuple(sorted(_interesting_tables(), key=lambda table: table.name))


def _interesting_tables() -> set[Table]:
    metadata = User.metadata
    names = {
        "user",
        "user_workspace",
        "work_engagement",
        "session",
        "api_token",
        "passkey_credential",
        "audit_log",
        "occurrence",
        "comment",
        "evidence",
        "expense_claim",
        "expense_attachment",
        "payslip",
        "payout_destination",
        "email_delivery",
        "notification",
        "chat_message",
        "llm_usage",
        "webhook_delivery",
    }
    return {table for name, table in metadata.tables.items() if name in names}


def _subject_filters(table: Table, user_id: str) -> list[ColumnElement[bool]]:
    columns = table.c
    filters: list[ColumnElement[bool]] = []
    for name in (
        "user_id",
        "actor_id",
        "subject_user_id",
        "delegate_for_user_id",
        "assignee_user_id",
        "completed_by_user_id",
        "reviewer_user_id",
        "created_by_user_id",
        "author_user_id",
        "recipient_user_id",
        "to_person_id",
    ):
        if name in columns:
            filters.append(columns[name] == user_id)
    if table.name == "user" and "id" in columns:
        filters = [columns["id"] == user_id]
    return filters


def _blob_hashes(row: object) -> set[str]:
    if not hasattr(row, "items"):
        return set()
    hashes: set[str] = set()
    for key, value in row.items():
        if isinstance(value, str) and (
            key.endswith("blob_hash") or key == "avatar_blob_hash"
        ):
            hashes.add(value)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    blob_hash = item.get("blob_hash")
                    if isinstance(blob_hash, str):
                        hashes.add(blob_hash)
    return hashes


def _audit_export_requested(
    session: Session, *, user_id: str, job_id: str, now: datetime
) -> None:
    rows = session.execute(
        select(UserWorkspace.workspace_id, Workspace.slug)
        .join(Workspace, Workspace.id == UserWorkspace.workspace_id)
        .where(UserWorkspace.user_id == user_id)
    ).all()
    for workspace_id, slug in rows:
        write_audit(
            session,
            WorkspaceContext(
                workspace_id=workspace_id,
                workspace_slug=slug,
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="worker",
                actor_was_owner_member=False,
                audit_correlation_id=job_id,
            ),
            entity_kind="privacy_export",
            entity_id=job_id,
            action="audit.privacy.export.issued",
            diff={"issued_at": now.isoformat()},
        )


def _workspace_ids_for_person(
    session: Session, person_id: str, workspace_id: str | None
) -> tuple[str, ...]:
    if workspace_id is not None:
        return (workspace_id,)
    ids = session.scalars(
        select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == person_id)
    ).all()
    if not ids:
        ids = session.scalars(
            select(Payslip.workspace_id).where(Payslip.user_id == person_id).distinct()
        ).all()
    return tuple(ids)


def _count_purge_targets(
    session: Session,
    person_id: str,
    workspace_ids: tuple[str, ...],
    secret_ids: tuple[str, ...],
    payslip_ids: tuple[str, ...],
) -> dict[str, int]:
    return {
        "anonymized_users": 1 if session.get(User, person_id) is not None else 0,
        "scrubbed_occurrences": _count(
            session,
            select(Occurrence.id).where(
                Occurrence.workspace_id.in_(workspace_ids),
                Occurrence.created_by_user_id == person_id,
            ),
        ),
        "scrubbed_comments": _count(
            session,
            select(Comment.id).where(
                Comment.workspace_id.in_(workspace_ids),
                Comment.author_user_id == person_id,
            ),
        ),
        "scrubbed_expenses": 0,
        "scrubbed_payout_destinations": _count(
            session,
            select(PayoutDestination.id).where(
                PayoutDestination.workspace_id.in_(workspace_ids),
                PayoutDestination.user_id == person_id,
            ),
        ),
        "scrubbed_payslips": len(payslip_ids),
        "deleted_secret_envelopes": _count(
            session, select(SecretEnvelope.id).where(SecretEnvelope.id.in_(secret_ids))
        )
        if secret_ids
        else 0,
    }


def _scrub_payslips(
    session: Session,
    *,
    payslip_ids: tuple[str, ...],
    now: datetime,
) -> int:
    if not payslip_ids:
        return 0
    rows = session.scalars(select(Payslip).where(Payslip.id.in_(payslip_ids))).all()
    for row in rows:
        snapshot = row.payout_snapshot_json
        row.payout_snapshot_json = _scrub_payout_snapshot(snapshot)
        row.payout_manifest_purged_at = now
    return len(rows)


def _scrub_payout_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    allowed = {"destination_id", "kind", "currency", "amount_cents"}
    scrubbed = {key: value for key, value in snapshot.items() if key in allowed}
    scrubbed["label"] = None
    scrubbed["display_stub"] = None
    return scrubbed


def _archive_and_delete(
    session: Session,
    *,
    table: Table,
    workspace_id: str | None,
    cutoff: datetime,
    data_dir: Path,
    where: ColumnElement[bool] | None = None,
    cutoff_column: ColumnElement[datetime | None] | None = None,
) -> RetentionResult:
    conditions: list[ColumnElement[bool]] = []
    if cutoff_column is not None:
        conditions.append(cutoff_column < cutoff)
    elif "created_at" in table.c:
        conditions.append(table.c.created_at < cutoff)
    elif "last_seen_at" in table.c:
        conditions.append(table.c.last_seen_at < cutoff)
    if workspace_id is not None and "workspace_id" in table.c:
        conditions.append(table.c.workspace_id == workspace_id)
    if where is not None:
        conditions.append(where)
    rows = session.execute(select(table).where(*conditions)).mappings().all()
    if not rows:
        return RetentionResult(
            table=table.name,
            workspace_id=workspace_id,
            archived_rows=0,
        )
    archive_dir = data_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{table.name}.jsonl.gz"
    with gzip.open(archive_path, "at", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(_jsonable_dict(row), sort_keys=True))
            fh.write("\n")
    pks = [column.name for column in inspect(table).primary_key]
    delete_conditions = [table.c[name].in_([row[name] for row in rows]) for name in pks]
    session.execute(delete(table).where(*delete_conditions))
    return RetentionResult(
        table=table.name,
        workspace_id=workspace_id,
        archived_rows=len(rows),
    )


def _retention_table(table_name: str) -> Table:
    mapping = {
        "audit_log": AuditLog.__table__,
        "llm_usage": LlmUsage.__table__,
        "email_delivery": EmailDelivery.__table__,
        "webhook_delivery": WebhookDelivery.__table__,
    }
    return cast("Table", mapping[table_name])


def _retention_days(workspace: Workspace, table_name: str) -> int:
    value = workspace.settings_json.get(f"privacy.retention.{table_name}_days")
    if isinstance(value, int) and value > 0:
        return value
    return RETENTION_DEFAULT_DAYS[table_name]


def _system_ctx(workspace_id: str, *, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_id,
        actor_id=actor_id,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
        principal_kind="system",
    )


def _count(session: Session, stmt: Executable) -> int:
    return len(session.execute(stmt).all())


def _rowcount(result: object) -> int:
    value = getattr(result, "rowcount", 0)
    return value if isinstance(value, int) else 0


def _jsonable_dict(row: object) -> dict[str, object]:
    if not hasattr(row, "items"):
        return {}
    return {str(key): _jsonable(value) for key, value in row.items()}


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _now(clock: Clock | None) -> datetime:
    return (clock if clock is not None else SystemClock()).now().astimezone(UTC)
