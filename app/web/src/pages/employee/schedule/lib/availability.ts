// Day availability resolver for `/schedule` (§14 "Schedule view").
//
// Single source of truth for the day's availability — used by the rail
// (bar + sideways text), the pending-day classname, and the empty-day
// fallback word. Returns a range in minutes-since-midnight so the rail
// bar can span exactly the shift's duration, in addition to the text
// and tone.
//
// The priority hierarchy (approved leave → pending leave → approved
// override → pending override → weekly pattern → "Off") matches §06
// "Approval logic (hybrid model)".

import { hhmmToMin } from "./bookingHelpers";
import type { DayCell } from "./buildCells";

type AvailTone = "moss" | "sand" | "rust" | "ghost";

export interface Availability {
  text: string;
  tone: AvailTone;
  startMin: number | null;
  endMin: number | null;
}

export function availability(cell: DayCell): Availability {
  const approvedLeave = cell.leaves.find((lv) => lv.approved_at !== null);
  if (approvedLeave) {
    return {
      text: approvedLeave.category.toUpperCase(),
      tone: "rust",
      startMin: 0,
      endMin: 24 * 60,
    };
  }
  const pendingLeave = cell.leaves.find((lv) => lv.approved_at === null);
  if (pendingLeave) {
    return {
      text: `${pendingLeave.category.toUpperCase()} · pending`,
      tone: "sand",
      startMin: 0,
      endMin: 24 * 60,
    };
  }

  const approvedOverride = cell.overrides.find((o) => o.approved_at !== null);
  if (approvedOverride) {
    if (!approvedOverride.available) {
      return { text: "OFF", tone: "rust", startMin: null, endMin: null };
    }
    const s = approvedOverride.starts_local ?? cell.pattern?.starts_local ?? null;
    const e = approvedOverride.ends_local ?? cell.pattern?.ends_local ?? null;
    if (s && e) {
      return {
        text: `${s}–${e}`,
        tone: "moss",
        startMin: hhmmToMin(s),
        endMin: hhmmToMin(e),
      };
    }
  }
  const pendingOverride = cell.overrides.find((o) => o.approved_at === null);
  if (pendingOverride) {
    if (!pendingOverride.available) {
      return { text: "OFF · pending", tone: "sand", startMin: null, endMin: null };
    }
    const s = pendingOverride.starts_local ?? cell.pattern?.starts_local ?? null;
    const e = pendingOverride.ends_local ?? cell.pattern?.ends_local ?? null;
    if (s && e) {
      return {
        text: `${s}–${e} · pending`,
        tone: "sand",
        startMin: hhmmToMin(s),
        endMin: hhmmToMin(e),
      };
    }
  }
  if (cell.pattern?.starts_local && cell.pattern.ends_local) {
    return {
      text: `${cell.pattern.starts_local}–${cell.pattern.ends_local}`,
      tone: "moss",
      startMin: hhmmToMin(cell.pattern.starts_local),
      endMin: hhmmToMin(cell.pattern.ends_local),
    };
  }
  return { text: "Off", tone: "ghost", startMin: null, endMin: null };
}

// Legacy-compat wrapper retained for call sites (DayCellView empty-day
// word, DayDrawer header) that only need the string/tone pair.
export function hoursLabel(cell: DayCell): { text: string; tone: AvailTone } {
  const a = availability(cell);
  return { text: a.text, tone: a.tone };
}
