"""Self-consistency unit tests for the action catalog.

These checks never touch the DB — they verify the in-memory shape of
:mod:`app.domain.identity._action_catalog` against the spec's
invariants (§05 "Action catalog"):

* Every key is unique (``Mapping`` shape guarantees this, but we
  double-check by counting).
* Every ``valid_scope_kind`` is in the allow-list
  (:data:`VALID_SCOPE_KINDS`).
* Every ``default_allow`` slug is one of the four system groups
  recognised by :mod:`app.authz.membership`.
* Root-only entries carry an empty ``default_allow`` (root-only
  actions never consult defaults — see §02 "Permission resolution").
* The catalog has the 76 entries currently documented in §05.
* :func:`validate_catalog_integrity` passes with the empty v1
  rule-repo.
"""

from __future__ import annotations

import pytest

from app.authz import EmptyPermissionRuleRepository
from app.authz.enforce import CatalogDrift, validate_catalog_integrity
from app.domain.identity._action_catalog import (
    ACTION_CATALOG,
    ACTION_KEYS,
    VALID_SCOPE_KINDS,
    ActionSpec,
)

_KNOWN_GROUPS: frozenset[str] = frozenset(
    {"owners", "managers", "all_workers", "all_clients"}
)


class TestShape:
    """Top-level shape of :data:`ACTION_CATALOG` and :data:`ACTION_KEYS`."""

    def test_every_key_well_formed(self) -> None:
        """Keys are non-empty dotted strings — no whitespace, no blanks."""
        for key in ACTION_CATALOG:
            assert key == key.strip(), f"{key!r}: has surrounding whitespace"
            assert "." in key, f"{key!r}: missing namespace separator"
            assert key, "empty key in ACTION_CATALOG"

    def test_no_duplicate_keys(self) -> None:
        """Mapping guarantees this, but the derived keys set must match."""
        assert len(ACTION_CATALOG) == len(ACTION_KEYS)

    def test_catalog_mirrors_keys(self) -> None:
        """The ``ACTION_KEYS`` view is strictly ``ACTION_CATALOG.keys()``."""
        assert frozenset(ACTION_CATALOG.keys()) == ACTION_KEYS

    def test_catalog_has_expected_size(self) -> None:
        """v1 spec §05 enumerates 7 root-only + 76 rule-driven = 83 keys.

        A hard number — if it changes, either the spec or the catalog
        drifted and the author needs to say which. cd-cfe4 added
        ``tasks.comment`` and ``tasks.comment_moderate`` alongside the
        agent-inbox comments service.
        """
        assert len(ACTION_CATALOG) == 83

    def test_entries_are_action_spec_instances(self) -> None:
        for key, spec in ACTION_CATALOG.items():
            assert isinstance(spec, ActionSpec), f"{key!r}: not ActionSpec"
            assert spec.key == key, f"{key!r}: spec.key does not match map key"


class TestValidScopeKinds:
    """Every entry's ``valid_scope_kinds`` is drawn from the allow-list."""

    def test_all_scope_kinds_recognised(self) -> None:
        for key, spec in ACTION_CATALOG.items():
            assert spec.valid_scope_kinds, f"{key!r}: empty valid_scope_kinds"
            for kind in spec.valid_scope_kinds:
                assert kind in VALID_SCOPE_KINDS, (
                    f"{key!r}: scope_kind={kind!r} not in VALID_SCOPE_KINDS"
                )

    def test_valid_scope_kinds_are_the_four(self) -> None:
        expected = frozenset({"workspace", "property", "organization", "deployment"})
        assert expected == VALID_SCOPE_KINDS


class TestDefaultAllow:
    """Every ``default_allow`` slug is one of the four system groups."""

    def test_all_default_allow_groups_recognised(self) -> None:
        for key, spec in ACTION_CATALOG.items():
            for group in spec.default_allow:
                assert group in _KNOWN_GROUPS, (
                    f"{key!r}: default_allow={group!r} is not a system group"
                )

    def test_root_only_default_allow_empty(self) -> None:
        """Root-only actions don't consult defaults — listing any group
        is a spec error.

        §02 "Permission resolution" #2 states the resolver short-
        circuits on ``root_only`` before step 5 (default_allow), so a
        non-empty list on a root-only entry would be silently ignored
        and mislead future readers.
        """
        for key, spec in ACTION_CATALOG.items():
            if spec.root_only:
                assert spec.default_allow == (), (
                    f"{key!r}: root_only action must not list default_allow "
                    f"groups (got {spec.default_allow!r})"
                )

    def test_rule_driven_has_at_least_one_default_allow(self) -> None:
        """Every non-root-only action ships with a default.

        §05 says ``default_allow`` may be empty (default-deny) but the
        v1 catalog has no such entries today; if a spec edit
        introduces one it should be a deliberate, reviewed choice —
        this test fails loudly to prompt that review.
        """
        for key, spec in ACTION_CATALOG.items():
            if not spec.root_only:
                assert spec.default_allow, (
                    f"{key!r}: rule-driven action ships with empty default_allow"
                )


class TestRootFlags:
    """Expected flags on spot-checked entries."""

    @pytest.mark.parametrize(
        "key",
        [
            "admin.purge",
            "deployment.rotate_root_key",
            "groups.manage_owners_membership",
            "organization.archive",
            "permissions.edit_rules",
            "scope.transfer",
            "workspace.archive",
        ],
    )
    def test_known_root_only(self, key: str) -> None:
        spec = ACTION_CATALOG[key]
        assert spec.root_only is True, f"{key!r}: expected root_only=True"
        assert spec.default_allow == ()

    @pytest.mark.parametrize(
        "key",
        [
            "scope.view",
            "scope.edit_settings",
            "users.invite",
            "users.archive",
            "role_grants.create",
            "role_grants.revoke",
            "groups.create",
            "groups.manage_members",
            "properties.archive",
            "payroll.lock_period",
            "payroll.issue_payslip",
            "expenses.reimburse",
            "api_tokens.manage",
            "audit_log.view",
            "organizations.edit_pay_destination",
            "property_workspace.revoke",
            "vendor_invoices.approve",
            "deployment.view",
            "deployment.workspaces.archive",
            "deployment.settings.edit",
            "deployment.audit.view",
        ],
    )
    def test_known_root_protected_deny(self, key: str) -> None:
        """These are the 21 ✅ entries from §05 "Rule-driven actions"."""
        spec = ACTION_CATALOG[key]
        assert spec.root_protected_deny is True, (
            f"{key!r}: expected root_protected_deny=True per §05"
        )
        assert spec.root_only is False


class TestIntegrityFunction:
    """``validate_catalog_integrity`` passes on the current catalog."""

    def test_passes_with_empty_repo(self) -> None:
        """v1 ships no ``permission_rule`` rows — integrity check passes.

        The session argument is unused by the empty-repo path; we pass
        ``None`` and rely on the function's early exit. When the SQL
        adapter lands, this test is paired with an integration-level
        check that asserts the walker raises :class:`CatalogDrift` on
        a row with an unknown key.
        """
        # ``session=None`` is acceptable here because the
        # ``EmptyPermissionRuleRepository`` does not read from the
        # session. The signature declares ``Session | None`` for
        # exactly this reason.
        validate_catalog_integrity(
            session=None,
            rule_repo=EmptyPermissionRuleRepository(),
        )

    def test_catalog_drift_is_runtime_error(self) -> None:
        assert issubclass(CatalogDrift, RuntimeError)
