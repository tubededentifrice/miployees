"""Unit tests for :mod:`app.api.v1.tasks` payload helpers.

Covers the derived fields on :class:`TaskPayload` (``overdue``,
``time_window_local``) and the rrule humanizer feeding
:class:`SchedulePayload.rrule_human`, plus the tuple-cursor helpers for
the comments list endpoint. Pure-Python tests: no FastAPI, no DB, no
router wiring. The integration-level behaviour is exercised by
``tests/integration/api/test_tasks_routes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.api.v1.tasks import (
    TaskPayload,
    _compute_overdue,
    _compute_time_window_local,
    _decode_comment_cursor,
    _encode_comment_cursor,
    _humanize_rrule,
)
from app.domain.tasks.oneoff import TaskView


def _view(
    *,
    state: str = "pending",
    scheduled_for_utc: datetime | None = None,
    duration_minutes: int | None = 60,
    property_id: str | None = "prop-01",
) -> TaskView:
    """Return a :class:`TaskView` populated with deterministic defaults."""
    anchor = scheduled_for_utc or datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC)
    return TaskView(
        id="01ZZZZZZZZZZZZZZZZZZZZZZZZZ",
        workspace_id="ws-01",
        template_id=None,
        schedule_id=None,
        property_id=property_id,
        area_id=None,
        unit_id=None,
        title="Clean pool",
        description_md=None,
        priority="normal",
        state=state,  # type: ignore[arg-type]
        scheduled_for_local="2026-04-20T11:00:00",
        scheduled_for_utc=anchor,
        duration_minutes=duration_minutes,
        photo_evidence="disabled",
        linked_instruction_ids=(),
        inventory_consumption_json={},
        expected_role_id=None,
        assigned_user_id=None,
        created_by="user-01",
        is_personal=False,
        created_at=datetime(2026, 4, 19, 0, 0, 0, tzinfo=UTC),
    )


class TestOverdue:
    """``overdue`` is ``True`` iff the task is past-anchor and non-terminal."""

    def test_future_pending_is_not_overdue(self) -> None:
        view = _view(
            state="pending",
            scheduled_for_utc=datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is False

    def test_past_pending_is_overdue(self) -> None:
        view = _view(
            state="pending",
            scheduled_for_utc=datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC),
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is True

    def test_terminal_states_are_never_overdue(self) -> None:
        anchor = datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC)
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        for state in ("done", "skipped", "cancelled"):
            view = _view(state=state, scheduled_for_utc=anchor)
            assert _compute_overdue(view, now) is False, (
                f"{state=} should collapse overdue to False"
            )

    def test_naive_anchor_is_treated_as_utc(self) -> None:
        """A DB round-trip that strips the tz on SQLite still lands sanely."""
        view = _view(
            state="pending",
            scheduled_for_utc=datetime(2026, 4, 20, 8, 0, 0),  # naive
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is True


class TestTimeWindowLocal:
    """``time_window_local`` renders the wall-clock window in the property TZ."""

    def test_renders_in_property_zone(self) -> None:
        # 09:00 UTC + 60min in Europe/Paris (UTC+2 during DST → 11:00-12:00).
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
            duration_minutes=60,
        )
        assert _compute_time_window_local(view, "Europe/Paris") == "11:00-12:00"

    def test_falls_back_to_thirty_minutes_when_duration_is_null(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
            duration_minutes=None,
        )
        # Europe/Paris at 09:00 UTC in April is 11:00 local; +30min → 11:30.
        assert _compute_time_window_local(view, "Europe/Paris") == "11:00-11:30"

    def test_missing_timezone_returns_none(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
        )
        assert _compute_time_window_local(view, None) is None

    def test_junk_timezone_returns_none(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
        )
        assert _compute_time_window_local(view, "Not/AZone") is None

    def test_antimeridian_timezone_renders_correctly(self) -> None:
        """Pacific/Auckland is UTC+12 in winter; the window should advance."""
        # 22:00 UTC on 2026-04-20 is 10:00 local NZST (UTC+12).
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 22, 0, 0, tzinfo=UTC),
            duration_minutes=90,
        )
        assert _compute_time_window_local(view, "Pacific/Auckland") == "10:00-11:30"


class TestFromViewEndToEnd:
    """The :meth:`TaskPayload.from_view` factory composes both helpers."""

    def test_from_view_populates_derived_fields(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
            duration_minutes=60,
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        payload = TaskPayload.from_view(
            view, property_timezone="Europe/Paris", now_utc=now
        )
        assert payload.overdue is True
        assert payload.time_window_local == "11:00-12:00"
        assert payload.title == "Clean pool"
        assert payload.id == "01ZZZZZZZZZZZZZZZZZZZZZZZZZ"

    def test_from_view_without_zone_leaves_window_null(self) -> None:
        view = _view(property_id=None)
        payload = TaskPayload.from_view(view, property_timezone=None)
        assert payload.time_window_local is None


class TestCommentCursor:
    """Tuple-cursor round-trips for the ``comments`` pagination."""

    def test_round_trip(self) -> None:
        created = datetime(2026, 4, 20, 9, 30, 0, tzinfo=UTC)
        cursor = _encode_comment_cursor(created, "01AAAA")
        decoded_ts, decoded_id = _decode_comment_cursor(cursor)
        assert decoded_ts == created
        assert decoded_id == "01AAAA"

    def test_empty_cursor_collapses_to_none_pair(self) -> None:
        assert _decode_comment_cursor(None) == (None, None)
        assert _decode_comment_cursor("") == (None, None)

    def test_tampered_cursor_raises_422(self) -> None:
        """A base64-valid blob missing the ``|`` separator collapses to 422."""
        # "no-pipe" base64 encoded.
        import base64

        bad = base64.urlsafe_b64encode(b"nopipehere").rstrip(b"=").decode("ascii")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            _decode_comment_cursor(bad)
        assert excinfo.value.status_code == 422


class TestOverdueAcrossTimezones:
    """Edge case — an anchor in Pacific/Auckland evaluated from the owner's
    Europe/Paris frame still reasons correctly because both collapse to UTC."""

    def test_cross_zone_overdue_uses_utc(self) -> None:
        # Anchor: 2026-04-20 09:00 Pacific/Auckland == 2026-04-19 21:00 UTC.
        auckland_anchor = datetime(
            2026, 4, 20, 9, 0, 0, tzinfo=ZoneInfo("Pacific/Auckland")
        ).astimezone(UTC)
        view = _view(scheduled_for_utc=auckland_anchor)
        # "Now" in Paris: 2026-04-20 00:00 CEST == 22:00 UTC.
        now = datetime(2026, 4, 19, 22, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is True


class TestHumanizeRrule:
    """``_humanize_rrule`` collapses an RRULE + DTSTART pair into copy.

    Mirrors the cadence labels the manager Schedules page renders ("Every
    Saturday at 09:00", "Weekly on Mon, Thu at 10:30") so the wire
    payload can drive the column without re-implementing the parser in
    TypeScript. The schedule's RRULE is validated at write time, so the
    helper's job is presentation-only — every input here is already
    accepted by :func:`app.domain.tasks.schedules._validate_rrule`.
    """

    def test_daily(self) -> None:
        assert _humanize_rrule("RRULE:FREQ=DAILY", "2026-04-20T09:00") == (
            "Every day at 09:00"
        )

    def test_daily_interval(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=DAILY;INTERVAL=3", "2026-04-15T09:00")
            == "Every 3 days at 09:00"
        )

    def test_weekly_single_day(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=WEEKLY;BYDAY=SA", "2026-04-18T09:00")
            == "Every Saturday at 09:00"
        )

    def test_weekly_two_days(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=WEEKLY;BYDAY=MO,TH", "2026-04-20T10:30")
            == "Weekly on Mon, Thu at 10:30"
        )

    def test_weekly_weekdays_set(self) -> None:
        """``MO,TU,WE,TH,FR`` collapses to ``"Weekdays …"``."""
        assert (
            _humanize_rrule(
                "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "2026-04-20T07:00"
            )
            == "Weekdays at 07:00"
        )

    def test_weekly_weekends_set(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=WEEKLY;BYDAY=SA,SU", "2026-04-18T11:00")
            == "Weekends at 11:00"
        )

    def test_weekly_interval_uses_explicit_form(self) -> None:
        """``INTERVAL>1`` keeps the cadence unambiguous."""
        assert (
            _humanize_rrule("RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO", "2026-04-20T09:00")
            == "Every 2 weeks on Mon at 09:00"
        )

    def test_weekly_no_byday_uses_dtstart_weekday(self) -> None:
        """``FREQ=WEEKLY`` without BYDAY anchors on dtstart's weekday."""
        # 2026-04-20 is a Monday; dateutil populates ``_byweekday`` from
        # dtstart so the label says "Every Monday at 10:00".
        assert (
            _humanize_rrule("RRULE:FREQ=WEEKLY", "2026-04-20T10:00")
            == "Every Monday at 10:00"
        )

    def test_monthly_picks_dtstart_day(self) -> None:
        """``FREQ=MONTHLY`` without BYMONTHDAY uses dtstart's day-of-month."""
        assert (
            _humanize_rrule("RRULE:FREQ=MONTHLY", "2026-04-20T09:00")
            == "Monthly on the 20th at 09:00"
        )

    def test_monthly_explicit_day(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=MONTHLY;BYMONTHDAY=15", "2026-04-15T09:00")
            == "Monthly on the 15th at 09:00"
        )

    def test_monthly_multiple_days(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15", "2026-04-15T09:00")
            == "Monthly on days 1, 15 at 09:00"
        )

    def test_monthly_interval(self) -> None:
        assert (
            _humanize_rrule(
                "RRULE:FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=15",
                "2026-04-15T09:00",
            )
            == "Every 2 months on the 15th at 09:00"
        )

    def test_yearly(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=YEARLY", "2026-04-15T09:00")
            == "Yearly at 09:00"
        )

    def test_yearly_interval(self) -> None:
        assert (
            _humanize_rrule("RRULE:FREQ=YEARLY;INTERVAL=4", "2026-04-15T09:00")
            == "Every 4 years at 09:00"
        )

    def test_ordinal_teens_use_th(self) -> None:
        """11/12/13 keep the ``"th"`` suffix despite the digit endings."""
        for day in (11, 12, 13):
            label = _humanize_rrule(
                f"RRULE:FREQ=MONTHLY;BYMONTHDAY={day}",
                f"2026-04-{day:02d}T09:00",
            )
            assert label == f"Monthly on the {day}th at 09:00"

    def test_ordinal_st_nd_rd(self) -> None:
        for day, ordinal in ((1, "1st"), (2, "2nd"), (3, "3rd"), (21, "21st")):
            label = _humanize_rrule(
                f"RRULE:FREQ=MONTHLY;BYMONTHDAY={day}",
                f"2026-04-{day:02d}T09:00",
            )
            assert label == f"Monthly on the {ordinal} at 09:00"

    def test_unparseable_rrule_returns_fallback(self) -> None:
        """Tampered body collapses to the friendly fallback rather than 500."""
        assert _humanize_rrule("not a rrule", "2026-04-15T09:00") == (
            "Custom recurrence"
        )

    def test_unsupported_freq_returns_fallback(self) -> None:
        """``FREQ=HOURLY`` / ``FREQ=MINUTELY`` are out of v1 scope."""
        assert (
            _humanize_rrule("RRULE:FREQ=HOURLY", "2026-04-15T09:00")
            == "Custom recurrence"
        )

    def test_empty_dtstart_drops_time_suffix(self) -> None:
        """An empty / malformed dtstart still yields a recurrence label."""
        assert _humanize_rrule("RRULE:FREQ=DAILY", "") == "Every day"

    def test_empty_dtstart_weekly_without_byday_is_deterministic(self) -> None:
        """``FREQ=WEEKLY`` + empty dtstart must not depend on the wall clock.

        ``dateutil.rrule`` falls back to ``datetime.now()`` when no
        dtstart is given, leaking the current weekday into
        ``_byweekday``. The humanizer pins a sentinel anchor and
        ignores dtstart-derived values when BYDAY is absent — so the
        label is stable across runs and across servers.
        """
        assert _humanize_rrule("RRULE:FREQ=WEEKLY", "") == "Weekly"

    def test_empty_dtstart_monthly_without_bymonthday_is_deterministic(self) -> None:
        """``FREQ=MONTHLY`` + empty dtstart must not leak ``now().day``."""
        assert _humanize_rrule("RRULE:FREQ=MONTHLY", "") == "Monthly"

    def test_explicit_byday_survives_empty_dtstart(self) -> None:
        """An explicit ``BYDAY=`` clause is rendered even without dtstart."""
        assert _humanize_rrule("RRULE:FREQ=WEEKLY;BYDAY=MO", "") == "Every Monday"

    def test_byhour_clause_is_ignored_v1(self) -> None:
        """``BYHOUR`` is dropped silently in v1 — best-effort label.

        The schedule's RRULE has been validated by the domain, but
        BYHOUR / BYMINUTE / BYSETPOS aren't part of the v1 cadence
        vocabulary the manager UI exposes. We render the base
        recurrence; the SPA renders the full RRULE elsewhere if the
        manager wants the gory detail.
        """
        assert (
            _humanize_rrule("RRULE:FREQ=DAILY;BYHOUR=9,17", "2026-04-20T09:00")
            == "Every day at 09:00"
        )

    def test_bysetpos_clause_falls_through_to_monthly(self) -> None:
        """``BYSETPOS`` collapses to the plain MONTHLY shape.

        ``RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1`` ("first Monday of
        the month") isn't part of the v1 recurrence vocabulary; the
        humanizer renders the base "Monthly" rather than mis-stating
        the cadence as "Every Monday".
        """
        assert (
            _humanize_rrule(
                "RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1",
                "2026-04-20T09:00",
            )
            == "Monthly at 09:00"
        )

    def test_minutely_frequency_returns_fallback(self) -> None:
        """``FREQ=MINUTELY`` is out of scope and collapses to fallback."""
        assert (
            _humanize_rrule("RRULE:FREQ=MINUTELY", "2026-04-15T09:00")
            == "Custom recurrence"
        )

    def test_weekly_three_day_combo_is_listed(self) -> None:
        """``MO,TU,WE`` (not weekdays/weekends) renders as a list."""
        assert (
            _humanize_rrule("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE", "2026-04-20T10:30")
            == "Weekly on Mon, Tue, Wed at 10:30"
        )

    def test_dtstart_with_seconds_is_truncated_to_minute(self) -> None:
        """``%H:%M`` strips a trailing seconds component cleanly."""
        assert (
            _humanize_rrule("RRULE:FREQ=DAILY", "2026-04-20T09:00:30")
            == "Every day at 09:00"
        )

    def test_tz_aware_dtstart_is_treated_as_naive(self) -> None:
        """A stray ``+02:00`` suffix doesn't shift the displayed time.

        The schedules domain stores ``dtstart_local`` as a naive
        property-local timestamp; a tz-suffixed value is a write-time
        bug, but the projection layer must not crash on it. The label
        renders the wall-clock as written.
        """
        assert (
            _humanize_rrule("RRULE:FREQ=DAILY", "2026-04-20T09:00+02:00")
            == "Every day at 09:00"
        )
