// Date helpers shared between the desktop agenda + phone day view of
// `/schedule` (§14 "Schedule view"). Every function here works on
// LOCAL wall-clock dates — not UTC — because each cell key, the
// scroll-to-today anchor, and the page-param compares all key off the
// worker's "today", not the server's. `toISOString()` would shift to
// UTC and could drop a day for users west of UTC.
//
// Centralised so `DesktopAgenda`, `PhoneDay`, and the orchestrator
// can't disagree on what a Monday looks like.

export function startOfIsoWeek(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  const iso = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - iso);
  return out;
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

export function isoDate(d: Date): string {
  // Local-date ISO. `toISOString` would shift to UTC and could drop
  // a day for users west of UTC — every cell key, scroll target, and
  // page-param compare in the agenda relies on `YYYY-MM-DD` matching
  // the user's wall clock.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function parseIsoDate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y!, (m ?? 1) - 1, d ?? 1);
}

export function sameDate(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

export function isoWeekday(d: Date): number {
  return (d.getDay() + 6) % 7;
}

export function dayLabel(d: Date): { weekday: string; day: string; month: string } {
  return {
    weekday: d.toLocaleDateString("en-GB", { weekday: "short" }),
    day: String(d.getDate()),
    month: d.toLocaleDateString("en-GB", { month: "short" }),
  };
}

export function timeOfTask(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
