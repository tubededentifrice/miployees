"""Operational-log retention worker."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.domain.privacy import RetentionResult, rotate_operational_logs
from app.util.clock import Clock

__all__ = ["run_retention_rotation"]


def run_retention_rotation(
    *,
    data_dir: Path,
    clock: Clock,
) -> tuple[RetentionResult, ...]:
    """Archive and delete rows past each workspace's retention window."""
    with make_uow() as session:
        assert isinstance(session, Session)
        return rotate_operational_logs(session, data_dir=data_dir, clock=clock)
