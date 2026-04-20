"""Unit tests for :mod:`app.domain.identity.membership`.

Cover the pure-function helpers (validators) plus the public
exception surface without touching a DB. Integration paths live in
:mod:`tests.integration.identity.test_membership`.
"""

from __future__ import annotations

import pytest

from app.domain.identity import membership


class TestPublicSurface:
    """The module's exported symbols match the spec contract."""

    def test_exports_exception_types(self) -> None:
        assert membership.InviteBodyInvalid is not None
        assert membership.InviteNotFound is not None
        assert membership.InviteStateInvalid is not None
        assert membership.InviteExpired is not None
        assert membership.InviteAlreadyAccepted is not None
        assert membership.PasskeySessionRequired is not None
        assert membership.NotAMember is not None
        assert membership.LastOwnerMember is not None

    def test_exports_write_member_remove_rejected_audit(self) -> None:
        # Reused from permission_groups — the membership module
        # re-exports it so the HTTP router can import both from
        # one place.
        assert membership.write_member_remove_rejected_audit is not None


class TestValueObjects:
    """Value dataclasses are frozen (immutable)."""

    def test_invite_outcome_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        outcome = membership.InviteOutcome(
            id="invite-1",
            pending_email="a@example.com",
            user_id="user-1",
            user_created=True,
        )
        with pytest.raises(FrozenInstanceError):
            # Frozen dataclass: mutation raises FrozenInstanceError.
            outcome.id = "mutated"  # type: ignore[misc]

    def test_invite_session_shape(self) -> None:
        ssn = membership.InviteSession(
            invite_id="invite-1",
            user_id="user-1",
            email_lower="a@example.com",
            display_name="A",
        )
        assert ssn.email_lower == "a@example.com"


class TestGrantValidation:
    """``_validate_grants`` enforces the v1 scope + role enum.

    Private helper but exercised here so we catch shape drift early —
    the integration tests take longer to fail on a regression.
    """

    _WORKSPACE_ID = "01HWA000000000000000WS001"

    def test_empty_grants_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid):
            membership._validate_grants([], workspace_id=self._WORKSPACE_ID)

    def test_unsupported_scope_kind_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "organization",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "organization" in str(exc.value)

    def test_scope_id_must_match_workspace(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": "different-workspace",
                        "grant_role": "worker",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "scope_id" in str(exc.value)

    def test_invalid_grant_role_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "admin",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "admin" in str(exc.value)

    def test_happy_path_accepts_every_v1_role(self) -> None:
        for role in ("manager", "worker", "client", "guest"):
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": role,
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )


class TestTimezoneNormalisation:
    """``_aware_utc`` stamps UTC on naive datetimes."""

    def test_naive_gets_utc(self) -> None:
        from datetime import datetime as _dt

        out = membership._aware_utc(_dt(2026, 1, 1, 12, 0, 0))
        assert out.tzinfo is not None

    def test_aware_stays_aware(self) -> None:
        from datetime import UTC
        from datetime import datetime as _dt

        out = membership._aware_utc(_dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        assert out.tzinfo == UTC
