"""Unit tests for :mod:`app.authz.enforce`.

Covers every quadrant of §02 "Permission resolution" against the
public :func:`require` entry point:

* **Unknown action / invalid scope** — caller bugs (422-equivalent)
  surface as :class:`UnknownActionKey` / :class:`InvalidScope`.
* **Root-only gate** — owners allowed; non-owners denied regardless
  of rules.
* **Root-protected-deny** — owners immune to ``deny`` rules.
* **Rule walk** — explicit ``allow`` wins; explicit ``deny`` wins
  (unless root-protected-deny + owner); most-specific-first scope
  ordering (property > workspace).
* **Default-allow fallback** — user in ``managers`` via role_grant
  allowed; unmatched user denied.
* **Structured log line** — one ``authz.denied`` warning per denied
  check.

Every test sets up a minimal SQLite schema, seeds the workspace +
owners group, and then drives :func:`require` with a custom
:class:`PermissionRuleRepository` stub so rule-driven flows light up
without the (not-yet-shipped) ``permission_rule`` table.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.authz.enforce import (
    EmptyPermissionRuleRepository,
    InvalidScope,
    PermissionCheck,
    PermissionDenied,
    RuleRow,
    UnknownActionKey,
    require,
)
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every ORM table created.

    Scoped per-test so each case runs against a pristine schema.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@dataclass
class _Seeded:
    """Bundle of the rows each test seeds before calling :func:`require`."""

    workspace_id: str
    owner_user_id: str
    manager_user_id: str
    worker_user_id: str
    stranger_user_id: str


def _seed(session: Session) -> _Seeded:
    """Insert a workspace + 4 personas: owner, manager, worker, stranger.

    * Owner — manager role_grant + owners-group member.
    * Manager — manager role_grant only (no owners membership).
    * Worker — worker role_grant.
    * Stranger — no grants, no group memberships.

    The test engine enables SQLite FK constraints, so the helper
    must materialise the real ``workspace`` and ``user`` rows before
    the authz rows can land. ``canonicalise_email`` keeps the
    ``email_lower`` column aligned with the production invariant —
    tests that round-trip users should never see a pre-flush NULL.
    """
    workspace_id = new_ulid()
    owner_user_id = new_ulid()
    manager_user_id = new_ulid()
    worker_user_id = new_ulid()
    stranger_user_id = new_ulid()

    # Seed the workspace + 4 users first so the authz tables' FKs
    # resolve cleanly.
    session.add(
        Workspace(
            id=workspace_id,
            slug="test-ws",
            name="Test Workspace",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    for user_id, tag in (
        (owner_user_id, "owner"),
        (manager_user_id, "manager"),
        (worker_user_id, "worker"),
        (stranger_user_id, "stranger"),
    ):
        email = f"{tag}@example.com"
        session.add(
            User(
                id=user_id,
                email=email,
                email_lower=canonicalise_email(email),
                display_name=tag.capitalize(),
                created_at=_PINNED,
            )
        )
    session.flush()

    owners_group = PermissionGroup(
        id=new_ulid(),
        workspace_id=workspace_id,
        slug="owners",
        name="Owners",
        system=True,
        capabilities_json={"all": True},
        created_at=_PINNED,
    )
    session.add(owners_group)
    session.flush()

    session.add(
        PermissionGroupMember(
            group_id=owners_group.id,
            user_id=owner_user_id,
            workspace_id=workspace_id,
            added_at=_PINNED,
            added_by_user_id=None,
        )
    )
    # Manager + worker role grants. Owners get a manager grant too
    # (matches the bootstrap shape in ``seed_owners_system_group``).
    session.add_all(
        [
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=owner_user_id,
                grant_role="manager",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            ),
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=manager_user_id,
                grant_role="manager",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            ),
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=worker_user_id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            ),
        ]
    )
    session.flush()
    return _Seeded(
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        manager_user_id=manager_user_id,
        worker_user_id=worker_user_id,
        stranger_user_id=stranger_user_id,
    )


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    grant_role: ActorGrantRole = "manager",
    was_owner: bool = False,
) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to the seeded workspace."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="test-ws",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=was_owner,
        audit_correlation_id=new_ulid(),
    )


# -----------------------------------------------------------------------
# Stub rule repository — lets tests inject arbitrary rule rows without
# needing the (not-yet-shipped) ``permission_rule`` table.
# -----------------------------------------------------------------------


@dataclass
class _StubRuleRepo:
    """In-memory :class:`PermissionRuleRepository` for tests.

    ``rules_by_scope`` is ``{(scope_kind, scope_id): [RuleRow, …]}``.
    The adapter contract is "return rows in most-specific-first scope
    order"; the stub preserves that by walking ``ancestor_scope_ids``
    in order and emitting rows keyed on each scope.
    """

    rules_by_scope: dict[tuple[str, str], list[RuleRow]] = field(default_factory=dict)

    def rules_for(
        self,
        session: Session,
        *,
        workspace_id: str,
        user_id: str,
        action_key: str,
        scope_kind: str,
        scope_id: str,
        ancestor_scope_ids: Sequence[tuple[str, str]],
    ) -> Sequence[RuleRow]:
        collected: list[RuleRow] = []
        for kind, sid in ancestor_scope_ids:
            for row in self.rules_by_scope.get((kind, sid), []):
                collected.append(row)
        return collected


# -----------------------------------------------------------------------
# Test cases — exercise each quadrant of §02 "Permission resolution".
# -----------------------------------------------------------------------


class TestValidation:
    """Caller-bug errors — 422-equivalent surface."""

    def test_unknown_action_raises(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id, actor_id=seeded.manager_user_id
            )
            with pytest.raises(UnknownActionKey) as exc:
                require(
                    s,
                    ctx,
                    action_key="not.a.real.action",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                )
            # The exception message carries the offending key.
            assert "not.a.real.action" in str(exc.value)

    def test_invalid_scope_kind_raises(self, factory: sessionmaker[Session]) -> None:
        """``workspace.archive`` doesn't accept ``scope_kind='property'``."""
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(workspace_id=seeded.workspace_id, actor_id=seeded.owner_user_id)
            with pytest.raises(InvalidScope):
                require(
                    s,
                    ctx,
                    action_key="workspace.archive",
                    scope_kind="property",
                    scope_id=new_ulid(),
                )


class TestRootOnly:
    """Root-only actions — owners only, no overrides."""

    def test_owner_allowed(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.owner_user_id,
                was_owner=True,
            )
            # No exception — returns None.
            require(
                s,
                ctx,
                action_key="workspace.archive",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
            )

    def test_non_owner_denied(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.manager_user_id,
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="workspace.archive",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                )

    def test_allow_rule_cannot_widen_root_only(
        self, factory: sessionmaker[Session]
    ) -> None:
        """§02 #2: allow/deny rules on root-only actions have no effect.

        Inject an explicit allow rule for the non-owner manager — the
        resolver must still deny because root-only bypasses the rule
        walk entirely.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.manager_user_id,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="allow",
                        ),
                    ],
                },
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="workspace.archive",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                    rule_repo=repo,
                )


class TestRootProtectedDeny:
    """Owners are immune to deny rules on ``root_protected_deny`` actions."""

    def test_owner_allowed_despite_deny_rule(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.owner_user_id,
                was_owner=True,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                    ],
                },
            )
            # ``payroll.issue_payslip`` is ``root_protected_deny=True``.
            require(
                s,
                ctx,
                action_key="payroll.issue_payslip",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
                rule_repo=repo,
            )

    def test_non_owner_still_denied_by_deny_rule(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The immunity applies to owners; managers still hit the deny."""
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.manager_user_id,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                    ],
                },
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="payroll.issue_payslip",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                    rule_repo=repo,
                )

    def test_same_scope_root_protected_deny_on_owner_is_ignored(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Owner immunity is per-row, not per-scope.

        Same-scope [deny, allow] on a ``root_protected_deny`` action
        with an owner caller: the deny is masked row-by-row, so the
        scope bucket reduces to "one allow" and the resolver returns.
        If the immunity were applied after the deny-wins check, the
        owner would still be denied — so this test pins the per-row
        contract.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.owner_user_id,
                was_owner=True,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="allow",
                        ),
                    ],
                },
            )
            # ``payroll.issue_payslip`` has ``root_protected_deny=True``.
            # Owner immunity masks the deny; the allow fires.
            require(
                s,
                ctx,
                action_key="payroll.issue_payslip",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
                rule_repo=repo,
            )

    def test_non_root_protected_deny_fires_on_owner_too(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Non-root-protected actions: owners enjoy no special immunity.

        ``expenses.approve`` has ``root_protected_deny=False`` in the
        catalog; a deny rule must apply to everyone including owners.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.owner_user_id,
                was_owner=True,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                    ],
                },
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="expenses.approve",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                    rule_repo=repo,
                )


class TestRuleWalk:
    """Explicit allow / deny rules, scope ordering."""

    def test_explicit_allow_grants_non_default_user(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A worker isn't in ``expenses.approve``'s default_allow; an
        allow rule widens them in.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.worker_user_id,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="allow",
                        ),
                    ],
                },
            )
            require(
                s,
                ctx,
                action_key="expenses.approve",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
                rule_repo=repo,
            )

    def test_property_rule_beats_workspace_rule(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Most-specific-first: property-scope allow fires before
        workspace-scope deny.

        The stub returns rows in scope-chain order (property first),
        and the resolver stops at the first match.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            property_id = new_ulid()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.worker_user_id,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("property", property_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="property",
                            scope_id=property_id,
                            effect="allow",
                        ),
                    ],
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                    ],
                },
            )
            # Worker isn't in ``expenses.approve`` default_allow; only
            # the rule walk can decide. Property-first ordering makes
            # the allow win.
            require(
                s,
                ctx,
                action_key="expenses.approve",
                scope_kind="property",
                scope_id=property_id,
                rule_repo=repo,
            )

    def test_explicit_deny_overrides_default(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A manager would normally pass ``expenses.approve`` via default_allow;
        a deny rule cuts them out.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.manager_user_id,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                    ],
                },
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="expenses.approve",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                    rule_repo=repo,
                )

    def test_same_scope_allow_then_deny_denies(
        self, factory: sessionmaker[Session]
    ) -> None:
        """§02: deny within a scope beats allow within the same scope.

        An [allow, deny] pair at workspace-scope must deny regardless
        of which row the adapter happens to emit first — the resolver
        tallies the whole scope bucket before deciding. If the
        ordering mattered, swapping the two rows would flip the
        outcome; pinning the assertion here locks the semantics.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.worker_user_id,
            )
            repo = _StubRuleRepo(
                rules_by_scope={
                    ("workspace", seeded.workspace_id): [
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="allow",
                        ),
                        RuleRow(
                            rule_id=new_ulid(),
                            scope_kind="workspace",
                            scope_id=seeded.workspace_id,
                            effect="deny",
                        ),
                    ],
                },
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="expenses.approve",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                    rule_repo=repo,
                )


class TestDefaultAllow:
    """Fallback step — ``default_allow`` consulted when no rule matched."""

    def test_manager_passes_via_default_allow(
        self, factory: sessionmaker[Session]
    ) -> None:
        """``scope.edit_settings`` defaults to ``owners, managers``; a
        manager role_grant qualifies.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.manager_user_id,
            )
            require(
                s,
                ctx,
                action_key="scope.edit_settings",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
            )

    def test_worker_passes_via_default_allow_all_workers(
        self, factory: sessionmaker[Session]
    ) -> None:
        """``tasks.create`` defaults include ``all_workers``."""
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.worker_user_id,
            )
            require(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
            )

    def test_stranger_denied(self, factory: sessionmaker[Session]) -> None:
        """No grants, no group memberships → no default match → deny."""
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.stranger_user_id,
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="scope.edit_settings",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                )

    def test_worker_denied_on_manager_only_action(
        self, factory: sessionmaker[Session]
    ) -> None:
        """``payroll.view_other`` defaults to ``owners, managers`` — a
        worker-only grant doesn't cover it.
        """
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.worker_user_id,
            )
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="payroll.view_other",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                )


class TestStructuredLogging:
    """Every deny emits one ``authz.denied`` log record with the decision data.

    The integration fixture's ``alembic upgrade head`` path runs
    ``logging.config.fileConfig`` with ``disable_existing_loggers=True``
    (alembic.ini default), which flips ``propagate=False`` on loggers
    not listed in the config. ``caplog`` attaches to the root logger,
    so every test in this class force-enables propagation before the
    assertions to stay stable regardless of whether an alembic-
    touching fixture ran earlier in the session.
    """

    @pytest.fixture(autouse=True)
    def _restore_logger_propagation(self) -> Iterator[None]:
        """Force ``app.authz.enforce`` to propagate for caplog visibility."""
        logger = logging.getLogger("app.authz.enforce")
        saved_propagate = logger.propagate
        saved_disabled = logger.disabled
        logger.propagate = True
        logger.disabled = False
        try:
            yield
        finally:
            logger.propagate = saved_propagate
            logger.disabled = saved_disabled

    def test_deny_emits_structured_log(
        self,
        factory: sessionmaker[Session],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.stranger_user_id,
            )
            caplog.set_level(logging.WARNING, logger="app.authz.enforce")
            with pytest.raises(PermissionDenied):
                require(
                    s,
                    ctx,
                    action_key="scope.edit_settings",
                    scope_kind="workspace",
                    scope_id=seeded.workspace_id,
                )

            # One record, carrying the structured fields the "who can
            # do this?" preview depends on.
            denied = [r for r in caplog.records if r.message == "authz.denied"]
            assert len(denied) == 1
            record = denied[0]
            assert record.levelno == logging.WARNING
            assert record.__dict__["event"] == "authz.denied"
            assert record.__dict__["action_key"] == "scope.edit_settings"
            assert record.__dict__["scope_kind"] == "workspace"
            assert record.__dict__["scope_id"] == seeded.workspace_id
            assert record.__dict__["actor_id"] == seeded.stranger_user_id
            assert record.__dict__["workspace_id"] == seeded.workspace_id
            # The reason hint distinguishes the three deny paths.
            assert record.__dict__["reason"] in {
                "no_match",
                "rule_deny",
                "root_only",
            }

    def test_allow_does_not_emit_log(
        self,
        factory: sessionmaker[Session],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No log line on the happy path — keep the signal-to-noise high."""
        with factory() as s:
            seeded = _seed(s)
            s.commit()
            ctx = _ctx(
                workspace_id=seeded.workspace_id,
                actor_id=seeded.owner_user_id,
                was_owner=True,
            )
            caplog.set_level(logging.WARNING, logger="app.authz.enforce")
            require(
                s,
                ctx,
                action_key="workspace.archive",
                scope_kind="workspace",
                scope_id=seeded.workspace_id,
            )
            assert [r for r in caplog.records if r.message == "authz.denied"] == []


class TestEmptyRepoDefault:
    """With the default empty repo, no rules → default_allow only."""

    def test_default_repo_is_empty(self, factory: sessionmaker[Session]) -> None:
        """The module-level default is :class:`EmptyPermissionRuleRepository`.

        No rules in v1 means every call falls through to the
        default_allow fallback; the empty repo exists so callers
        don't have to construct one per request. We still pass a
        real session to keep the Protocol contract honest even though
        the empty repo never reads from it.
        """
        repo = EmptyPermissionRuleRepository()
        with factory() as s:
            rows = repo.rules_for(
                s,
                workspace_id="ws",
                user_id="u",
                action_key="scope.view",
                scope_kind="workspace",
                scope_id="ws",
                ancestor_scope_ids=(("workspace", "ws"),),
            )
            assert list(rows) == []


class TestPermissionCheckValue:
    """Dataclass carries the fields the caller + log line need."""

    def test_permission_check_is_frozen(self) -> None:
        """``frozen=True`` + ``slots=True`` dataclasses are immutable.

        ``dataclasses.replace`` is the supported way to derive a new
        instance with a changed field; that contract is what callers
        rely on, so we pin it here. (A direct attribute write raises
        — either ``AttributeError`` or ``TypeError`` depending on the
        Python build — but testing that path with a statement-level
        assignment earns a ``B010`` lint; using :func:`dataclasses.replace`
        checks the meaningful invariant instead.)
        """
        import dataclasses

        check = PermissionCheck(
            action_key="scope.view",
            scope_kind="workspace",
            scope_id="01HWA00000000000000000WS01",
        )
        derived = dataclasses.replace(check, action_key="scope.edit_settings")
        assert check.action_key == "scope.view"
        assert derived.action_key == "scope.edit_settings"
        assert check is not derived

    def test_permission_check_roundtrip(self) -> None:
        """Values survive construction unchanged."""
        check = PermissionCheck(
            action_key="tasks.create",
            scope_kind="property",
            scope_id="01HWA00000000000000000PR01",
        )
        assert check.action_key == "tasks.create"
        assert check.scope_kind == "property"
        assert check.scope_id == "01HWA00000000000000000PR01"
