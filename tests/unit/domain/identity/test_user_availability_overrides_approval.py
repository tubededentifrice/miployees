"""Pure-function tests for the §06 hybrid approval calculator (cd-uqw1).

The HTTP-tier suite at
:mod:`tests.unit.api.v1.identity.test_user_availability_overrides`
exercises the §06 "Approval logic (hybrid model)" matrix end-to-end
through the router + service + DB. This module pins the same matrix
on the **pure** ``_compute_approval_required`` function so a future
regression in the calculator (a swapped branch, a flipped comparison)
fires here in milliseconds without spinning up a TestClient.

Each test maps to one row of the §06 table; see
``docs/specs/06-tasks-and-scheduling.md`` §"Approval logic (hybrid
model)".
"""

from __future__ import annotations

from datetime import datetime, time

from app.adapters.db.availability.models import UserWeeklyAvailability
from app.domain.identity.user_availability_overrides import (
    _compute_approval_required,
)

_PINNED = datetime(2026, 4, 25)


def _weekly(starts: time | None, ends: time | None) -> UserWeeklyAvailability:
    """Construct an unpersisted weekly row for the calculator's input."""
    return UserWeeklyAvailability(
        id="01HWWEEKLY00000000000000",
        workspace_id="01HWWS00000000000000000000",
        user_id="01HWUSER0000000000000000",
        weekday=0,
        starts_local=starts,
        ends_local=ends,
        updated_at=_PINNED,
    )


class TestApprovalMatrix:
    """One test per row of the §06 hybrid-approval table."""

    def test_no_weekly_row_treated_as_off_when_adding(self) -> None:
        """``weekly=None`` → treat as off → adding hours is auto-approved."""
        assert (
            _compute_approval_required(
                weekly=None,
                override_available=True,
                override_starts=time(9, 0),
                override_ends=time(13, 0),
            )
            is False
        )

    def test_no_weekly_row_treated_as_off_when_confirming_off(self) -> None:
        """``weekly=None`` + override available=false → auto-approved."""
        assert (
            _compute_approval_required(
                weekly=None,
                override_available=False,
                override_starts=None,
                override_ends=None,
            )
            is False
        )

    def test_off_pattern_adds_work_day_auto_approved(self) -> None:
        """Off (null hours) + available=true → adds → auto-approved."""
        assert (
            _compute_approval_required(
                weekly=_weekly(None, None),
                override_available=True,
                override_starts=time(9, 0),
                override_ends=time(13, 0),
            )
            is False
        )

    def test_off_pattern_confirms_off_auto_approved(self) -> None:
        """Off (null hours) + available=false → confirm off → auto-approved."""
        assert (
            _compute_approval_required(
                weekly=_weekly(None, None),
                override_available=False,
                override_starts=None,
                override_ends=None,
            )
            is False
        )

    def test_working_pattern_removes_day_requires_approval(self) -> None:
        """Working + available=false → removes work day → needs approval."""
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=False,
                override_starts=None,
                override_ends=None,
            )
            is True
        )

    def test_working_pattern_narrows_hours_requires_approval(self) -> None:
        """Working 09-17 + override 09-12 → reduces coverage → needs approval."""
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=time(9, 0),
                override_ends=time(12, 0),
            )
            is True
        )

    def test_working_pattern_narrows_starts_only_requires_approval(self) -> None:
        """Working 09-17 + override 10-17 → narrows the start → needs approval."""
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=time(10, 0),
                override_ends=time(17, 0),
            )
            is True
        )

    def test_working_pattern_extends_hours_auto_approved(self) -> None:
        """Working 09-17 + override 09-19 → extends end → auto-approved."""
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=time(9, 0),
                override_ends=time(19, 0),
            )
            is False
        )

    def test_working_pattern_extends_both_edges_auto_approved(self) -> None:
        """Working 09-17 + override 08-19 → fully encloses → auto-approved."""
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=time(8, 0),
                override_ends=time(19, 0),
            )
            is False
        )

    def test_working_pattern_matches_hours_auto_approved(self) -> None:
        """Working 09-17 + override 09-17 → identical → auto-approved.

        Matching is the degenerate "no change" case — same coverage as
        the weekly pattern, no shrink, so per §06 it doesn't require
        approval.
        """
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=time(9, 0),
                override_ends=time(17, 0),
            )
            is False
        )

    def test_working_pattern_null_hours_falls_back_auto_approved(self) -> None:
        """Working + available=true with null hours → falls back to weekly.

        §06 "user_availability_overrides" §"Invariants": a null-hours
        ``available=true`` override inherits the weekly window — same
        coverage → auto-approved.
        """
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=None,
                override_ends=None,
            )
            is False
        )

    def test_working_pattern_shifted_window_requires_approval(self) -> None:
        """Working 09-17 + override 10-19 → shifted window → needs approval.

        Shifts gain hours on the end (17-19) but lose hours on the
        start (9-10). The §06 spec table doesn't enumerate this
        explicitly, but the "less available for any subset" rule means
        **any** narrowing on either edge requires approval — the
        manager has to sign off on the dropped morning hours even
        though the evening is wider.
        """
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), time(17, 0)),
                override_available=True,
                override_starts=time(10, 0),
                override_ends=time(19, 0),
            )
            is True
        )

    def test_half_set_weekly_treated_as_off(self) -> None:
        """A half-set weekly row (DB CHECK forbids this, but be defensive).

        If somehow a half-set weekly row leaks through, treat it as
        off — same as both-null. The DB CHECK should make this
        unreachable in practice; the test pins the behaviour anyway.
        """
        # Only starts, no ends — both null and half-set should land
        # on the "off" branch (``weekly_working`` is False unless
        # both are non-null).
        assert (
            _compute_approval_required(
                weekly=_weekly(time(9, 0), None),
                override_available=False,
                override_starts=None,
                override_ends=None,
            )
            is False
        )
        assert (
            _compute_approval_required(
                weekly=_weekly(None, time(17, 0)),
                override_available=True,
                override_starts=time(10, 0),
                override_ends=time(12, 0),
            )
            is False
        )
