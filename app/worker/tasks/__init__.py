"""Scheduler worker jobs for the tasks domain.

Houses the hourly ``generate_task_occurrences`` tick and (later) the
overdue-sweeper. Each job is a pure callable taking a
:class:`~app.tenancy.WorkspaceContext` and an injected session / clock
so tests can drive the body without touching APScheduler.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Generation" and
``docs/specs/01-architecture.md`` §"Worker".
"""
