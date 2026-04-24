"""Worker — APScheduler jobs and background task runners.

Two entry-points share the same :func:`register_jobs` seam
(§16 "Worker process"):

* Inline inside the FastAPI factory's lifespan — the default for
  single-container deployments (Recipe A). Gated on
  ``settings.worker == "internal"``.
* Standalone via ``python -m app.worker`` — the separate
  ``worker`` container in Recipes B and D. See
  :mod:`app.worker.__main__`.

The public seam downstream tasks import from is:

* :func:`register_jobs` — call once per scheduler to install every
  job this deployment ships.
* :func:`create_scheduler` / :func:`start` / :func:`stop` — build
  + manage the scheduler itself; the standalone entrypoint and the
  factory lifespan both go through these.

Individual jobs live under ``app/worker/tasks/`` — each one a pure
callable taking its dependencies as arguments so tests can drive
the body without touching APScheduler. The scheduler wrapper
(:func:`app.worker.scheduler.wrap_job`) handles per-tick session
scope, heartbeat upsert, and exception containment uniformly.

See ``docs/specs/01-architecture.md`` §"Worker",
``docs/specs/16-deployment-operations.md`` §"Worker process", and
``docs/specs/06-tasks-and-scheduling.md`` §"Generation".
"""

from __future__ import annotations

from app.worker.scheduler import (
    GENERATOR_JOB_ID,
    HEARTBEAT_JOB_ID,
    HEARTBEAT_JOB_INTERVAL_SECONDS,
    IDEMPOTENCY_SWEEP_JOB_ID,
    create_scheduler,
    register_jobs,
    registered_job_ids,
    start,
    stop,
    wrap_job,
)

__all__ = [
    "GENERATOR_JOB_ID",
    "HEARTBEAT_JOB_ID",
    "HEARTBEAT_JOB_INTERVAL_SECONDS",
    "IDEMPOTENCY_SWEEP_JOB_ID",
    "create_scheduler",
    "register_jobs",
    "registered_job_ids",
    "start",
    "stop",
    "wrap_job",
]
