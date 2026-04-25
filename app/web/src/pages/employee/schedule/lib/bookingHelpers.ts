// Booking + timeline helpers for `/schedule` (§14 "Schedule view").
//
// `bookingMinutes` and `fmtDuration` drive the day drawer's
// per-booking duration label; `bookingNeedsAttention` and
// `BOOKING_STATUS_LABEL` drive the pending banner + chip text.
// `hhmmToMin`, `isoToMinOfDay`, `computeWindow`, `TASK_CHIP_*` build
// the per-week timeline geometry — a single window per ISO week so
// every cell in the desktop grid shares top/bottom hours and stays
// at the same height.

import type { Booking, BookingStatus } from "@/types/api";

interface DayCellLite {
  rota: { slot: { starts_local: string; ends_local: string } }[];
  tasks: { scheduled_start: string; estimated_minutes: number; property_id: string }[];
  bookings: Pick<Booking, "scheduled_start" | "scheduled_end" | "property_id">[];
  pattern: { starts_local: string | null; ends_local: string | null } | null;
}

export const BOOKING_STATUS_LABEL: Record<BookingStatus, string> = {
  pending_approval: "Pending approval",
  scheduled: "Scheduled",
  completed: "Completed",
  cancelled_by_client: "Cancelled (client)",
  cancelled_by_agency: "Cancelled (agency)",
  no_show_worker: "No-show",
  adjusted: "Completed (edited)",
};

export function bookingMinutes(b: Booking): number {
  if (b.actual_minutes_paid != null) return b.actual_minutes_paid;
  if (b.actual_minutes != null) return b.actual_minutes;
  const ms = new Date(b.scheduled_end).getTime() - new Date(b.scheduled_start).getTime();
  return Math.max(0, Math.round(ms / 60_000) - Math.round(b.break_seconds / 60));
}

export function fmtDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

export function fmtHM(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// "Needs attention" = a pending_approval row (ad-hoc proposal or a
// declined-and-unassigned one) OR a non-null pending self-amend the
// manager hasn't ruled on yet. Drives both the top banner count and
// the day-cell sand-edge modifier.
export function bookingNeedsAttention(b: Booking): boolean {
  return b.status === "pending_approval" || b.pending_amend_minutes != null;
}

export function hhmmToMin(s: string): number {
  const [h, m] = s.split(":").map((n) => Number(n));
  return (h ?? 0) * 60 + (m ?? 0);
}

export function isoToMinOfDay(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

// ── Timeline geometry ─────────────────────────────────────────────────
//
// The day-cell timeline is a vertical axis of minutes. Each loaded ISO
// week computes one `TimeWindow` covering every event in the week, so
// all seven desktop cells share the same top/bottom hours and render at
// identical height (required for a clean 7-col grid). Phone cards in
// the scrolling agenda use the same per-week window so switching weeks
// feels continuous.
//
// Scale is bounded: ~0.5 px/min with a 220–480px total clamp. Clamping
// keeps a quiet day readable without a 9h shift ballooning the agenda;
// an unusually long event simply compresses the scale rather than
// inflating the whole week.

export interface TimeWindow {
  startMin: number;
  endMin: number;
  pxPerMin: number;
  totalPx: number;
}

// Minimum pixel height a task chip needs to stay readable (time +
// one-line title). Used both to clamp chip size at render time and
// to compute how dense a booking's tasks are when picking the row
// scale.
export const TASK_CHIP_MIN_PX = 20;
export const TASK_CHIP_GAP_PX = 2;

export function computeWindow(cells: DayCellLite[]): TimeWindow {
  let minStart = Infinity;
  let maxEnd = -Infinity;
  for (const cell of cells) {
    for (const r of cell.rota) {
      minStart = Math.min(minStart, hhmmToMin(r.slot.starts_local));
      maxEnd = Math.max(maxEnd, hhmmToMin(r.slot.ends_local));
    }
    for (const b of cell.bookings) {
      minStart = Math.min(minStart, isoToMinOfDay(b.scheduled_start));
      maxEnd = Math.max(maxEnd, isoToMinOfDay(b.scheduled_end));
    }
    for (const t of cell.tasks) {
      const s = isoToMinOfDay(t.scheduled_start);
      const e = s + (t.estimated_minutes || 30);
      minStart = Math.min(minStart, s);
      maxEnd = Math.max(maxEnd, e);
    }
    // Weekly pattern keeps an "Off" or 09–17 week visually anchored
    // even when nothing concrete is scheduled.
    if (cell.pattern?.starts_local && cell.pattern.ends_local) {
      minStart = Math.min(minStart, hhmmToMin(cell.pattern.starts_local));
      maxEnd = Math.max(maxEnd, hhmmToMin(cell.pattern.ends_local));
    }
  }
  if (!isFinite(minStart) || !isFinite(maxEnd)) {
    // Fully empty week — default to a 9–17 office window so the hour
    // grid still renders as a "paused" planner page.
    minStart = 9 * 60;
    maxEnd = 17 * 60;
  }
  minStart = Math.max(0, Math.floor((minStart - 30) / 60) * 60);
  maxEnd = Math.min(24 * 60, Math.ceil((maxEnd + 30) / 60) * 60);
  if (maxEnd - minStart < 360) {
    // Ensure at least 6 hours visible so a single 9am task doesn't
    // render a pillbox-height timeline. Expand symmetrically around
    // the midpoint, clamped to [00:00, 24:00].
    const mid = (minStart + maxEnd) / 2;
    minStart = Math.max(0, Math.floor((mid - 180) / 60) * 60);
    maxEnd = Math.min(24 * 60, Math.ceil((mid + 180) / 60) * 60);
  }
  const totalMin = maxEnd - minStart;

  // Baseline scale keeps quiet weeks compact. When any booking in
  // the loaded set holds more tasks than its clock-time range can
  // show at that scale, bump the scale for the WHOLE row so all
  // seven cards keep sharing one top/bottom hour grid — the user's
  // "all cards in a week expand by the same so hours stay aligned"
  // rule.
  let pxPerMin = 0.5;
  for (const cell of cells) {
    for (const b of cell.bookings) {
      const bStart = isoToMinOfDay(b.scheduled_start);
      const bEnd = isoToMinOfDay(b.scheduled_end);
      const tasksInBooking = cell.tasks.filter((t) => {
        if (t.property_id !== b.property_id) return false;
        const tStart = isoToMinOfDay(t.scheduled_start);
        return tStart >= bStart && tStart < bEnd;
      });
      if (tasksInBooking.length === 0) continue;
      // Sort by start so we evaluate worst-case crowding at the
      // shortest gap between consecutive tasks (or between the first
      // task and the booking's start).
      const starts = tasksInBooking
        .map((t) => isoToMinOfDay(t.scheduled_start))
        .sort((a, b2) => a - b2);
      let minGap = Math.max(1, starts[0]! - bStart);
      for (let i = 1; i < starts.length; i++) {
        minGap = Math.min(minGap, starts[i]! - starts[i - 1]!);
      }
      // We need at least TASK_CHIP_MIN_PX of vertical space per gap
      // so consecutive task chips don't stack on top of each other.
      const needPerGap = (TASK_CHIP_MIN_PX + TASK_CHIP_GAP_PX) / minGap;
      if (needPerGap > pxPerMin) pxPerMin = needPerGap;
    }
  }
  // Cap the scale — past 1.5 px/min the canvas becomes taller than
  // the viewport and loses its "glanceable week" quality. An
  // unusually crowded booking (tasks every few minutes) would then
  // overflow its lane, but the drawer remains the canonical read
  // surface for those.
  pxPerMin = Math.min(pxPerMin, 1.5);

  const totalPx = totalMin * pxPerMin;
  return { startMin: minStart, endMin: maxEnd, pxPerMin, totalPx };
}

export function posTop(minutes: number, window: TimeWindow): number {
  return (minutes - window.startMin) * window.pxPerMin;
}
