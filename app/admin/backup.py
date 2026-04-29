"""Host-only backup and restore service.

The public entry points back ``crewday admin backup`` and
``crewday admin restore``. They intentionally run in-process on the
deployment host rather than through HTTP; see specs §13 and §16.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import pkgutil
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import zstandard as zstd
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, func, inspect, make_url, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db as adapters_db_pkg
from app.adapters.db.base import Base
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.session import normalise_sync_url
from app.adapters.storage.envelope import compute_key_fingerprint
from app.config import Settings
from app.tenancy import tenant_agnostic

__all__ = [
    "BackupManifest",
    "BackupResult",
    "RestoreResult",
    "backup",
    "restore",
]


ArchiveKind = Literal["sqlite", "postgres"]

_ARCHIVE_PREFIX = "crewday-backup-"
_ARCHIVE_SUFFIX = ".tar.zst"
_MANIFEST = "manifest.json"
_SECRET_ENVELOPES = "secret_envelopes.jsonl"
_FILES_PREFIX = "files"
_SQLITE_DUMP = "db/crewday.sqlite3"
_POSTGRES_DUMP = "db/postgres.dump"


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Wire manifest written into each backup archive."""

    archive_version: int
    created_at: str
    kind: ArchiveKind
    database_member: str
    files_member: str
    secret_envelopes_member: str
    row_counts: dict[str, int]
    secret_envelope_count: int
    key_fps: list[str]
    current_key_fp: str | None
    content_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "archive_version": self.archive_version,
            "created_at": self.created_at,
            "kind": self.kind,
            "database_member": self.database_member,
            "files_member": self.files_member,
            "secret_envelopes_member": self.secret_envelopes_member,
            "row_counts": self.row_counts,
            "secret_envelope_count": self.secret_envelope_count,
            "key_fps": self.key_fps,
            "current_key_fp": self.current_key_fp,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object]) -> BackupManifest:
        if raw.get("archive_version") != 1:
            raise RuntimeError("unsupported backup archive version")
        row_counts_raw = raw.get("row_counts")
        key_fps_raw = raw.get("key_fps")
        if not isinstance(row_counts_raw, dict) or not isinstance(key_fps_raw, list):
            raise RuntimeError("backup manifest is malformed")
        return cls(
            archive_version=1,
            created_at=_require_str(raw, "created_at"),
            kind=_require_kind(raw),
            database_member=_require_str(raw, "database_member"),
            files_member=_require_str(raw, "files_member"),
            secret_envelopes_member=_require_str(raw, "secret_envelopes_member"),
            row_counts={str(k): int(v) for k, v in row_counts_raw.items()},
            secret_envelope_count=_require_int(raw, "secret_envelope_count"),
            key_fps=[str(v) for v in key_fps_raw],
            current_key_fp=(
                str(raw["current_key_fp"])
                if raw.get("current_key_fp") is not None
                else None
            ),
            content_sha256=_require_str(raw, "content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class BackupResult:
    archive_path: Path
    manifest: BackupManifest
    pruned: list[Path]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_database: Path | None
    restored_files: Path
    manifest: BackupManifest


def backup(
    out_dir: Path,
    *,
    settings: Settings,
    keep_daily: int = 30,
    keep_monthly: int = 12,
) -> BackupResult:
    """Create a ``.tar.zst`` deployment backup and prune old archives."""

    out_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(tz=UTC).replace(microsecond=0)
    archive_name = f"{_ARCHIVE_PREFIX}{created_at:%Y%m%dT%H%M%SZ}{_ARCHIVE_SUFFIX}"
    archive_path = out_dir / archive_name
    if archive_path.exists():
        raise RuntimeError(f"backup archive already exists: {archive_path}")
    _load_all_models()
    database_url = normalise_sync_url(settings.database_url)
    kind = _archive_kind(database_url)
    engine = create_engine(database_url, future=True)

    try:
        with tempfile.TemporaryDirectory(prefix="crewday-backup-") as tmp_raw:
            tmp = Path(tmp_raw)
            db_member = _SQLITE_DUMP if kind == "sqlite" else _POSTGRES_DUMP
            db_path = tmp / db_member
            db_path.parent.mkdir(parents=True, exist_ok=True)

            row_counts, envelopes = _snapshot_database_and_metadata(
                kind,
                engine,
                database_url=database_url,
                db_path=db_path,
            )

            envelopes_path = tmp / _SECRET_ENVELOPES
            envelopes_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in envelopes),
                encoding="utf-8",
            )

            staged_files = tmp / _FILES_PREFIX
            _stage_uploads(settings.data_dir / "files", staged_files)
            payload_hash = _content_hash(
                [(db_member, db_path), (_SECRET_ENVELOPES, envelopes_path)],
                files_root=staged_files,
            )
            key_fps = sorted({str(row["key_fp"]) for row in envelopes})
            manifest = BackupManifest(
                archive_version=1,
                created_at=created_at.isoformat(),
                kind=kind,
                database_member=db_member,
                files_member=_FILES_PREFIX,
                secret_envelopes_member=_SECRET_ENVELOPES,
                row_counts=row_counts,
                secret_envelope_count=len(envelopes),
                key_fps=key_fps,
                current_key_fp=_current_key_fp(settings.root_key),
                content_sha256=payload_hash,
            )
            (tmp / _MANIFEST).write_text(
                json.dumps(manifest.as_json(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            _write_archive(archive_path, tmp)
    finally:
        engine.dispose()
    pruned = rotate_backups(out_dir, keep_daily=keep_daily, keep_monthly=keep_monthly)
    return BackupResult(archive_path=archive_path, manifest=manifest, pruned=pruned)


def restore(
    bundle: Path,
    *,
    settings: Settings,
    legacy_key_files: Iterable[Path] = (),
) -> RestoreResult:
    """Restore ``bundle`` into the database and local files root from settings."""

    database_url = normalise_sync_url(settings.database_url)
    with tempfile.TemporaryDirectory(prefix="crewday-restore-") as tmp_raw:
        tmp = Path(tmp_raw)
        _extract_archive(bundle, tmp)
        manifest = BackupManifest.from_json(
            json.loads((tmp / _MANIFEST).read_text(encoding="utf-8"))
        )
        target_kind = _archive_kind(database_url)
        if manifest.kind != target_kind:
            raise RuntimeError(
                f"backup is for {manifest.kind}, target database is {target_kind}"
            )
        expected = _content_hash(
            [
                (manifest.database_member, tmp / manifest.database_member),
                (
                    manifest.secret_envelopes_member,
                    tmp / manifest.secret_envelopes_member,
                ),
            ],
            files_root=tmp / manifest.files_member,
        )
        if expected != manifest.content_sha256:
            raise RuntimeError("backup content hash mismatch")

        key_fps = _key_fps_from_jsonl(tmp / manifest.secret_envelopes_member)
        if sorted(key_fps) != manifest.key_fps:
            raise RuntimeError("backup manifest key_fps do not match envelope payload")
        _verify_key_fps(key_fps, settings=settings, legacy_key_files=legacy_key_files)

        restored_database = _restore_database(
            manifest,
            tmp / manifest.database_member,
            database_url=database_url,
        )
        files_dest = settings.data_dir / "files"
        _restore_files(tmp / manifest.files_member, files_dest)
        return RestoreResult(
            restored_database=restored_database,
            restored_files=files_dest,
            manifest=manifest,
        )


def rotate_backups(
    out_dir: Path,
    *,
    keep_daily: int = 30,
    keep_monthly: int = 12,
) -> list[Path]:
    """Delete archives outside the daily/monthly retention windows."""
    if keep_daily < 0 or keep_monthly < 0:
        raise ValueError("backup retention values must be non-negative")

    archives = sorted(
        [
            p
            for p in out_dir.glob(f"{_ARCHIVE_PREFIX}*{_ARCHIVE_SUFFIX}")
            if p.is_file()
        ],
        key=lambda p: p.name,
        reverse=True,
    )
    keep: set[Path] = set()
    seen_days: set[str] = set()
    seen_months: set[str] = set()
    for path in archives:
        stamp = path.name.removeprefix(_ARCHIVE_PREFIX).removesuffix(_ARCHIVE_SUFFIX)
        day = stamp[:8]
        month = stamp[:6]
        if len(seen_days) < keep_daily and day not in seen_days:
            keep.add(path)
            seen_days.add(day)
        if len(seen_months) < keep_monthly and month not in seen_months:
            keep.add(path)
            seen_months.add(month)

    pruned: list[Path] = []
    for path in archives:
        if path in keep:
            continue
        path.unlink()
        pruned.append(path)
    return pruned


def _load_all_models() -> None:
    for modinfo in pkgutil.iter_modules(
        adapters_db_pkg.__path__, prefix=f"{adapters_db_pkg.__name__}."
    ):
        if not modinfo.ispkg:
            continue
        models_name = f"{modinfo.name}.models"
        try:
            importlib.import_module(models_name)
        except ModuleNotFoundError as exc:
            if exc.name == models_name:
                continue
            raise


def _archive_kind(database_url: str) -> ArchiveKind:
    driver = make_url(database_url).drivername
    if driver.startswith("sqlite"):
        return "sqlite"
    if driver.startswith("postgresql"):
        return "postgres"
    raise RuntimeError(f"unsupported database backend for backup: {driver}")


def _sqlite_snapshot(engine: Engine, dest: Path) -> None:
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(FULL)")
        raw = conn.connection.driver_connection
        if not isinstance(raw, sqlite3.Connection):
            raise RuntimeError("sqlite backup expected a sqlite3 connection")
        dest_conn = sqlite3.connect(dest)
        try:
            raw.backup(dest_conn)
        finally:
            dest_conn.close()


def _snapshot_database_and_metadata(
    kind: ArchiveKind,
    engine: Engine,
    *,
    database_url: str,
    db_path: Path,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    if kind == "sqlite":
        _sqlite_snapshot(engine, db_path)
        snapshot_engine = create_engine(f"sqlite:///{db_path}", future=True)
        try:
            factory = sessionmaker(
                bind=snapshot_engine,
                expire_on_commit=False,
                class_=Session,
            )
            with factory() as session:
                return _row_counts(session), _secret_envelope_rows(session)
        finally:
            snapshot_engine.dispose()

    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        transaction = conn.begin()
        try:
            snapshot = str(
                conn.exec_driver_sql("SELECT pg_export_snapshot()").scalar_one()
            )
            _pg_dump(database_url, db_path, snapshot=snapshot)
            session = Session(bind=conn, expire_on_commit=False)
            try:
                metadata = _row_counts(session), _secret_envelope_rows(session)
            finally:
                session.close()
            transaction.commit()
            return metadata
        except Exception:
            transaction.rollback()
            raise


def _pg_dump(database_url: str, dest: Path, *, snapshot: str | None = None) -> None:
    cmd = ["pg_dump", "-Fc", "--file", str(dest)]
    if snapshot is not None:
        cmd.extend(["--snapshot", snapshot])
    cmd.append(database_url)
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")


def _row_counts(session: Session) -> dict[str, int]:
    bind = session.get_bind()
    existing = set(inspect(bind).get_table_names())
    counts: dict[str, int] = {}
    with tenant_agnostic():
        for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
            if table.name not in existing:
                continue
            try:
                counts[table.name] = int(
                    session.execute(select(func.count()).select_from(table)).scalar_one()
                )
            except SQLAlchemyError:
                continue
    return counts


def _secret_envelope_rows(session: Session) -> list[dict[str, object]]:
    with tenant_agnostic():
        rows = session.scalars(select(SecretEnvelope).order_by(SecretEnvelope.id)).all()
        return [
            {
                "id": row.id,
                "owner_entity_kind": row.owner_entity_kind,
                "owner_entity_id": row.owner_entity_id,
                "purpose": row.purpose,
                "ciphertext": base64.b64encode(bytes(row.ciphertext)).decode("ascii"),
                "nonce": base64.b64encode(bytes(row.nonce)).decode("ascii"),
                "key_fp": bytes(row.key_fp).hex(),
                "created_at": row.created_at.astimezone(UTC).isoformat(),
                "rotated_at": (
                    row.rotated_at.astimezone(UTC).isoformat()
                    if row.rotated_at is not None
                    else None
                ),
            }
            for row in rows
        ]


def _content_hash(
    files: Iterable[tuple[str, Path]],
    *,
    files_root: Path,
) -> str:
    h = hashlib.sha256()
    for name, path in sorted(files):
        h.update(b"file\0")
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    if files_root.exists():
        for path in sorted(p for p in files_root.rglob("*") if p.is_file()):
            rel = path.relative_to(files_root).as_posix()
            h.update(b"upload\0")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def _stage_uploads(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    if src.exists():
        shutil.copytree(src, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)


def _write_archive(archive_path: Path, tmp: Path) -> None:
    compressor = zstd.ZstdCompressor()
    with (
        archive_path.open("xb") as raw,
        compressor.stream_writer(raw) as zfh,
        tarfile.open(fileobj=zfh, mode="w|") as tar,
    ):
        tar.add(tmp / _MANIFEST, arcname=_MANIFEST)
        tar.add(tmp / _SECRET_ENVELOPES, arcname=_SECRET_ENVELOPES)
        db_dir = tmp / "db"
        if db_dir.exists():
            tar.add(db_dir, arcname="db")
        files_root = tmp / _FILES_PREFIX
        if files_root.exists():
            tar.add(files_root, arcname=_FILES_PREFIX)
        else:
            info = tarfile.TarInfo(_FILES_PREFIX)
            info.type = tarfile.DIRTYPE
            info.mtime = datetime.now(tz=UTC).timestamp()
            tar.addfile(info)


def _extract_archive(bundle: Path, dest: Path) -> None:
    decompressor = zstd.ZstdDecompressor()
    with (
        bundle.open("rb") as raw,
        decompressor.stream_reader(raw) as zfh,
        tarfile.open(fileobj=zfh, mode="r|") as tar,
    ):
        tar.extractall(dest, filter="data")


def _current_key_fp(root_key: SecretStr | None) -> str | None:
    if root_key is None:
        return None
    return compute_key_fingerprint(root_key).hex()


def _legacy_key_fps(paths: Iterable[Path]) -> set[str]:
    fps: set[str] = set()
    for path in paths:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise RuntimeError(f"legacy key file is empty: {path}")
        fps.add(compute_key_fingerprint(SecretStr(raw)).hex())
    return fps


def _verify_key_fps(
    key_fps: set[str],
    *,
    settings: Settings,
    legacy_key_files: Iterable[Path],
) -> None:
    allowed = _legacy_key_fps(legacy_key_files)
    active = _current_key_fp(settings.root_key)
    if active is not None:
        allowed.add(active)
    missing = sorted(key_fps - allowed)
    if missing:
        raise RuntimeError(
            "backup contains secret envelopes encrypted with unavailable key "
            f"fingerprint(s): {', '.join(missing)}"
        )


def _restore_database(
    manifest: BackupManifest,
    db_dump: Path,
    *,
    database_url: str,
) -> Path | None:
    if manifest.kind == "sqlite":
        parsed = make_url(database_url)
        target_raw = parsed.database
        if not target_raw or target_raw == ":memory:":
            raise RuntimeError("sqlite restore requires a file-backed database URL")
        target = Path(target_raw)
        if target.exists():
            raise RuntimeError(f"sqlite restore target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_dump, target)
        return target

    result = subprocess.run(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            database_url,
            str(db_dump),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {result.stderr.strip()}")
    return None


def _restore_files(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copytree(src, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)


def _key_fps_from_jsonl(path: Path) -> set[str]:
    fps: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key_fp = row.get("key_fp")
        if not isinstance(key_fp, str):
            raise RuntimeError("secret_envelopes JSONL row missing key_fp")
        fps.add(key_fp)
    return fps


def _require_str(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"backup manifest missing string field {key!r}")
    return value


def _require_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int):
        raise RuntimeError(f"backup manifest missing integer field {key!r}")
    return value


def _require_kind(raw: Mapping[str, object]) -> ArchiveKind:
    value = _require_str(raw, "kind")
    if value == "sqlite":
        return "sqlite"
    if value == "postgres":
        return "postgres"
    raise RuntimeError(f"unsupported backup kind {value!r}")
