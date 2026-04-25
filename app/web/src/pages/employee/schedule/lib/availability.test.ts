// Unit tests for the schedule day-availability resolver (cd-ops1).
//
// The priority hierarchy (approved leave → pending leave → approved
// override → pending override → weekly pattern → "Off") is the §06
// "Approval logic (hybrid model)" rule. A regression here would
// silently flip a worker's day from "Vacation" to "Off" or vice
// versa, so each branch gets its own focused assertion.

import { describe, expect, it } from "vitest";
import type {
  AvailabilityOverride,
  Leave,
  SelfWeeklyAvailabilitySlot,
} from "@/types/api";
import { availability, hoursLabel } from "./availability";
import type { DayCell } from "./buildCells";

function makeCell(overrides: Partial<DayCell> = {}): DayCell {
  return {
    date: new Date(2025, 3, 24),
    iso: "2025-04-24",
    rota: [],
    tasks: [],
    leaves: [],
    overrides: [],
    bookings: [],
    pattern: null,
    ...overrides,
  };
}

function leave(over: Partial<Leave> = {}): Leave {
  return {
    id: "lv",
    employee_id: "emp",
    starts_on: "2025-04-24",
    ends_on: "2025-04-24",
    category: "vacation",
    note: "",
    approved_at: null,
    ...over,
  };
}

function override(over: Partial<AvailabilityOverride> = {}): AvailabilityOverride {
  return {
    id: "ao",
    user_id: "u1",
    workspace_id: "w1",
    date: "2025-04-24",
    available: true,
    starts_local: null,
    ends_local: null,
    reason: null,
    approval_required: false,
    approved_at: null,
    approved_by: null,
    created_at: "2025-04-24T00:00:00Z",
    ...over,
  };
}

function pattern(starts: string | null, ends: string | null): SelfWeeklyAvailabilitySlot {
  return { weekday: 3, starts_local: starts, ends_local: ends };
}

describe("availability — priority hierarchy", () => {
  it("approved leave wins over everything", () => {
    const a = availability(
      makeCell({
        leaves: [leave({ approved_at: "2025-04-20T00:00:00Z", category: "sick" })],
        overrides: [override({ approved_at: "2025-04-21T00:00:00Z", available: true, starts_local: "09:00", ends_local: "17:00" })],
        pattern: pattern("08:00", "16:00"),
      }),
    );
    expect(a.text).toBe("SICK");
    expect(a.tone).toBe("rust");
    expect(a.startMin).toBe(0);
    expect(a.endMin).toBe(24 * 60);
  });

  it("pending leave wins over override + pattern", () => {
    const a = availability(
      makeCell({
        leaves: [leave({ category: "personal" })],
        overrides: [override({ approved_at: "2025-04-21T00:00:00Z", starts_local: "09:00", ends_local: "17:00" })],
      }),
    );
    expect(a.text).toBe("PERSONAL · pending");
    expect(a.tone).toBe("sand");
  });

  it("approved 'OFF' override returns rust + null range", () => {
    const a = availability(
      makeCell({
        overrides: [override({
          approved_at: "2025-04-21T00:00:00Z",
          available: false,
        })],
      }),
    );
    expect(a.text).toBe("OFF");
    expect(a.tone).toBe("rust");
    expect(a.startMin).toBeNull();
    expect(a.endMin).toBeNull();
  });

  it("approved custom-hours override returns moss + the hour range", () => {
    const a = availability(
      makeCell({
        overrides: [override({
          approved_at: "2025-04-21T00:00:00Z",
          available: true,
          starts_local: "10:00",
          ends_local: "14:00",
        })],
      }),
    );
    expect(a.text).toBe("10:00–14:00");
    expect(a.tone).toBe("moss");
    expect(a.startMin).toBe(10 * 60);
    expect(a.endMin).toBe(14 * 60);
  });

  it("approved override falls back to pattern hours if its own hours are null", () => {
    const a = availability(
      makeCell({
        overrides: [override({ approved_at: "x", available: true })],
        pattern: pattern("09:00", "17:00"),
      }),
    );
    expect(a.text).toBe("09:00–17:00");
    expect(a.tone).toBe("moss");
  });

  it("pending OFF override carries · pending suffix + sand tone", () => {
    const a = availability(
      makeCell({ overrides: [override({ available: false })] }),
    );
    expect(a.text).toBe("OFF · pending");
    expect(a.tone).toBe("sand");
  });

  it("pending custom-hours override returns sand + range + suffix", () => {
    const a = availability(
      makeCell({
        overrides: [override({ available: true, starts_local: "10:00", ends_local: "14:00" })],
      }),
    );
    expect(a.text).toBe("10:00–14:00 · pending");
    expect(a.tone).toBe("sand");
    expect(a.startMin).toBe(10 * 60);
  });

  it("falls through to the weekly pattern when nothing else applies", () => {
    const a = availability(makeCell({ pattern: pattern("09:00", "17:00") }));
    expect(a.text).toBe("09:00–17:00");
    expect(a.tone).toBe("moss");
  });

  it("returns the 'Off' ghost fallback for a quiet day with no pattern", () => {
    const a = availability(makeCell({ pattern: pattern(null, null) }));
    expect(a.text).toBe("Off");
    expect(a.tone).toBe("ghost");
    expect(a.startMin).toBeNull();
  });
});

describe("hoursLabel", () => {
  it("strips the range, keeping only text + tone", () => {
    const out = hoursLabel(makeCell({ pattern: pattern("09:00", "17:00") }));
    expect(out).toEqual({ text: "09:00–17:00", tone: "moss" });
  });
});
