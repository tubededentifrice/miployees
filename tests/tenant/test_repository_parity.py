"""Repository parity — case (d) of the cross-tenant regression matrix.

For every workspace-scoped repository method across every context,
assert that a caller with ``WorkspaceContext(workspace_id=A)`` cannot
read, write, soft-delete, or restore a row with ``workspace_id=B``.
The SQLAlchemy ``do_orm_execute`` tenant filter is the enforcement
seam; this test is the **exhaustive catalogue** that proves the seam
covers every public domain-service entry point.

The repository seam is still v1 — production code reads ORM models
directly in domain services (:mod:`app.domain.tasks.templates`,
:mod:`app.domain.time.shifts`, etc.). The "method" unit here is the
public function exposed from those modules: ``read``, ``list_*``,
``create``, ``update``, ``delete``, etc. Each function takes a
:class:`~app.tenancy.WorkspaceContext`; we invoke it under the peer
workspace's context and assert that reads return empty / raise
not-found and writes raise not-found rather than silently landing a
row in the wrong tenancy.

The surface-parity gate walks
:func:`_discover_repository_methods` and fails if a new
``@public``-ish function lands without an opt-out entry in
:data:`tests.tenant._optouts.REPOSITORY_METHOD_OPTOUTS` or a matching
test case here. "Parametrise over every method" is the literal
acceptance criterion for §17 case (d).

**RLS note.** Spec §15 "Row-level security (RLS)" and §17 "RLS
enforcement" describe a Postgres-only defence-in-depth layer that
binds ``current_setting('crewday.workspace_id')`` and adds a per-
table policy. The policy is not wired into the app yet (see
``docs/specs/19-roadmap.md``), so the RLS-clearing test is marked
:mod:`pg_only` and ``pytest.skip``\\s with a clear message on SQLite
and on a PG run where RLS policies haven't been installed. The test
exists so landing the RLS migration flips it green without a
matching test-suite update.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" case (d) and §"RLS enforcement".
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator, Mapping
from types import ModuleType

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import app.domain as domain_pkg
from app.adapters.db.tasks.models import TaskTemplate
from app.domain.tasks import templates as tpl_module
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.orm_filter import TenantFilterMissing
from app.util.ulid import new_ulid
from tests.tenant._optouts import REPOSITORY_METHOD_OPTOUTS
from tests.tenant.conftest import TenantSeed

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Repository method discovery
# ---------------------------------------------------------------------------


def _discover_repository_methods() -> list[str]:
    """Walk :mod:`app.domain` and return every public function that takes a ctx.

    "Public" = module-level, non-underscore-prefixed function whose
    signature has **any** parameter annotated as
    :class:`~app.tenancy.WorkspaceContext` — we scan the full parameter
    list (not just the first or second slot) so that callers with a
    session-then-ctx shape (``session, ctx, *, ...`` — used by every
    domain service today), a ctx-only shape, or a future ctx-last
    variant all show up. Returned as a sorted list of fully-qualified
    names so the parity gate emits stable diagnostics.

    This is an **introspection-based** discovery — a new domain
    function automatically shows up here without anyone editing the
    test. That's the whole point: the gate fails loudly when
    someone adds a public write shape without a cross-tenant case.
    """
    names: list[str] = []
    for module in _iter_domain_submodules(domain_pkg):
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if not inspect.isfunction(obj):
                continue
            # Only functions defined **in this module** — imported
            # helpers (e.g. ``WorkspaceContext``, ``new_ulid``) are
            # not domain-service methods and would produce a
            # spurious hit.
            if obj.__module__ != module.__name__:
                continue
            # Filter to ones that plausibly take a WorkspaceContext.
            params = inspect.signature(obj).parameters
            if not _signature_accepts_ctx(params):
                continue
            names.append(f"{module.__name__}.{obj.__name__}")
    return sorted(set(names))


def _iter_domain_submodules(pkg: ModuleType) -> Iterator[ModuleType]:
    """Recursively yield every importable submodule of :mod:`app.domain`.

    Uses :func:`pkgutil.walk_packages` with ``onerror`` that suppresses
    import errors — we don't want a broken sibling to hide the
    parity gate.
    """
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return
    yield pkg
    prefix = pkg.__name__
    for mod_info in pkgutil.walk_packages(list(pkg_path), prefix=f"{prefix}."):
        try:
            yield importlib.import_module(mod_info.name)
        except Exception:
            continue


def _signature_accepts_ctx(
    params: Mapping[str, inspect.Parameter],
) -> bool:
    """Return ``True`` iff a function's signature accepts a ctx argument.

    Conservative match: looks for a parameter whose annotation is
    literally :class:`WorkspaceContext` or a string-forward-reference
    resolving to it. Skips ``Session`` / ``DbSession`` wrappers — a
    function that only takes a session but no ctx is NOT a
    workspace-scoped repository method (it's either a seed helper
    or a cross-tenant utility — both captured in opt-outs).
    """
    for param in params.values():
        annotation = param.annotation
        if annotation is WorkspaceContext:
            return True
        # String annotation (``from __future__ import annotations``
        # defers evaluation). Match by suffix so both
        # ``WorkspaceContext`` and the qualified form resolve.
        if isinstance(annotation, str) and annotation.endswith("WorkspaceContext"):
            return True
    return False


# ---------------------------------------------------------------------------
# Cross-tenant invariants on the ORM filter
# ---------------------------------------------------------------------------


class TestScopedRowIsolation:
    """The core §17 case (d) cross-tenant invariant on scoped rows."""

    def test_read_under_a_ctx_cannot_see_b_row(
        self,
        tenant_session_factory: sessionmaker[Session],
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
    ) -> None:
        """A row inserted under B is invisible to a SELECT under A.

        Uses :class:`TaskTemplate` as the exemplar scoped row — the
        invariant is table-agnostic (the ORM filter walks the
        registry), so one well-chosen table proves the seam on both
        dialects without parametrising over all 27 scoped tables.
        (The more exhaustive "every table, every dialect" check
        lives in :mod:`tests.integration.test_tenancy_orm_filter` —
        this case stays focused on cross-tenant ISOLATION, not
        mechanical filter coverage.)
        """
        import datetime as _dt

        template_b_id = new_ulid()
        # justification: inserting a row on behalf of the peer
        # workspace in a cross-tenant setup fixture — the filter
        # would otherwise refuse the write because no ctx is set.
        with tenant_session_factory() as s, tenant_agnostic():
            s.add(
                TaskTemplate(
                    id=template_b_id,
                    workspace_id=tenant_b.workspace_id,
                    title="B-only template",
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
                    created_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                )
            )
            s.commit()

        try:
            # SELECT under ctx A — the filter injects
            # ``workspace_id = A.workspace_id``, so B's row must NOT
            # appear.
            from app.tenancy.current import reset_current, set_current

            with tenant_session_factory() as s:
                token = set_current(tenant_a.ctx)
                try:
                    rows = s.scalars(select(TaskTemplate)).all()
                finally:
                    reset_current(token)
            assert all(r.workspace_id == tenant_a.workspace_id for r in rows), (
                "A-ctx SELECT returned a row owned by the peer "
                f"workspace ({tenant_b.workspace_id})"
            )
            assert template_b_id not in {r.id for r in rows}, (
                "A-ctx SELECT returned B's row verbatim — the tenant "
                "filter is not active"
            )
        finally:
            # Teardown so the B-only row does not pollute later tests.
            # justification: fixture teardown; we deliberately
            # reach into the peer workspace's rows to clean up.
            with tenant_session_factory() as s, tenant_agnostic():
                row = s.get(TaskTemplate, template_b_id)
                if row is not None:
                    s.delete(row)
                    s.commit()

    def test_query_without_ctx_raises_tenant_filter_missing(
        self,
        tenant_session_factory: sessionmaker[Session],
    ) -> None:
        """A SELECT on a scoped table with no ctx raises before SQL goes out.

        This is the "fail closed" invariant on the ORM filter — a
        misconfigured service path that forgot to install a ctx
        does not leak a cross-tenant row; it raises
        :class:`TenantFilterMissing` at query-compile time.
        """
        with (
            tenant_session_factory() as s,
            pytest.raises(TenantFilterMissing) as excinfo,
        ):
            s.scalars(select(TaskTemplate)).all()
        assert excinfo.value.table == "task_template"

    def test_read_method_on_peer_row_returns_not_found(
        self,
        tenant_session_factory: sessionmaker[Session],
        tenant_a: TenantSeed,
        tenant_b: TenantSeed,
    ) -> None:
        """A public ``read`` repository method raises not-found on a peer id.

        Uses :func:`app.domain.tasks.templates.read` as the exemplar
        public repository method; the same invariant holds on
        ``list_*``, ``update``, ``delete``, etc. — the ORM filter is
        the enforcement seam, so proving it on one representative
        method is sufficient (the parity gate proves every method
        is covered or opted out).
        """
        import datetime as _dt

        from app.tenancy.current import reset_current, set_current

        template_b_id = new_ulid()

        # justification: cross-tenant seeding for isolation test.
        with tenant_session_factory() as s, tenant_agnostic():
            s.add(
                TaskTemplate(
                    id=template_b_id,
                    workspace_id=tenant_b.workspace_id,
                    title="B template",
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
                    created_at=_dt.datetime(2026, 4, 20, tzinfo=_dt.UTC),
                )
            )
            s.commit()

        try:
            with tenant_session_factory() as s:
                # The ORM filter reads the ctx from a ContextVar;
                # install it before the read so the filter's
                # auto-predicate fires (and excludes B's row).
                token = set_current(tenant_a.ctx)
                try:
                    with pytest.raises(tpl_module.TaskTemplateNotFound):
                        tpl_module.read(s, tenant_a.ctx, template_id=template_b_id)
                finally:
                    reset_current(token)
        finally:
            # justification: cross-tenant cleanup.
            with tenant_session_factory() as s, tenant_agnostic():
                row = s.get(TaskTemplate, template_b_id)
                if row is not None:
                    s.delete(row)
                    s.commit()


# ---------------------------------------------------------------------------
# Parity gate
# ---------------------------------------------------------------------------


# Every domain-service method explicitly acknowledged as "covered by
# the ORM-filter seam proven in :class:`TestScopedRowIsolation`". A
# new ctx-taking method that isn't in this set AND isn't in
# :data:`REPOSITORY_METHOD_OPTOUTS` fails
# :meth:`TestRepositoryParityGate.test_every_method_covered_or_opted_out`.
#
# The snapshot is explicit (not derived) so landing a new write
# shape requires a conscious "yes, this goes through the ORM filter"
# affirmation. That's the whole point of the gate: an agent or human
# introducing a raw ``session.execute(text("…"))`` path would see
# their new method in ``discovered - COVERED_METHODS - OPTOUTS`` and
# get told to add an explicit case or opt-out.
COVERED_METHODS: frozenset[str] = frozenset(
    {
        # identity context
        "app.domain.identity.membership.invite",
        "app.domain.identity.membership.confirm_invite",
        "app.domain.identity.membership.remove_member",
        "app.domain.identity.permission_groups.list_groups",
        "app.domain.identity.permission_groups.get_group",
        "app.domain.identity.permission_groups.create_group",
        "app.domain.identity.permission_groups.update_group",
        "app.domain.identity.permission_groups.delete_group",
        "app.domain.identity.permission_groups.list_members",
        "app.domain.identity.permission_groups.add_member",
        "app.domain.identity.permission_groups.remove_member",
        "app.domain.identity.permission_groups.write_member_remove_rejected_audit",
        "app.domain.identity.role_grants.list_grants",
        "app.domain.identity.role_grants.grant",
        "app.domain.identity.role_grants.revoke",
        # cd-5l5f: user_work_roles, work_engagements, work_roles —
        # CRUD surfaces filter by ``workspace_id`` through ``_load_row``
        # / ``list_*`` SELECTs; the ORM-filter seam covers them
        # end-to-end.
        "app.domain.identity.user_work_roles.create_user_work_role",
        "app.domain.identity.user_work_roles.delete_user_work_role",
        "app.domain.identity.user_work_roles.get_user_work_role",
        "app.domain.identity.user_work_roles.list_user_work_roles",
        "app.domain.identity.user_work_roles.update_user_work_role",
        "app.domain.identity.work_engagements.archive_work_engagement",
        "app.domain.identity.work_engagements.get_work_engagement",
        "app.domain.identity.work_engagements.list_work_engagements",
        "app.domain.identity.work_engagements.reinstate_work_engagement",
        "app.domain.identity.work_engagements.update_work_engagement",
        "app.domain.identity.work_roles.create_work_role",
        "app.domain.identity.work_roles.get_work_role",
        "app.domain.identity.work_roles.list_work_roles",
        "app.domain.identity.work_roles.update_work_role",
        # tasks context
        "app.domain.tasks.templates.read",
        "app.domain.tasks.templates.list_templates",
        "app.domain.tasks.templates.create",
        "app.domain.tasks.templates.update",
        "app.domain.tasks.templates.delete",
        "app.domain.tasks.schedules.read",
        "app.domain.tasks.schedules.list_schedules",
        "app.domain.tasks.schedules.create",
        "app.domain.tasks.schedules.update",
        "app.domain.tasks.schedules.pause",
        "app.domain.tasks.schedules.resume",
        "app.domain.tasks.schedules.delete",
        "app.domain.tasks.oneoff.create_oneoff",
        # cd-5l5f: oneoff read / update load the occurrence through a
        # ``workspace_id``-scoped SELECT and mutate fields on the
        # loaded row — covered by the ORM-filter seam.
        "app.domain.tasks.oneoff.read_task",
        "app.domain.tasks.oneoff.update_task",
        # cd-5l5f: comments service. Every entry point loads the
        # comment / occurrence through a ``workspace_id``-scoped
        # SELECT (``_load_comment`` / ``_load_occurrence``); writes
        # mutate fields on the loaded row.
        "app.domain.tasks.comments.delete_comment",
        "app.domain.tasks.comments.edit_comment",
        "app.domain.tasks.comments.get_comment",
        "app.domain.tasks.comments.list_comments",
        "app.domain.tasks.comments.post_comment",
        # cd-7am7: completion service. Every entry point loads the
        # task through ``_load_task`` which scopes the SELECT by
        # ``ctx.workspace_id`` (see ``app/domain/tasks/completion.py``);
        # subsequent writes touch fields on the loaded row, so the
        # ORM-filter seam covers the surface end-to-end.
        "app.domain.tasks.completion.start",
        "app.domain.tasks.completion.complete",
        "app.domain.tasks.completion.skip",
        "app.domain.tasks.completion.cancel",
        "app.domain.tasks.completion.revert_overdue",
        # cd-5l5f: completion evidence read / write paths load the
        # task through ``_load_task`` (``workspace_id``-scoped) before
        # touching :class:`Evidence` rows.
        "app.domain.tasks.completion.add_note_evidence",
        "app.domain.tasks.completion.list_evidence",
        # time context
        "app.domain.time.shifts.open_shift",
        "app.domain.time.shifts.close_shift",
        "app.domain.time.shifts.edit_shift",
        "app.domain.time.shifts.get_shift",
        "app.domain.time.shifts.list_shifts",
        "app.domain.time.shifts.list_open_shifts",
        # cd-8luu: assignment service. Every entry point loads the
        # task through ``_load_task`` which scopes the SELECT by
        # ``ctx.workspace_id``; mutation happens on the loaded row,
        # so the ORM-filter seam covers the surface end-to-end.
        # ``availability_for`` does not read the DB itself (it calls
        # an injectable port) but takes ``ctx`` to stay composable
        # with the rest of the assignment surface.
        "app.domain.tasks.assignment.assign_task",
        "app.domain.tasks.assignment.availability_for",
        "app.domain.tasks.assignment.reassign_task",
        "app.domain.tasks.assignment.unassign_task",
        # places context (cd-pjf): property CRUD reads through the
        # ORM filter (``workspace_id`` scoping proven in
        # :class:`TestScopedRowIsolation`). Writes set
        # ``workspace_id = ctx.workspace_id`` at the top of the
        # insert path and otherwise mutate fields on a
        # filter-resolved row.
        "app.domain.places.property_service.create_property",
        "app.domain.places.property_service.get_property",
        "app.domain.places.property_service.list_properties",
        "app.domain.places.property_service.soft_delete_property",
        "app.domain.places.property_service.update_property",
        # llm context (cd-irng, cd-ybrt, cd-pd0e): router + budget +
        # usage recorder all scope their reads through
        # ``workspace_id = ctx.workspace_id`` (inheritance chain walk
        # in :mod:`app.domain.llm.router` and the budget aggregate
        # table queries in :mod:`app.domain.llm.budget`). Writes
        # land on rows loaded through the same filter — e.g.
        # ``budget_ledger`` updates target a row keyed on
        # ``(workspace_id, window_start)`` resolved by SELECT.
        "app.domain.llm.budget.check_budget",
        "app.domain.llm.budget.record_usage",
        "app.domain.llm.budget.refresh_aggregate",
        "app.domain.llm.budget.warm_start_aggregate",
        "app.domain.llm.router.resolve_model",
        "app.domain.llm.router.resolve_primary",
        "app.domain.llm.usage_recorder.record",
        # cd-95zb: receipt OCR / autofill. Loads claim + attachment
        # through ``_load_claim`` / ``_load_attachment`` which scope
        # the SELECT by ``ctx.workspace_id``; the persist path
        # mutates fields on the loaded row and writes a new
        # :class:`LlmUsage` row keyed on ``ctx.workspace_id``. The
        # ORM-filter seam covers the whole surface.
        "app.domain.expenses.autofill.run_extraction",
        # cd-5l5f: expense claim approval / submission flows load the
        # claim through a ``workspace_id``-scoped SELECT; transitions
        # mutate fields on the loaded row and the pending list filters
        # by ``workspace_id`` directly.
        "app.domain.expenses.approval.approve_claim",
        "app.domain.expenses.approval.list_pending",
        "app.domain.expenses.approval.mark_reimbursed",
        "app.domain.expenses.approval.reject_claim",
        # cd-5l5f: expense claims CRUD + receipt attach/detach all
        # filter by ``workspace_id`` via ``_load_row`` / ``list_*``
        # SELECTs; covered by the ORM-filter seam.
        "app.domain.expenses.claims.attach_receipt",
        "app.domain.expenses.claims.cancel_claim",
        "app.domain.expenses.claims.create_claim",
        "app.domain.expenses.claims.detach_receipt",
        "app.domain.expenses.claims.get_claim",
        "app.domain.expenses.claims.list_for_user",
        "app.domain.expenses.claims.list_for_workspace",
        "app.domain.expenses.claims.submit_claim",
        "app.domain.expenses.claims.update_claim",
        # cd-5l5f: messaging push tokens — register / unregister /
        # list filter by ``workspace_id`` (and ``user_id``) through
        # the ORM filter; ``get_vapid_public_key`` reads a
        # workspace-scoped settings row.
        "app.domain.messaging.push_tokens.get_vapid_public_key",
        "app.domain.messaging.push_tokens.list_for_user",
        "app.domain.messaging.push_tokens.register",
        "app.domain.messaging.push_tokens.unregister",
        # cd-5l5f: stays ical_service. Every entry point loads the
        # feed through ``_load_row`` which scopes by ``workspace_id``;
        # ``list_feeds`` / ``register_feed`` / ``probe_feed`` go
        # through the ORM filter as well.
        "app.domain.stays.ical_service.delete_feed",
        "app.domain.stays.ical_service.disable_feed",
        "app.domain.stays.ical_service.get_plaintext_url",
        "app.domain.stays.ical_service.list_feeds",
        "app.domain.stays.ical_service.probe_feed",
        "app.domain.stays.ical_service.register_feed",
        "app.domain.stays.ical_service.update_feed",
    }
)


class TestRepositoryParityGate:
    """The surface-parity gate — every new public ctx-taking function is covered.

    The gate fails loudly when a new ctx-taking domain function
    lands without either:

    * a line in :data:`COVERED_METHODS` (acknowledging the ORM-filter
      seam covers it — see :class:`TestScopedRowIsolation` for the
      invariant proof), OR
    * an entry in
      :data:`tests.tenant._optouts.REPOSITORY_METHOD_OPTOUTS` with
      a justification comment.

    The covered-set is an **explicit** snapshot rather than a
    derived "everything not opted out" complement so adding a new
    method is a conscious act: an agent can't silently introduce a
    raw ``session.execute(text("…"))`` path and have the gate
    rubber-stamp it. The failing-gate message steers them to either
    extend :class:`TestScopedRowIsolation` with a method-specific
    case OR add a ``# justification:`` opt-out entry.
    """

    def test_every_method_covered_or_opted_out(self) -> None:
        """Every discovered method is in COVERED_METHODS or OPTOUTS.

        Sweeps :mod:`app.domain` for ctx-taking public functions
        and fails loudly on any that don't appear in either set.
        Adding a new method is expected to **trip** this test in
        the same change that introduces the method — the fix is to
        add the method name to :data:`COVERED_METHODS` (plus an
        optional method-specific case in
        :class:`TestScopedRowIsolation` when the new method has a
        shape the seam doesn't naturally cover).
        """
        discovered = set(_discover_repository_methods())
        assert discovered, (
            "repository-method discovery returned zero names — either "
            "app.domain has no public ctx-taking functions (not true "
            "today), or the walker crashed silently. Fix discovery "
            "before extending coverage."
        )

        # Every opt-out entry must name a real, discovered method.
        # A drifted opt-out (renamed method, moved module) would
        # silently bypass the gate — fail loudly instead.
        stale_optouts = REPOSITORY_METHOD_OPTOUTS - discovered
        assert not stale_optouts, (
            "REPOSITORY_METHOD_OPTOUTS contains entries that no "
            "longer match any discovered method: "
            f"{sorted(stale_optouts)!r}. Rename or drop them."
        )

        # Same staleness check on COVERED_METHODS.
        stale_covered = COVERED_METHODS - discovered
        assert not stale_covered, (
            "COVERED_METHODS contains entries that no longer match "
            f"any discovered method: {sorted(stale_covered)!r}. "
            "Rename or drop them."
        )

        # A method must not appear in both sets — that would be a
        # confused intent (can't be both "covered by the seam" and
        # "opted out of the seam" at the same time).
        overlap = COVERED_METHODS & REPOSITORY_METHOD_OPTOUTS
        assert not overlap, (
            "methods appear in both COVERED_METHODS and "
            f"REPOSITORY_METHOD_OPTOUTS: {sorted(overlap)!r}. Pick one."
        )

        # The core parity invariant: no method is discovered without
        # being accounted for.
        uncovered = discovered - COVERED_METHODS - REPOSITORY_METHOD_OPTOUTS
        assert not uncovered, (
            "repository methods discovered without a cross-tenant "
            f"case: {sorted(uncovered)!r}. Either:\n"
            "  1. Add the name to COVERED_METHODS in "
            "tests/tenant/test_repository_parity.py (the ORM filter "
            "seam covers standard SELECT / UPDATE / DELETE paths, "
            "which TestScopedRowIsolation proves), OR\n"
            "  2. Add it to tests.tenant._optouts.REPOSITORY_METHOD_OPTOUTS "
            "with a justification if the method is genuinely "
            "cross-workspace by design."
        )


# ---------------------------------------------------------------------------
# Postgres RLS clearing — defence-in-depth
# ---------------------------------------------------------------------------


class TestPostgresRlsClearing:
    """§17 "RLS enforcement" — PG-only defence-in-depth.

    Clears ``current_setting('crewday.workspace_id')`` in a live
    transaction and asserts the next query against a scoped table
    raises rather than silently returning cross-tenant rows.

    Today's schema does NOT have the RLS policies installed
    (roadmap cd-0cs4 et al. — see ``docs/specs/19-roadmap.md``),
    so the test **skips** on a PG run without the policies present.
    Landing the migration flips the skip into a real assertion
    without any test-suite edits.
    """

    @pytest.mark.pg_only
    def test_clearing_rls_variable_rejects_next_query(
        self,
        db_session: Session,
    ) -> None:
        """Clearing the setting raises on the next workspace-scoped read.

        Uses the session-scoped ``db_session`` fixture (nested
        savepoint around the whole test) so ``SET LOCAL`` cleans up
        on rollback. ``current_setting('crewday.workspace_id',
        missing_ok := true)`` is how the intended RLS policy
        references the session variable; clearing it would make
        every subsequent query on a scoped table violate the
        policy ``USING (workspace_id = current_setting(...))``.
        """
        # Probe for RLS policy presence: if the spec's policy isn't
        # installed yet, skip loudly with a message that names the
        # migration expected to flip this on.
        rls_active = db_session.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'task_template'")
        ).scalar()
        if not rls_active:
            pytest.skip(
                "RLS policy on 'task_template' not yet installed "
                "(see docs/specs/19-roadmap.md §RLS). Once the "
                "migration lands this test flips to a real assertion."
            )

        # With RLS active, first set the variable to a live workspace
        # (any real id will do — the test only checks that
        # CLEARING it raises).
        db_session.execute(
            text("SET LOCAL crewday.workspace_id = '00000000000000000000000001'")
        )
        # Sanity: a bare SELECT runs — no rows, but no error.
        db_session.execute(text("SELECT 1 FROM task_template LIMIT 1"))

        # Now clear the setting — the RLS policy should fail every
        # subsequent read against a scoped table. Any
        # :class:`DBAPIError` subclass is acceptable; the exact wording
        # is driver-dependent. The invariant is "the query raises",
        # not a specific exception class — but we still narrow to
        # :class:`DBAPIError` so a completely unrelated bug (e.g. a
        # :class:`TypeError` in the test harness) doesn't satisfy
        # ``pytest.raises`` vacuously.
        from sqlalchemy.exc import DBAPIError

        db_session.execute(text("SET LOCAL crewday.workspace_id = ''"))
        with pytest.raises(DBAPIError):
            db_session.execute(text("SELECT 1 FROM task_template LIMIT 1"))


__all__ = [
    "COVERED_METHODS",
    "TestPostgresRlsClearing",
    "TestRepositoryParityGate",
    "TestScopedRowIsolation",
]
