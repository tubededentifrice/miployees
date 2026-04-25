// Unit tests for the schedule cell + page-merge helpers (cd-ops1).
//
// `buildCells` is the centrepiece of the day-cell render; it takes
// the raw `MySchedulePayload` and slots each row into the day it
// belongs on. `mergeSchedulePages` is the infinite-scroll glue that
// stitches multiple paginated payloads into a single page-shaped
// payload.

import { describe, expect, it } from "vitest";
import type {
  AvailabilityOverride,
  Booking,
  Leave,
  MySchedulePayload,
  ScheduleAssignment,
  ScheduleRulesetSlot,
  SchedulerTaskView,
} from "@/types/api";
import { buildCells, mergeSchedulePages } from "./buildCells";

function emptyPayload(over: Partial<MySchedulePayload> = {}): MySchedulePayload {
  return {
    window: { from: "2025-04-21", to: "2025-04-27" },
    user_id: "u1",
    weekly_availability: [],
    rulesets: [],
    slots: [],
    assignments: [],
    tasks: [],
    properties: [],
    leaves: [],
    overrides: [],
    bookings: [],
    ...over,
  };
}

function slot(over: Partial<ScheduleRulesetSlot> = {}): ScheduleRulesetSlot {
  return {
    id: `slot-${Math.random()}`,
    schedule_ruleset_id: "rs1",
    weekday: 0,
    starts_local: "09:00",
    ends_local: "17:00",
    ...over,
  };
}

function assignment(over: Partial<ScheduleAssignment> = {}): ScheduleAssignment {
  return {
    id: `as-${Math.random()}`,
    user_id: "u1",
    work_role_id: null,
    property_id: "p1",
    schedule_ruleset_id: "rs1",
    ...over,
  };
}

function task(over: Partial<SchedulerTaskView> = {}): SchedulerTaskView {
  return {
    id: `t-${Math.random()}`,
    title: "Clean",
    property_id: "p1",
    user_id: "u1",
    scheduled_start: "2025-04-21T09:00:00",
    estimated_minutes: 30,
    priority: "normal",
    status: "scheduled",
    ...over,
  };
}

function leave(over: Partial<Leave> = {}): Leave {
  return {
    id: `lv-${Math.random()}`,
    employee_id: "emp",
    starts_on: "2025-04-22",
    ends_on: "2025-04-22",
    category: "vacation",
    note: "",
    approved_at: null,
    ...over,
  };
}

function override(over: Partial<AvailabilityOverride> = {}): AvailabilityOverride {
  return {
    id: `ao-${Math.random()}`,
    user_id: "u1",
    workspace_id: "w1",
    date: "2025-04-22",
    available: true,
    starts_local: null,
    ends_local: null,
    reason: null,
    approval_required: false,
    approved_at: null,
    approved_by: null,
    created_at: "2025-04-21T00:00:00Z",
    ...over,
  };
}

function booking(over: Partial<Booking> = {}): Booking {
  return {
    id: `b-${Math.random()}`,
    employee_id: "emp",
    property_id: "p1",
    scheduled_start: "2025-04-21T09:00:00",
    scheduled_end: "2025-04-21T11:00:00",
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
    ...over,
  };
}

describe("buildCells", () => {
  it("emits one cell per requested day in order", () => {
    const cells = buildCells(new Date(2025, 3, 21), 3, emptyPayload());
    expect(cells.map((c) => c.iso)).toEqual([
      "2025-04-21",
      "2025-04-22",
      "2025-04-23",
    ]);
  });

  it("buckets tasks by their local-ISO scheduled day", () => {
    const cells = buildCells(
      new Date(2025, 3, 21),
      3,
      emptyPayload({
        tasks: [
          task({ scheduled_start: "2025-04-21T09:00:00" }),
          task({ scheduled_start: "2025-04-22T09:00:00" }),
          task({ scheduled_start: "2025-04-22T15:00:00" }),
        ],
      }),
    );
    expect(cells[0]!.tasks).toHaveLength(1);
    expect(cells[1]!.tasks).toHaveLength(2);
    expect(cells[2]!.tasks).toHaveLength(0);
  });

  it("sorts tasks by scheduled_start ascending", () => {
    const cells = buildCells(
      new Date(2025, 3, 21),
      1,
      emptyPayload({
        tasks: [
          task({ scheduled_start: "2025-04-21T15:00:00" }),
          task({ scheduled_start: "2025-04-21T09:00:00" }),
        ],
      }),
    );
    expect(cells[0]!.tasks.map((t) => t.scheduled_start)).toEqual([
      "2025-04-21T09:00:00",
      "2025-04-21T15:00:00",
    ]);
  });

  it("attaches rota slots through their assignment's property_id", () => {
    const s = slot({ id: "s1", weekday: 0, schedule_ruleset_id: "rs1" });
    const cells = buildCells(
      new Date(2025, 3, 21),
      1,
      emptyPayload({
        slots: [s],
        assignments: [assignment({ schedule_ruleset_id: "rs1", property_id: "p1" })],
      }),
    );
    expect(cells[0]!.rota).toEqual([{ slot: s, property_id: "p1" }]);
  });

  it("drops rota rows whose assignment has no property", () => {
    const cells = buildCells(
      new Date(2025, 3, 21),
      1,
      emptyPayload({
        slots: [slot({ id: "s1", weekday: 0, schedule_ruleset_id: "rs1" })],
        // No assignment for rs1 — rota should be empty.
      }),
    );
    expect(cells[0]!.rota).toEqual([]);
  });

  it("includes leaves whose date range covers the day", () => {
    const cells = buildCells(
      new Date(2025, 3, 21),
      3,
      emptyPayload({
        leaves: [leave({ starts_on: "2025-04-22", ends_on: "2025-04-23" })],
      }),
    );
    expect(cells[0]!.leaves).toHaveLength(0);
    expect(cells[1]!.leaves).toHaveLength(1);
    expect(cells[2]!.leaves).toHaveLength(1);
  });

  it("only includes overrides whose date matches", () => {
    const cells = buildCells(
      new Date(2025, 3, 21),
      3,
      emptyPayload({ overrides: [override({ date: "2025-04-22" })] }),
    );
    expect(cells[0]!.overrides).toHaveLength(0);
    expect(cells[1]!.overrides).toHaveLength(1);
  });

  it("buckets bookings by local-ISO start day, sorted by start", () => {
    const cells = buildCells(
      new Date(2025, 3, 21),
      1,
      emptyPayload({
        bookings: [
          booking({ scheduled_start: "2025-04-21T15:00:00", scheduled_end: "2025-04-21T17:00:00" }),
          booking({ scheduled_start: "2025-04-21T09:00:00", scheduled_end: "2025-04-21T11:00:00" }),
        ],
      }),
    );
    expect(cells[0]!.bookings.map((b) => b.scheduled_start)).toEqual([
      "2025-04-21T09:00:00",
      "2025-04-21T15:00:00",
    ]);
  });

  it("attaches the matching weekly pattern when the weekday lines up", () => {
    const cells = buildCells(
      new Date(2025, 3, 21), // Monday → ISO weekday 0
      2,
      emptyPayload({
        weekly_availability: [{ weekday: 0, starts_local: "09:00", ends_local: "17:00" }],
      }),
    );
    expect(cells[0]!.pattern).toEqual({ weekday: 0, starts_local: "09:00", ends_local: "17:00" });
    expect(cells[1]!.pattern).toBeNull();
  });
});

describe("mergeSchedulePages", () => {
  it("returns null on an empty input", () => {
    expect(mergeSchedulePages([])).toBeNull();
  });

  it("returns the lone page unchanged when only one is given", () => {
    const p = emptyPayload();
    expect(mergeSchedulePages([p])).toBe(p);
  });

  it("dedupes per-page collections by id while preserving order", () => {
    const a = emptyPayload({
      tasks: [task({ id: "t1" }), task({ id: "t2" })],
      bookings: [booking({ id: "b1" })],
      leaves: [leave({ id: "l1" })],
      overrides: [override({ id: "o1" })],
    });
    const b = emptyPayload({
      tasks: [task({ id: "t2" }), task({ id: "t3" })],
      bookings: [booking({ id: "b1" }), booking({ id: "b2" })],
      leaves: [leave({ id: "l1" }), leave({ id: "l2" })],
      overrides: [override({ id: "o1" })],
    });
    const merged = mergeSchedulePages([a, b])!;
    expect(merged.tasks.map((t) => t.id)).toEqual(["t1", "t2", "t3"]);
    expect(merged.bookings.map((b2) => b2.id)).toEqual(["b1", "b2"]);
    expect(merged.leaves.map((l) => l.id)).toEqual(["l1", "l2"]);
    expect(merged.overrides.map((o) => o.id)).toEqual(["o1"]);
  });

  it("spans the merged window from first.from to last.to", () => {
    const a = emptyPayload({ window: { from: "2025-04-14", to: "2025-04-20" } });
    const b = emptyPayload({ window: { from: "2025-04-21", to: "2025-04-27" } });
    expect(mergeSchedulePages([a, b])!.window).toEqual({
      from: "2025-04-14",
      to: "2025-04-27",
    });
  });

  it("uses the first page's weekly_availability + user_id", () => {
    const a = emptyPayload({
      user_id: "first",
      weekly_availability: [{ weekday: 0, starts_local: "09:00", ends_local: "17:00" }],
    });
    const b = emptyPayload({ user_id: "second" });
    const merged = mergeSchedulePages([a, b])!;
    expect(merged.user_id).toBe("first");
    expect(merged.weekly_availability).toHaveLength(1);
  });
});
