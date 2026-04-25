// Unit tests for the schedule-page date helpers (cd-ops1).
//
// Covers the LOCAL-time invariants the agenda relies on: scrolling to
// "today" must not jump a day west of UTC, ISO weeks start on Monday,
// and `parseIsoDate ∘ isoDate` round-trips a `Date` faithfully.

import { describe, expect, it } from "vitest";
import {
  addDays,
  dayLabel,
  isoDate,
  isoWeekday,
  parseIsoDate,
  sameDate,
  startOfIsoWeek,
  timeOfTask,
} from "./dateHelpers";

describe("isoDate", () => {
  it("formats a local date as YYYY-MM-DD without UTC drift", () => {
    // Construct in local time so this passes regardless of host TZ.
    const d = new Date(2025, 0, 5, 23, 30, 0); // 5 Jan 2025 23:30 local
    expect(isoDate(d)).toBe("2025-01-05");
  });

  it("zero-pads single-digit months and days", () => {
    expect(isoDate(new Date(2025, 8, 9))).toBe("2025-09-09");
  });
});

describe("parseIsoDate", () => {
  it("round-trips with isoDate", () => {
    const d = new Date(2025, 3, 24);
    expect(isoDate(parseIsoDate(isoDate(d)))).toBe("2025-04-24");
  });

  it("returns midnight in local time", () => {
    const d = parseIsoDate("2025-04-24");
    expect(d.getHours()).toBe(0);
    expect(d.getMinutes()).toBe(0);
  });

  it("falls back gracefully when parts are missing", () => {
    // Defensive: malformed input must not throw — the agenda's
    // page-param compares feed back into `parseIsoDate` and a NaN
    // Date would propagate everywhere.
    const d = parseIsoDate("2025");
    expect(d.getFullYear()).toBe(2025);
    expect(d.getMonth()).toBe(0); // January
    expect(d.getDate()).toBe(1);
  });
});

describe("addDays", () => {
  it("adds positive days, including across months", () => {
    expect(isoDate(addDays(new Date(2025, 0, 30), 5))).toBe("2025-02-04");
  });

  it("handles negative offsets", () => {
    expect(isoDate(addDays(new Date(2025, 1, 1), -1))).toBe("2025-01-31");
  });

  it("does not mutate the input", () => {
    const d = new Date(2025, 3, 24);
    const before = d.getTime();
    addDays(d, 7);
    expect(d.getTime()).toBe(before);
  });
});

describe("startOfIsoWeek", () => {
  it("snaps Wednesday back to Monday", () => {
    // 24 Apr 2025 is a Thursday; the ISO week starts Mon 21 Apr.
    expect(isoDate(startOfIsoWeek(new Date(2025, 3, 24)))).toBe("2025-04-21");
  });

  it("snaps Sunday back to the prior Monday (ISO weeks end Sunday)", () => {
    // 27 Apr 2025 is a Sunday; ISO week start = Mon 21 Apr.
    expect(isoDate(startOfIsoWeek(new Date(2025, 3, 27)))).toBe("2025-04-21");
  });

  it("returns the same Monday when given a Monday", () => {
    expect(isoDate(startOfIsoWeek(new Date(2025, 3, 21)))).toBe("2025-04-21");
  });

  it("zeros the time-of-day", () => {
    const d = startOfIsoWeek(new Date(2025, 3, 24, 14, 32, 17));
    expect(d.getHours()).toBe(0);
    expect(d.getMinutes()).toBe(0);
    expect(d.getSeconds()).toBe(0);
  });
});

describe("isoWeekday", () => {
  it("returns 0 for Monday and 6 for Sunday", () => {
    expect(isoWeekday(new Date(2025, 3, 21))).toBe(0); // Mon
    expect(isoWeekday(new Date(2025, 3, 22))).toBe(1); // Tue
    expect(isoWeekday(new Date(2025, 3, 27))).toBe(6); // Sun
  });
});

describe("sameDate", () => {
  it("ignores time-of-day", () => {
    const a = new Date(2025, 3, 24, 0, 0, 0);
    const b = new Date(2025, 3, 24, 23, 59, 59);
    expect(sameDate(a, b)).toBe(true);
  });

  it("returns false across midnight", () => {
    expect(sameDate(new Date(2025, 3, 24), new Date(2025, 3, 25))).toBe(false);
  });
});

describe("dayLabel", () => {
  it("returns short weekday + numeric day + short month in en-GB", () => {
    const out = dayLabel(new Date(2025, 3, 24));
    // Locale-dependent string; assert structure rather than exact glyphs
    // (the runner's en-GB locale data is stable but case-sensitive).
    expect(out.day).toBe("24");
    expect(out.weekday.length).toBeGreaterThan(0);
    expect(out.month.length).toBeGreaterThan(0);
  });
});

describe("timeOfTask", () => {
  it("formats local HH:MM from an ISO timestamp", () => {
    // Construct from local components so the output is timezone-stable.
    const d = new Date(2025, 3, 24, 9, 5, 0);
    expect(timeOfTask(d.toISOString())).toBe(
      `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`,
    );
  });
});
