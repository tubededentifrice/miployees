// Unit tests for the schedule palette helpers (cd-ops1).
//
// Verify the palette indices stay in lockstep, missing properties
// fall back to moss-soft, and the 5-tone palette wraps via modulo
// when the workspace has more properties than tones.

import { describe, expect, it } from "vitest";
import type { MySchedulePayload } from "@/types/api";
import {
  PALETTE,
  PALETTE_SOLID,
  WEEKDAYS,
  propertyColor,
  propertyName,
  propertySolid,
} from "./palette";

function makeData(propertyIds: string[]): MySchedulePayload {
  return {
    window: { from: "2025-04-21", to: "2025-04-27" },
    user_id: "u1",
    weekly_availability: [],
    rulesets: [],
    slots: [],
    assignments: [],
    tasks: [],
    properties: propertyIds.map((id) => ({ id, name: `Prop ${id}`, timezone: "UTC" })),
    leaves: [],
    overrides: [],
    bookings: [],
  };
}

describe("PALETTE / PALETTE_SOLID", () => {
  it("have matching lengths so soft + solid stay in lockstep", () => {
    expect(PALETTE).toHaveLength(PALETTE_SOLID.length);
  });
});

describe("WEEKDAYS", () => {
  it("indexes Monday=0..Sunday=6 with stable short labels", () => {
    expect(WEEKDAYS).toHaveLength(7);
    expect(WEEKDAYS[0]?.short).toBe("Mon");
    expect(WEEKDAYS[6]?.short).toBe("Sun");
  });
});

describe("propertyColor / propertySolid", () => {
  it("returns moss-soft / moss var when the property is unknown", () => {
    expect(propertyColor("missing", makeData(["a"]))).toBe("var(--moss-soft)");
    expect(propertySolid("missing", makeData(["a"]))).toBe("var(--moss)");
  });

  it("returns the same palette index for soft + solid", () => {
    const data = makeData(["a", "b", "c"]);
    expect(propertyColor("b", data)).toBe(PALETTE[1]);
    expect(propertySolid("b", data)).toBe(PALETTE_SOLID[1]);
  });

  it("wraps via modulo for properties beyond the 5-tone palette", () => {
    const data = makeData(["a", "b", "c", "d", "e", "f"]);
    // Index 5 wraps to PALETTE[0].
    expect(propertyColor("f", data)).toBe(PALETTE[0]);
    expect(propertySolid("f", data)).toBe(PALETTE_SOLID[0]);
  });
});

describe("propertyName", () => {
  it("returns the property's name", () => {
    expect(propertyName("a", makeData(["a"]))).toBe("Prop a");
  });

  it("falls back to em-dash when the property is unknown", () => {
    expect(propertyName("missing", makeData(["a"]))).toBe("—");
  });
});
