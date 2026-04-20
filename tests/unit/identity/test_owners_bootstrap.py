"""Unit tests for owners-group bootstrap + governance surface.

Pure-Python surface: error-class hierarchy for the new
:class:`LastOwnerMember` exception, the frozen-dataclass invariants
around ``resolve_is_owner`` / ``is_owner_member`` identity, and the
audit shape emitted by :func:`write_member_remove_rejected_audit`.

Integration coverage (real DB, real audit rows, real seed write)
lives under ``tests/integration/identity/test_owners_governance.py``.

See ``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog" and ``docs/specs/02-domain-model.md``
§"permission_group" §"Invariants".
"""

from __future__ import annotations

from app.authz import is_owner_member, resolve_is_owner
from app.domain.identity.permission_groups import (
    LastOwnerMember,
    write_member_remove_rejected_audit,
)


class TestLastOwnerMemberType:
    """The new guard exception has the spec-expected hierarchy."""

    def test_last_owner_member_is_value_error(self) -> None:
        """409-style domain errors subclass :class:`ValueError`."""
        assert issubclass(LastOwnerMember, ValueError)

    def test_last_owner_member_carries_message(self) -> None:
        """The message mentions the ``owners`` group so logs are legible."""
        err = LastOwnerMember("cannot remove the last member of 'owners'")
        assert "owners" in str(err)


class TestResolveIsOwnerAlias:
    """``resolve_is_owner`` is the task-spec alias for ``is_owner_member``."""

    def test_aliases_identity(self) -> None:
        """The two public names resolve to the same callable.

        Middleware (cd-7y4) prefers ``resolve_is_owner`` — matches
        its naming. The domain service prefers ``is_owner_member``
        — reads more naturally inside an ``if`` guard. Both MUST
        resolve to one implementation so the definition of "owner"
        never diverges between layers.
        """
        assert resolve_is_owner is is_owner_member


class TestRejectedAuditHelperSurface:
    """``write_member_remove_rejected_audit`` is callable + exported."""

    def test_helper_is_exported(self) -> None:
        """Public surface is discoverable from the domain service module."""
        from app.domain.identity import permission_groups

        assert permission_groups.write_member_remove_rejected_audit is (
            write_member_remove_rejected_audit
        )
