"""Unit coverage for host-only backup / restore helpers."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.admin.backup as backup_module
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.secrets.models import SecretEnvelope
from app.admin.backup import BackupManifest, backup, restore, rotate_backups
from app.config import Settings
from app.tenancy import tenant_agnostic


def _load_all_models() -> None:
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


def _settings(db_path: Path, data_dir: Path, root_key: str) -> Settings:
    return Settings.model_construct(
        database_url=f"sqlite:///{db_path}",
        data_dir=data_dir,
        root_key=SecretStr(root_key),
        demo_mode=False,
        storage_backend="localfs",
    )


def _seed_database(db_path: Path) -> None:
    _load_all_models()
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        session.add(
            User(
                id="usr_backup",
                email="backup@example.com",
                email_lower="backup@example.com",
                display_name="Backup User",
                timezone="UTC",
                created_at=datetime(2026, 4, 29, 7, 0, tzinfo=UTC),
            )
        )
        session.add(
            SecretEnvelope(
                id="env_backup",
                owner_entity_kind="deployment_setting",
                owner_entity_id="smtp_password",
                purpose="smtp-password",
                ciphertext=b"ciphertext",
                nonce=b"123456789012",
                key_fp=bytes.fromhex("8eba9648f425e32b"),
                created_at=datetime(2026, 4, 29, 7, 1, tzinfo=UTC),
                rotated_at=None,
            )
        )
        session.commit()
    engine.dispose()


def test_sqlite_backup_restore_round_trip(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_data = tmp_path / "source-data"
    source_files = source_data / "files"
    source_files.mkdir(parents=True)
    (source_files / "avatar.bin").write_bytes(b"avatar")
    _seed_database(source_db)

    backup_result = backup(
        tmp_path / "backups",
        settings=_settings(source_db, source_data, "active-root-key"),
    )

    assert backup_result.archive_path.suffixes[-2:] == [".tar", ".zst"]
    assert backup_result.manifest.row_counts["user"] == 1
    assert backup_result.manifest.secret_envelope_count == 1

    restored_db = tmp_path / "restored.db"
    restored_data = tmp_path / "restored-data"
    restore_result = restore(
        backup_result.archive_path,
        settings=_settings(restored_db, restored_data, "active-root-key"),
        legacy_key_files=[_write_key(tmp_path, "legacy.key", "old-root-key")],
    )

    assert restore_result.restored_database == restored_db
    assert (restored_data / "files" / "avatar.bin").read_bytes() == b"avatar"

    engine = create_engine(f"sqlite:///{restored_db}", future=True)
    with Session(engine) as session, tenant_agnostic():
        user = session.scalar(select(User).where(User.id == "usr_backup"))
        envelope = session.scalar(
            select(SecretEnvelope).where(SecretEnvelope.id == "env_backup")
        )
    engine.dispose()
    assert user is not None
    assert user.email_lower == "backup@example.com"
    assert envelope is not None
    assert bytes(envelope.key_fp).hex() == "8eba9648f425e32b"


def test_restore_refuses_unavailable_secret_envelope_key(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_data = tmp_path / "source-data"
    _seed_database(source_db)
    result = backup(
        tmp_path / "backups",
        settings=_settings(source_db, source_data, "active-root-key"),
    )

    target_db = tmp_path / "target.db"
    with pytest.raises(RuntimeError, match="unavailable key fingerprint"):
        restore(
            result.archive_path,
            settings=_settings(target_db, tmp_path / "target-data", "wrong-root-key"),
        )

    assert not target_db.exists()


def test_legacy_key_file_allows_old_fingerprint(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_data = tmp_path / "source-data"
    _seed_database(source_db)
    result = backup(
        tmp_path / "backups",
        settings=_settings(source_db, source_data, "active-root-key"),
    )

    restore(
        result.archive_path,
        settings=_settings(tmp_path / "target.db", tmp_path / "target-data", "wrong"),
        legacy_key_files=[_write_key(tmp_path, "legacy.key", "old-root-key")],
    )

    assert (tmp_path / "target.db").exists()


def test_rotate_backups_keeps_newest_daily(tmp_path: Path) -> None:
    names = [
        "crewday-backup-20260427T030000Z.tar.zst",
        "crewday-backup-20260428T030000Z.tar.zst",
        "crewday-backup-20260429T030000Z.tar.zst",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"x")

    pruned = rotate_backups(tmp_path, keep_daily=1, keep_monthly=0)

    assert {path.name for path in pruned} == set(names[:2])
    assert (tmp_path / names[2]).exists()


def test_rotate_backups_rejects_negative_retention(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        rotate_backups(tmp_path, keep_daily=-1)


def test_pg_dump_uses_custom_format_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)

    backup_module._pg_dump(
        "postgresql+psycopg://crewday@example.test/db",
        tmp_path / "postgres.dump",
        snapshot="00000003-00000009-1",
    )

    assert calls == [
        [
            "pg_dump",
            "-Fc",
            "--file",
            str(tmp_path / "postgres.dump"),
            "--snapshot",
            "00000003-00000009-1",
            "postgresql+psycopg://crewday@example.test/db",
        ]
    ]


def test_postgres_restore_invokes_pg_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)
    manifest = BackupManifest(
        archive_version=1,
        created_at="2026-04-29T07:00:00+00:00",
        kind="postgres",
        database_member="db/postgres.dump",
        files_member="files",
        secret_envelopes_member="secret_envelopes.jsonl",
        row_counts={},
        secret_envelope_count=0,
        key_fps=[],
        current_key_fp=None,
        content_sha256="hash",
    )

    restored = backup_module._restore_database(
        manifest,
        tmp_path / "postgres.dump",
        database_url="postgresql+psycopg://crewday@example.test/db",
    )

    assert restored is None
    assert calls == [
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            "postgresql+psycopg://crewday@example.test/db",
            str(tmp_path / "postgres.dump"),
        ]
    ]


def _write_key(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    return path
