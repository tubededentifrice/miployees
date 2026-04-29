"""Tests for host-only ``crewday admin`` overrides."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from crewday._overrides import admin


def test_admin_init_demo_refusal_happens_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demo mode must refuse before the command touches the database."""
    migrations_called = False

    def run_migrations() -> None:
        nonlocal migrations_called
        migrations_called = True

    monkeypatch.setattr(
        admin,
        "_load_app_admin",
        lambda: SimpleNamespace(
            ADMIN_DEMO_REFUSAL="admin commands not available in demo"
        ),
    )
    monkeypatch.setattr(admin, "_make_uow", lambda: object())
    monkeypatch.setattr(admin, "_settings", lambda: SimpleNamespace(demo_mode=True))
    monkeypatch.setattr(admin, "_run_migrations", run_migrations)

    result = CliRunner().invoke(admin.init, [])

    assert result.exit_code == 5
    assert "admin commands not available in demo" in result.output
    assert migrations_called is False


def test_admin_backup_outputs_archive_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin,
        "_load_app_admin",
        lambda: SimpleNamespace(ADMIN_DEMO_REFUSAL="demo"),
    )
    monkeypatch.setattr(
        admin,
        "_settings",
        lambda: SimpleNamespace(demo_mode=False),
    )
    monkeypatch.setattr(
        admin,
        "_load_app_backup",
        lambda: SimpleNamespace(
            backup=lambda out_dir, *, settings, keep_daily, keep_monthly: (
                SimpleNamespace(
                    archive_path=out_dir / "crewday-backup-test.tar.zst",
                    manifest=SimpleNamespace(
                        kind="sqlite",
                        content_sha256="abc",
                        row_counts={"user": 1},
                        secret_envelope_count=0,
                    ),
                    pruned=[],
                )
            )
        ),
    )

    result = CliRunner().invoke(admin.backup, ["--to", "/tmp/backups"])

    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["archive_path"] == "/tmp/backups/crewday-backup-test.tar.zst"
    assert body["kind"] == "sqlite"


def test_admin_backup_rejects_negative_retention() -> None:
    result = CliRunner().invoke(
        admin.backup,
        ["--to", "/tmp/backups", "--keep-daily", "-1"],
    )

    assert result.exit_code == 2
    assert "Invalid value for '--keep-daily'" in result.output


def test_admin_restore_runs_migrations_after_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle.tar.zst"
    bundle.write_bytes(b"bundle")
    migrations_called = False

    def run_migrations() -> None:
        nonlocal migrations_called
        migrations_called = True

    monkeypatch.setattr(
        admin,
        "_load_app_admin",
        lambda: SimpleNamespace(ADMIN_DEMO_REFUSAL="demo"),
    )
    monkeypatch.setattr(admin, "_settings", lambda: SimpleNamespace(demo_mode=False))
    monkeypatch.setattr(admin, "_run_migrations", run_migrations)
    monkeypatch.setattr(
        admin,
        "_load_app_backup",
        lambda: SimpleNamespace(
            restore=lambda bundle, *, settings, legacy_key_files: SimpleNamespace(
                manifest=SimpleNamespace(kind="sqlite", content_sha256="abc"),
                restored_database=tmp_path / "restored.db",
                restored_files=tmp_path / "files",
            )
        ),
    )

    result = CliRunner().invoke(admin.restore, ["--from", str(bundle)])

    assert result.exit_code == 0
    assert migrations_called is True
    body = json.loads(result.output)
    assert body["restored_database"] == str(tmp_path / "restored.db")
