// Palette + property-name helpers for `/schedule` (§14 "Schedule view").
//
// The schedule's visual language hangs on a small five-tone palette —
// moss / sand / rust / sky / earth — that maps stably to the loaded
// property list. Indices in `PALETTE` and `PALETTE_SOLID` MUST stay in
// lockstep so the soft tint (rota cell background) and the solid tone
// (left tick, chip outline, task hairline) read as the same property.

import type { MySchedulePayload } from "@/types/api";

export const WEEKDAYS: { idx: number; short: string; long: string }[] = [
  { idx: 0, short: "Mon", long: "Monday" },
  { idx: 1, short: "Tue", long: "Tuesday" },
  { idx: 2, short: "Wed", long: "Wednesday" },
  { idx: 3, short: "Thu", long: "Thursday" },
  { idx: 4, short: "Fri", long: "Friday" },
  { idx: 5, short: "Sat", long: "Saturday" },
  { idx: 6, short: "Sun", long: "Sunday" },
];

export const PALETTE = [
  "rgba(63, 110, 59, 0.22)",  // moss
  "rgba(217, 164, 65, 0.28)", // sand
  "rgba(176, 74, 39, 0.22)",  // rust
  "rgba(79, 124, 168, 0.22)", // sky
  "rgba(146, 94, 57, 0.22)",  // earth
];

// Solid companions to PALETTE — full-opacity property colours used
// for left ticks, chip outlines, and the task-to-rota hairline.
// Indices MUST stay in lockstep with PALETTE.
export const PALETTE_SOLID = [
  "#3F6E3B", // moss
  "#D9A441", // sand
  "#B04A27", // rust
  "#4F7CA8", // sky
  "#925E39", // earth
];

export function propertyColor(pid: string, data: MySchedulePayload): string {
  const idx = data.properties.findIndex((p) => p.id === pid);
  if (idx < 0) return "var(--moss-soft)";
  return PALETTE[idx % PALETTE.length] ?? PALETTE[0]!;
}

export function propertySolid(pid: string, data: MySchedulePayload): string {
  const idx = data.properties.findIndex((p) => p.id === pid);
  if (idx < 0) return "var(--moss)";
  return PALETTE_SOLID[idx % PALETTE_SOLID.length] ?? PALETTE_SOLID[0]!;
}

export function propertyName(pid: string, data: MySchedulePayload): string {
  return data.properties.find((p) => p.id === pid)?.name ?? "—";
}
