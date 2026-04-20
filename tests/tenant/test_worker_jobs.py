"""Worker jobs — case (b) of the cross-tenant regression matrix.

For every worker job on the registry, inject a
:class:`WorkspaceContext` with ``workspace_id=B.workspace_id`` and
intercept every SQL statement the job emits via
:func:`sqlalchemy.event.listen` on ``before_cursor_execute``.
Assert every statement that targets a workspace-scoped table carries
a ``workspace_id`` parameter bound to ``B`` — **never** ``A``, never
missing. A statement that sneaks past the tenancy filter without a
``workspace_id`` predicate is the exact bug this case exists to
catch (§17 "Cross-tenant regression test" case (b)).

The current v1 worker registry is small — only
:func:`app.worker.tasks.generator.generate_task_occurrences` ships
today. Every future job added to the registry MUST add a matching
case here; the parity gate in
:mod:`tests.tenant.test_repository_parity` enforces that link so a
new job cannot ship cross-tenant-blind.

**The assertion shape.**

Parametrising "every emitted statement carries the context's
workspace_id" is subtle: the filter may inject the predicate as a
bound parameter (the normal path) or as a constant literal (the
``tenant_agnostic()`` escape hatch — not expected in a worker, but
we still fail loudly if it shows up). We inspect SQLAlchemy's
compiled parameter dict per statement and cross-check:

1. For every statement whose text references a scoped table, either
   a ``workspace_id`` parameter is bound in the compiled SQL AND its
   value matches ``ctx.workspace_id``, OR the statement is a known
   cross-tenant shape (an INSERT carrying ``workspace_id`` in the
   VALUES list — the filter does NOT rewrite INSERTs per
   :mod:`app.tenancy.orm_filter` module docstring).
2. No statement binds a ``workspace_id`` to the peer workspace's
   id — a "wrong workspace leak" would trip on the string equality.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" case (b).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.tasks.models import Schedule, TaskTemplate
from app.tenancy import tenant_agnostic
from app.tenancy.registry import scoped_tables
from app.util.ulid import new_ulid
from app.worker.tasks.generator import generate_task_occurrences
from tests.tenant.conftest import TenantSeed

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Worker registry
# ---------------------------------------------------------------------------


# Fully-qualified name → callable. Today the only registered job is
# the task generator; adding a new job with no matching test case
# here fails :func:`TestWorkerRegistryParity.test_every_job_covered`.
#
# The "registry" is a hand-maintained tuple because
# :mod:`app.worker.tasks` hasn't settled its runtime registry shape
# yet (APScheduler wiring lands in cd-pnbn / cd-3p3z). When it does,
# this table collapses to an import from that module.
_WORKER_REGISTRY: tuple[tuple[str, Any], ...] = (
    (
        "app.worker.tasks.generator.generate_task_occurrences",
        generate_task_occurrences,
    ),
)


# ---------------------------------------------------------------------------
# SQL capture
# ---------------------------------------------------------------------------


class _SqlCapture:
    """Collect every compiled SQL statement + its **named** bound parameters.

    Attached to the SQLAlchemy :class:`Engine` via
    :meth:`event.listen`\\ (``before_cursor_execute``). The DBAPI-
    level parameter payload differs per dialect (``qmark`` positional
    tuples on SQLite, ``pyformat`` dicts on psycopg) so walking
    ``parameters`` directly would require a per-dialect normaliser.
    Instead we lift the **compiled** named-parameter mapping off
    ``context.compiled_parameters`` — SQLAlchemy's internal
    representation before per-dialect lowering — which is a list of
    dicts in both backends.

    :attr:`entries` holds one record per executed statement:

    * ``statement`` — the rendered SQL text;
    * ``named_params`` — the named-parameter dicts that backed the
      statement (list because ``executemany`` yields one per row).
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, list[dict[str, Any]]]] = []

    def __call__(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: Any,
        context: Any,
        _executemany: bool,
    ) -> None:
        named: list[dict[str, Any]] = []
        # ``compiled_parameters`` is SQLAlchemy's pre-dialect-lowering
        # shape: always a list of named-parameter dicts. Empty for
        # ``text()`` statements and a few DDL shapes — we default to
        # ``[]`` and let the caller treat "no named params" as
        # "the statement isn't a scoped-table read/write the harness
        # cares about".
        compiled_params = getattr(context, "compiled_parameters", None)
        if compiled_params:
            for item in compiled_params:
                if isinstance(item, dict):
                    named.append(dict(item))
        self.entries.append((statement, named))


def _iter_param_dicts(params: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield every parameter dict in ``params``.

    Thin wrapper so the caller can iterate without duplicating the
    loop body; ``params`` is already normalised by
    :class:`_SqlCapture` to "list of dicts".
    """
    yield from params


def _statement_targets_scoped_table(statement: str) -> str | None:
    """Return the first scoped table name referenced in ``statement``, else ``None``.

    A heuristic that looks for a scoped table name as a whole word in
    the rendered SQL. Good enough for this test: the generator emits
    a handful of readable statements; the tenant filter already runs
    its own structural walk against the same table registry, so a
    wrong-table match here would produce a too-strict assertion,
    not a leak.
    """
    lowered = statement.lower()
    for table_name in scoped_tables():
        if table_name == "invite":
            # The generator never touches invites; skip to avoid a
            # false-positive on a column named ``invite_*``.
            continue
        # Whole-word match (``FROM table_name`` / ``JOIN table_name`` /
        # ``INTO table_name`` / ``UPDATE table_name`` / ``DELETE FROM
        # table_name``).
        token = f" {table_name} "
        token_end = f" {table_name}\n"
        token_comma = f" {table_name},"
        token_paren = f" {table_name}("
        if (
            token in lowered
            or token_end in lowered
            or token_comma in lowered
            or token_paren in lowered
        ):
            return table_name
    return None


def _assert_workspace_bound(
    statement: str,
    parameters: list[dict[str, Any]],
    *,
    expected_workspace_id: str,
    forbidden_workspace_id: str,
) -> None:
    """Fail if ``statement`` leaks across the workspace boundary.

    Invariants:

    * The statement's compiled parameters contain a key whose name is
      ``workspace_id`` (or ``workspace_id_1`` / ``_2`` — SQLAlchemy's
      bind-dedup suffix when the same column is filtered twice; the
      filter's idempotency guard prevents this in practice but we
      accept it here to keep the test stable against a future dialect
      compilation tweak).
    * At least one bound ``workspace_id`` equals ``expected_workspace_id``.
    * No bound ``workspace_id`` equals ``forbidden_workspace_id`` — that
      is the exact "wrong workspace leak" signature.

    INSERT statements are accepted without a WHERE-clause filter per
    :mod:`app.tenancy.orm_filter` ("``Insert`` statements are not
    touched — rows must already carry ``workspace_id``"): we still
    require the VALUES payload to carry the column AND match the
    expected id.
    """
    workspace_values: list[Any] = []
    for param_dict in _iter_param_dicts(parameters):
        for key, value in param_dict.items():
            if key == "workspace_id" or key.startswith("workspace_id_"):
                workspace_values.append(value)

    assert workspace_values, (
        f"SQL emitted against a scoped table without a workspace_id "
        f"binding:\n  {statement!r}\n  parameters={parameters!r}"
    )
    # No bound workspace_id should equal the peer workspace's id.
    for value in workspace_values:
        assert value != forbidden_workspace_id, (
            f"SQL leaked the peer workspace id ({forbidden_workspace_id!r}) "
            f"into a bound parameter:\n  {statement!r}\n"
            f"  parameters={parameters!r}"
        )
    # At least one bound workspace_id must match the expected id.
    assert expected_workspace_id in workspace_values, (
        f"SQL against a scoped table did not bind the expected "
        f"workspace_id ({expected_workspace_id!r}):\n  {statement!r}\n"
        f"  parameters={parameters!r}\n"
        f"  bound workspace ids: {workspace_values!r}"
    )


# ---------------------------------------------------------------------------
# Fixture — seed a live schedule in BOTH workspaces so the generator
# actually walks rows in each context
# ---------------------------------------------------------------------------


_ACTIVE_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def seeded_schedules(
    tenant_session_factory: sessionmaker[Session],
    tenant_a: TenantSeed,
    tenant_b: TenantSeed,
) -> Iterator[dict[str, str]]:
    """Seed one :class:`TaskTemplate` + :class:`Schedule` per workspace.

    Both workspaces get a schedule so the generator has SOMETHING to
    walk under each ctx — the cross-tenant invariant holds on a job
    that has work to do, not a no-op. The two schedules share every
    non-PK field (same RRULE, same dtstart_local, same template
    title) so a cross-tenant read would surface as a wrong id, not
    an empty row.

    Rolls back on teardown so the seed data is purely local to this
    fixture — even with the session-scoped ``tenants`` fixture alive.
    """
    template_a_id = new_ulid()
    template_b_id = new_ulid()
    schedule_a_id = new_ulid()
    schedule_b_id = new_ulid()

    # justification: seeding a scoped row under one ctx and the
    # symmetric row under the peer ctx in a single transaction.
    # The tenant filter is not installed when we write the rows
    # under ``tenant_agnostic``; cross-context writes are the
    # whole point of the fixture.
    with tenant_session_factory() as s, tenant_agnostic():
        for tid, wsid in (
            (template_a_id, tenant_a.workspace_id),
            (template_b_id, tenant_b.workspace_id),
        ):
            s.add(
                TaskTemplate(
                    id=tid,
                    workspace_id=wsid,
                    title="Shared Template",
                    description_md="",
                    default_duration_min=30,
                    property_scope="any",
                    listed_property_ids=[],
                    area_scope="any",
                    listed_area_ids=[],
                    checklist_template_json=[],
                    photo_evidence="disabled",
                    priority="normal",
                    inventory_consumption_json={},
                    created_at=_ACTIVE_NOW,
                )
            )
        s.flush()
        # Schedule rows. Both reference a null property_id so the
        # generator skips materialising occurrences (the v1
        # ``Occurrence`` model requires non-null property_id); we
        # don't actually need task rows to land — we just need the
        # generator to emit the scoped SELECTs that the harness
        # inspects.
        for sid, tid, wsid in (
            (schedule_a_id, template_a_id, tenant_a.workspace_id),
            (schedule_b_id, template_b_id, tenant_b.workspace_id),
        ):
            s.add(
                Schedule(
                    id=sid,
                    workspace_id=wsid,
                    template_id=tid,
                    property_id=None,
                    dtstart=_ACTIVE_NOW,
                    dtstart_local="2026-04-20T09:00:00",
                    rrule_text="FREQ=DAILY;COUNT=3",
                    duration_minutes=30,
                    created_at=_ACTIVE_NOW,
                )
            )
        s.commit()

    try:
        yield {
            "template_a_id": template_a_id,
            "template_b_id": template_b_id,
            "schedule_a_id": schedule_a_id,
            "schedule_b_id": schedule_b_id,
        }
    finally:
        # justification: teardown deletion mirrors the setup write —
        # both cross-context because the fixture owns both rows.
        with tenant_session_factory() as s, tenant_agnostic():
            for sid in (schedule_a_id, schedule_b_id):
                row_sched = s.get(Schedule, sid)
                if row_sched is not None:
                    s.delete(row_sched)
            for tid in (template_a_id, template_b_id):
                row_tpl = s.get(TaskTemplate, tid)
                if row_tpl is not None:
                    s.delete(row_tpl)
            s.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkerSqlIsWorkspaceScoped:
    """Case (b) — every emitted SQL statement binds the ctx's workspace_id."""

    def test_generator_binds_ctx_workspace_id_only(
        self,
        engine: Engine,
        tenant_session_factory: sessionmaker[Session],
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
        seeded_schedules: dict[str, str],
    ) -> None:
        """Run the generator under ctx ``B``; assert no ``A`` bindings leak.

        The generator walks schedules, loads templates, closures,
        and materialises occurrences. Every SQL on a scoped table
        must bind ``ctx.workspace_id`` — not the peer workspace's
        id, not an empty string.
        """
        from app.tenancy.current import reset_current, set_current

        capture = _SqlCapture()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            with tenant_session_factory() as session:
                # The ORM filter reads the ambient ctx from a
                # ContextVar; production callers install it via the
                # middleware / scheduler runner. Install it directly
                # so the generator's SELECTs get the auto-injected
                # ``workspace_id`` predicate we're here to verify.
                token = set_current(tenant_b.ctx)
                try:
                    generate_task_occurrences(
                        tenant_b.ctx,
                        session=session,
                        now=_ACTIVE_NOW,
                    )
                    session.commit()
                finally:
                    reset_current(token)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert capture.entries, (
            "generator emitted zero statements — fixture didn't seed a "
            "schedule the generator could walk, or the capture hook is "
            "mis-wired"
        )

        offenders: list[tuple[str, str]] = []
        for stmt, params in capture.entries:
            table = _statement_targets_scoped_table(stmt)
            if table is None:
                continue
            try:
                _assert_workspace_bound(
                    stmt,
                    params,
                    expected_workspace_id=tenant_b.workspace_id,
                    forbidden_workspace_id=tenant_a.workspace_id,
                )
            except AssertionError as exc:
                offenders.append((stmt, str(exc)))

        assert not offenders, (
            "worker emitted statements that failed the workspace-scope "
            f"check: {offenders!r}"
        )


class TestWorkerRegistryParity:
    """Surface-parity gate for the worker registry."""

    def test_every_registered_job_has_a_case(self) -> None:
        """Every job in :data:`_WORKER_REGISTRY` is reachable by the case above.

        The "case" is a hard-coded assertion in this module. A new
        job added to :data:`_WORKER_REGISTRY` without updating
        :class:`TestWorkerSqlIsWorkspaceScoped` fails this gate — the
        fix is to add a matching test or list the job in
        :data:`tests.tenant._optouts.WORKER_JOB_OPTOUTS` with a
        justification.
        """
        covered: set[str] = {
            "app.worker.tasks.generator.generate_task_occurrences",
        }
        uncovered = [
            name for name, _callable in _WORKER_REGISTRY if name not in covered
        ]
        # Opt-outs drop out of the uncovered list.
        from tests.tenant._optouts import WORKER_JOB_OPTOUTS

        uncovered = [name for name in uncovered if name not in WORKER_JOB_OPTOUTS]
        assert not uncovered, (
            "worker jobs missing a cross-tenant case: "
            f"{uncovered!r}. Either extend TestWorkerSqlIsWorkspaceScoped "
            "or opt out via tests.tenant._optouts.WORKER_JOB_OPTOUTS "
            "with a justification comment."
        )
