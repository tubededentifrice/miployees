"""Unit tests for :mod:`app.domain.identity.role_grants`.

Pure-Python surface: error-class hierarchy, the frozen / slotted
invariants on :class:`RoleGrantRef`, and the accepted-role set.
The full CRUD round-trip (with a real DB session, the tenant filter,
the owner-authority policy, and last-owner protection) lives under
``tests/integration/identity/test_role_grants.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.identity.role_grants import (
    CrossWorkspaceProperty,
    GrantRoleInvalid,
    LastOwnerGrantProtected,
    NotAuthorizedForRole,
    RoleGrantNotFound,
    RoleGrantRef,
)


class TestErrorTypes:
    """Error classes subclass the right stdlib parents.

    Callers ``except`` on the stdlib parents (``PermissionError``,
    ``LookupError``, ``ValueError``) at the HTTP boundary to map to
    403 / 404 / 422 without importing every domain-specific
    exception. A regression in the hierarchy would silently break
    that mapping.
    """

    def test_not_found_is_lookup_error(self) -> None:
        assert issubclass(RoleGrantNotFound, LookupError)

    def test_grant_role_invalid_is_value_error(self) -> None:
        assert issubclass(GrantRoleInvalid, ValueError)

    def test_not_authorized_is_permission_error(self) -> None:
        assert issubclass(NotAuthorizedForRole, PermissionError)

    def test_cross_workspace_property_is_value_error(self) -> None:
        assert issubclass(CrossWorkspaceProperty, ValueError)

    def test_last_owner_protected_is_value_error(self) -> None:
        assert issubclass(LastOwnerGrantProtected, ValueError)

    def test_errors_are_distinct(self) -> None:
        """Each error class is its own type — callers ``except`` on them."""
        classes = {
            RoleGrantNotFound,
            GrantRoleInvalid,
            NotAuthorizedForRole,
            CrossWorkspaceProperty,
            LastOwnerGrantProtected,
        }
        assert len(classes) == 5


class TestRoleGrantRef:
    """``RoleGrantRef`` is frozen + slotted."""

    def _ref(self) -> RoleGrantRef:
        return RoleGrantRef(
            id="01HWA00000000000000000RG01",
            workspace_id="01HWA00000000000000000WS01",
            user_id="01HWA00000000000000000USR1",
            grant_role="manager",
            scope_property_id=None,
            created_at=datetime(2026, 4, 19, tzinfo=UTC),
            created_by_user_id="01HWA00000000000000000USR2",
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
        """Frozen dataclasses reject in-place field writes."""
        from dataclasses import FrozenInstanceError

        ref = self._ref()
        with pytest.raises(FrozenInstanceError):
            ref.grant_role = "worker"  # type: ignore[misc]

    def test_ref_equality_by_value(self) -> None:
        assert self._ref() == self._ref()

    def test_scope_property_id_may_be_none(self) -> None:
        """Workspace-wide grants carry ``scope_property_id = None``."""
        ref = self._ref()
        assert ref.scope_property_id is None

    def test_created_by_user_id_may_be_none(self) -> None:
        """The self-bootstrap grant emitted by workspace creation has no prior actor."""
        ref = RoleGrantRef(
            id="01HWA00000000000000000RG02",
            workspace_id="01HWA00000000000000000WS01",
            user_id="01HWA00000000000000000USR1",
            grant_role="manager",
            scope_property_id=None,
            created_at=datetime(2026, 4, 19, tzinfo=UTC),
            created_by_user_id=None,
        )
        assert ref.created_by_user_id is None
