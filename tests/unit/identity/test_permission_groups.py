"""Unit tests for :mod:`app.domain.identity.permission_groups`.

Pure-Python surface: error-class hierarchy, action-catalog keys, and
the frozen / slotted invariants on the public dataclass refs. The
full CRUD round-trip (with a real DB session, tenant filter, audit
writes, and cascade behaviour) lives under
``tests/integration/identity/test_permission_groups.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.identity._action_catalog import ACTION_CATALOG, ACTION_KEYS
from app.domain.identity.permission_groups import (
    PermissionGroupMemberRef,
    PermissionGroupNotFound,
    PermissionGroupRef,
    PermissionGroupSlugTaken,
    SystemGroupProtected,
    UnknownCapability,
)


class TestErrorTypes:
    """Error classes subclass the right stdlib parents."""

    def test_not_found_is_lookup_error(self) -> None:
        assert issubclass(PermissionGroupNotFound, LookupError)

    def test_slug_taken_is_value_error(self) -> None:
        assert issubclass(PermissionGroupSlugTaken, ValueError)

    def test_system_group_protected_is_value_error(self) -> None:
        assert issubclass(SystemGroupProtected, ValueError)

    def test_unknown_capability_is_value_error(self) -> None:
        assert issubclass(UnknownCapability, ValueError)

    def test_errors_are_distinct(self) -> None:
        """Each error class is its own type — callers ``except`` on them."""
        classes = {
            PermissionGroupNotFound,
            PermissionGroupSlugTaken,
            SystemGroupProtected,
            UnknownCapability,
        }
        assert len(classes) == 4


class TestActionCatalog:
    """Catalog has the expected shape and carries the spec's v1 keys."""

    def test_catalog_is_mapping(self) -> None:
        """``ACTION_CATALOG`` is the primary surface — a key→spec mapping."""
        from collections.abc import Mapping

        assert isinstance(ACTION_CATALOG, Mapping)

    def test_action_keys_is_frozenset(self) -> None:
        """``ACTION_KEYS`` is the membership-only view callers reach for."""
        assert isinstance(ACTION_KEYS, frozenset)

    def test_catalog_matches_action_keys(self) -> None:
        """``ACTION_KEYS`` is derived from ``ACTION_CATALOG``."""
        assert frozenset(ACTION_CATALOG.keys()) == ACTION_KEYS

    def test_catalog_is_non_empty(self) -> None:
        """v1 ships a non-trivial number of actions — guard against a regression
        that accidentally empties the literal.

        The spec tables today enumerate ~7 root-only + ~60 rule-driven
        keys; the floor of 50 keeps the test robust against minor edits
        while still flagging any structural collapse.
        """
        assert len(ACTION_CATALOG) >= 50

    @pytest.mark.parametrize(
        "key",
        [
            # Root-only governance keys from §05 "Root-only actions".
            "workspace.archive",
            "permissions.edit_rules",
            "groups.manage_owners_membership",
            "admin.purge",
            "deployment.rotate_root_key",
            # Rule-driven spot-checks spanning multiple namespaces.
            "scope.view",
            "tasks.create",
            "bookings.cancel",
            "payroll.issue_payslip",
            "audit_log.view",
        ],
    )
    def test_expected_keys_present(self, key: str) -> None:
        assert key in ACTION_CATALOG, f"{key!r} missing from ACTION_CATALOG"

    def test_no_v0_owner_synonym(self) -> None:
        """Safety-net: a legacy v0 key must not sneak back in."""
        assert "owner" not in ACTION_CATALOG


class TestPermissionGroupRef:
    """``PermissionGroupRef`` is frozen + slotted."""

    def _ref(self) -> PermissionGroupRef:
        return PermissionGroupRef(
            id="01HWA00000000000000000GR01",
            slug="family",
            name="Family",
            system=False,
            capabilities={"tasks.create": True},
            created_at=datetime(2026, 4, 19, tzinfo=UTC),
        )

    def test_ref_is_slotted(self) -> None:
        """Slotted dataclasses cannot grow attributes at runtime.

        Depending on the Python version, ``frozen=True, slots=True``
        raises either :class:`AttributeError` (slots reject unknown
        names) or :class:`TypeError` (frozen dataclasses route through
        ``__setattr__`` first). Accept both.
        """
        ref = self._ref()
        with pytest.raises((AttributeError, TypeError)):
            ref.extra = "nope"  # type: ignore[attr-defined]

    def test_ref_is_frozen(self) -> None:
        """Frozen dataclasses reject in-place field writes.

        ``FrozenInstanceError`` is the canonical exception type; the
        broader ``AttributeError`` parent class catch stays portable
        across minor Python version bumps.
        """
        from dataclasses import FrozenInstanceError

        ref = self._ref()
        with pytest.raises(FrozenInstanceError):
            ref.name = "Renamed"  # type: ignore[misc]

    def test_ref_equality_by_value(self) -> None:
        assert self._ref() == self._ref()


class TestPermissionGroupMemberRef:
    """``PermissionGroupMemberRef`` is frozen + slotted."""

    def _ref(self) -> PermissionGroupMemberRef:
        return PermissionGroupMemberRef(
            group_id="01HWA00000000000000000GR01",
            user_id="01HWA00000000000000000USR1",
            added_at=datetime(2026, 4, 19, tzinfo=UTC),
            added_by_user_id="01HWA00000000000000000USR2",
        )

    def test_ref_is_slotted(self) -> None:
        ref = self._ref()
        with pytest.raises((AttributeError, TypeError)):
            ref.extra = "nope"  # type: ignore[attr-defined]

    def test_ref_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        ref = self._ref()
        with pytest.raises(FrozenInstanceError):
            ref.user_id = "somebody-else"  # type: ignore[misc]

    def test_ref_equality_by_value(self) -> None:
        assert self._ref() == self._ref()
