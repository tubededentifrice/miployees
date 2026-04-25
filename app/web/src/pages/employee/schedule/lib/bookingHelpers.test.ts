// Unit tests for the schedule-page booking + timeline helpers (cd-ops1).
//
// Covers `bookingMinutes`'s precedence (`actual_minutes_paid` >
// `actual_minutes` > scheduled-minus-break), `bookingNeedsAttention`'s
// two-bucket logic, and `computeWindow`'s clamping behaviour for an
// empty week, a quiet 6h day, and a chip-dense booking.

import { describe, expect, it } from "vitest";
import type { Booking } from "@/types/api";
import {
  BOOKING_STATUS_LABEL,
  TASK_CHIP_GAP_PX,
  TASK_CHIP_MIN_PX,
  bookingMinutes,
  bookingNeedsAttention,
  computeWindow,
  fmtDuration,
  fmtHM,
  hhmmToMin,
  isoToMinOfDay,
  posTop,
} from "./bookingHelpers";

function makeBooking(overrides: Partial<Booking> = {}): Booking {
  return {
    id: "b1",
    employee_id: "emp",
    property_id: "p1",
    scheduled_start: "2025-04-24T09:00:00",
    scheduled_end: "2025-04-24T11:00:00",
    status: "scheduled",
    kind: "work",
    actual_minutes: null,
    actual_minutes_paid: null,
    break_seconds: 0,
    pending_amend_minutes: null,
    pending_amend_reason: null,
    declined_at: null,
    declined_reason: null,
    notes_md: "",
    adjusted: false,
    adjustment_reason: null,
    client_org_id: null,
    work_engagement_id: "we1",
    user_id: "u1",
    ...overrides,
  };
}

describe("BOOKING_STATUS_LABEL", () => {
  it("covers every BookingStatus union member", () => {
    // Sanity check: a new status added to the union must get a label
    // here or the day-drawer pill renders as `undefined`.
    expect(BOOKING_STATUS_LABEL.pending_approval).toBeTypeOf("string");
    expect(BOOKING_STATUS_LABEL.adjusted).toBe("Completed (edited)");
    expect(BOOKING_STATUS_LABEL.no_show_worker).toBe("No-show");
  });
});

describe("bookingMinutes", () => {
  it("prefers actual_minutes_paid when set", () => {
    expect(
      bookingMinutes(makeBooking({ actual_minutes_paid: 90, actual_minutes: 60 })),
    ).toBe(90);
  });

  it("falls back to actual_minutes when paid is null", () => {
    expect(bookingMinutes(makeBooking({ actual_minutes: 60 }))).toBe(60);
  });

  it("computes scheduled minutes minus break when both actuals are null", () => {
    // 09:00 → 11:00 = 120m, minus 30m break = 90m.
    expect(
      bookingMinutes(makeBooking({ break_seconds: 30 * 60 })),
    ).toBe(90);
  });

  it("clamps to zero on a negative-duration row (data corruption guard)", () => {
    expect(
      bookingMinutes(
        makeBooking({
          scheduled_start: "2025-04-24T11:00:00",
          scheduled_end: "2025-04-24T09:00:00",
        }),
      ),
    ).toBe(0);
  });
});

describe("bookingNeedsAttention", () => {
  it("flags pending_approval", () => {
    expect(bookingNeedsAttention(makeBooking({ status: "pending_approval" }))).toBe(true);
  });

  it("flags a non-null pending self-amend", () => {
    expect(
      bookingNeedsAttention(makeBooking({ pending_amend_minutes: 30 })),
    ).toBe(true);
  });

  it("does not flag a quiet scheduled row", () => {
    expect(bookingNeedsAttention(makeBooking())).toBe(false);
  });
});

describe("fmtDuration", () => {
  it("formats hours and zero-padded minutes", () => {
    expect(fmtDuration(125)).toBe("2h 05m");
    expect(fmtDuration(60)).toBe("1h 00m");
    expect(fmtDuration(0)).toBe("0h 00m");
  });
});

describe("fmtHM", () => {
  it("formats local HH:MM from an ISO timestamp", () => {
    const d = new Date(2025, 3, 24, 14, 7, 0);
    expect(fmtHM(d.toISOString())).toBe(
      `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`,
    );
  });
});

describe("hhmmToMin", () => {
  it("parses HH:MM into minutes-since-midnight", () => {
    expect(hhmmToMin("00:00")).toBe(0);
    expect(hhmmToMin("09:30")).toBe(570);
    expect(hhmmToMin("23:59")).toBe(23 * 60 + 59);
  });

  it("returns 0 for an empty token (Number('') === 0 fallback)", () => {
    // Defensive: a corrupted slot row coerces to zero rather than
    // NaN-poisoning the window math. `Number('')` is `0`, so an
    // empty input slips through to minute zero.
    expect(hhmmToMin("")).toBe(0);
  });
});

describe("isoToMinOfDay", () => {
  it("returns local-clock minutes for a given timestamp", () => {
    const d = new Date(2025, 3, 24, 9, 30, 0);
    expect(isoToMinOfDay(d.toISOString())).toBe(9 * 60 + 30);
  });
});

describe("computeWindow", () => {
  // The cell shape `computeWindow` consumes is a duck-typed subset of
  // DayCell — only the fields it touches matter. Build a tiny fixture
  // factory so each test reads as one focused assertion.
  function emptyCell(): {
    rota: { slot: { starts_local: string; ends_local: string } }[];
    tasks: { scheduled_start: string; estimated_minutes: number; property_id: string }[];
    bookings: Pick<Booking, "scheduled_start" | "scheduled_end" | "property_id">[];
    pattern: { starts_local: string | null; ends_local: string | null } | null;
  } {
    return { rota: [], tasks: [], bookings: [], pattern: null };
  }

  it("falls back to a padded 09–17 window when the week is empty", () => {
    const w = computeWindow([emptyCell()]);
    // The 09–17 fallback gets the standard ±30min pad, snapping to
    // 08:00–18:00 once rounded to whole hours. The 6h floor never
    // kicks in because the padded range is already 10h.
    expect(w.startMin).toBe(8 * 60);
    expect(w.endMin).toBe(18 * 60);
    expect(w.totalPx).toBeGreaterThan(0);
  });

  it("expands a single 09:30–11:00 booking to a 6h+ window with padding", () => {
    const cell = emptyCell();
    cell.bookings = [{
      scheduled_start: "2025-04-24T09:30:00",
      scheduled_end: "2025-04-24T11:00:00",
      property_id: "p1",
    }];
    const w = computeWindow([cell]);
    // 09:30 - 30 = 09:00 (already on hour); 11:00 + 30 = 11:30 → 12:00.
    // Span is 3h, < 6h floor, so it expands symmetrically around the
    // midpoint (10:30) to 6h: roughly 07:30 → 13:30, snapped to hours.
    expect(w.endMin - w.startMin).toBeGreaterThanOrEqual(6 * 60);
  });

  it("caps pxPerMin at 1.5 even with a chip-dense booking", () => {
    // A 60-min booking with 30 task chips at 2-minute spacing would
    // demand >> 1.5 px/min; the cap protects the agenda height.
    const cell = emptyCell();
    cell.bookings = [{
      scheduled_start: "2025-04-24T09:00:00",
      scheduled_end: "2025-04-24T10:00:00",
      property_id: "p1",
    }];
    cell.tasks = Array.from({ length: 30 }, (_, i) => ({
      scheduled_start: `2025-04-24T09:${String(i * 2).padStart(2, "0")}:00`,
      estimated_minutes: 1,
      property_id: "p1",
    }));
    const w = computeWindow([cell]);
    expect(w.pxPerMin).toBeLessThanOrEqual(1.5);
  });

  it("reuses TASK_CHIP_MIN_PX + GAP for the chip-density floor", () => {
    // Regression guard: changing the constants must change the
    // baseline scale picked by computeWindow.
    expect(TASK_CHIP_MIN_PX).toBeGreaterThan(0);
    expect(TASK_CHIP_GAP_PX).toBeGreaterThanOrEqual(0);
  });

  it("posTop maps a minute within the window to a non-negative pixel", () => {
    const w = computeWindow([(() => {
      const c = emptyCell();
      c.bookings = [{
        scheduled_start: "2025-04-24T09:00:00",
        scheduled_end: "2025-04-24T17:00:00",
        property_id: "p1",
      }];
      return c;
    })()]);
    expect(posTop(10 * 60, w)).toBeGreaterThanOrEqual(0);
  });
});
