import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import PageHeader from "@/components/PageHeader";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import { LeaveDialog, OverrideDialog } from "@/components/ScheduleDialogs";
import { useRole } from "@/context/RoleContext";
import type {
  AvailabilityOverride,
  Booking,
  BookingStatus,
  Leave,
  Me,
  MySchedulePayload,
  ScheduleRulesetSlot,
  SchedulerTaskView,
  SelfWeeklyAvailabilitySlot,
} from "@/types/api";

// §14 "Schedule view". Self-only calendar hub that replaces the old
// `/week` flat list, the `/me/schedule` alias, and the retired
// `/bookings` page. Phone and desktop both render a continuous
// agenda backed by a bidirectional infinite query (7-day pages): the
// worker lands on today, scrolls up to past weeks, scrolls down to
// load the next. Phone stacks days as cards; desktop stacks 7-column
// Mon..Sun grids, one per ISO week. Click a day anywhere to open the
// shared day drawer with rota, tasks, bookings (§09, amend/decline
// inline), plus the Request-leave / Request-override forms. A
// pending banner sits above the agenda whenever any booking in the
// loaded window is pending_approval or has a pending self-amend —
// so a stale approval can't fall off-screen. See spec §06 for the
// approval rules and §09 for the booking lifecycle.

const BOOKING_STATUS_LABEL: Record<BookingStatus, string> = {
  pending_approval: "Pending approval",
  scheduled: "Scheduled",
  completed: "Completed",
  cancelled_by_client: "Cancelled (client)",
  cancelled_by_agency: "Cancelled (agency)",
  no_show_worker: "No-show",
  adjusted: "Completed (edited)",
};

function bookingMinutes(b: Booking): number {
  if (b.actual_minutes_paid != null) return b.actual_minutes_paid;
  if (b.actual_minutes != null) return b.actual_minutes;
  const ms = new Date(b.scheduled_end).getTime() - new Date(b.scheduled_start).getTime();
  return Math.max(0, Math.round(ms / 60_000) - Math.round(b.break_seconds / 60));
}

function fmtDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

function fmtHM(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// "Needs attention" = a pending_approval row (ad-hoc proposal or a
// declined-and-unassigned one) OR a non-null pending self-amend the
// manager hasn't ruled on yet. Drives both the top banner count and
// the day-cell sand-edge modifier.
function bookingNeedsAttention(b: Booking): boolean {
  return b.status === "pending_approval" || b.pending_amend_minutes != null;
}

const WEEKDAYS: { idx: number; short: string; long: string }[] = [
  { idx: 0, short: "Mon", long: "Monday" },
  { idx: 1, short: "Tue", long: "Tuesday" },
  { idx: 2, short: "Wed", long: "Wednesday" },
  { idx: 3, short: "Thu", long: "Thursday" },
  { idx: 4, short: "Fri", long: "Friday" },
  { idx: 5, short: "Sat", long: "Saturday" },
  { idx: 6, short: "Sun", long: "Sunday" },
];

const PALETTE = [
  "rgba(63, 110, 59, 0.22)",  // moss
  "rgba(217, 164, 65, 0.28)", // sand
  "rgba(176, 74, 39, 0.22)",  // rust
  "rgba(79, 124, 168, 0.22)", // sky
  "rgba(146, 94, 57, 0.22)",  // earth
];

// Solid companions to PALETTE — full-opacity property colours used
// for left ticks, chip outlines, and the task-to-rota hairline.
// Indices MUST stay in lockstep with PALETTE.
const PALETTE_SOLID = [
  "#3F6E3B", // moss
  "#D9A441", // sand
  "#B04A27", // rust
  "#4F7CA8", // sky
  "#925E39", // earth
];

function startOfIsoWeek(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  const iso = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - iso);
  return out;
}

function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

function isoDate(d: Date): string {
  // Local-date ISO. `toISOString` would shift to UTC and could drop
  // a day for users west of UTC — every cell key, scroll target, and
  // page-param compare in the agenda relies on `YYYY-MM-DD` matching
  // the user's wall clock.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function parseIsoDate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y!, (m ?? 1) - 1, d ?? 1);
}

// Phone vs desktop split. Mirrors the `(min-width: 720px)` breakpoint
// used by `.schedule--phone` / `.schedule--desktop` in CSS so the
// per-variant layout lines up with what `useIsPhone` reports. Both
// variants run the same bidirectional infinite query; only the
// per-week rendering differs.
function useIsPhone(): boolean {
  const query = "(max-width: 719px)";
  const [isPhone, setIsPhone] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(query).matches;
  });
  useEffect(() => {
    const mq = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setIsPhone(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isPhone;
}

function dayLabel(d: Date): { weekday: string; day: string; month: string } {
  return {
    weekday: d.toLocaleDateString("en-GB", { weekday: "short" }),
    day: String(d.getDate()),
    month: d.toLocaleDateString("en-GB", { month: "short" }),
  };
}

function timeOfTask(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function isoWeekday(d: Date): number {
  return (d.getDay() + 6) % 7;
}

function sameDate(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

interface DayCell {
  date: Date;
  iso: string;
  rota: { slot: ScheduleRulesetSlot; property_id: string }[];
  tasks: SchedulerTaskView[];
  leaves: Leave[];
  overrides: AvailabilityOverride[];
  bookings: Booking[];
  pattern: SelfWeeklyAvailabilitySlot | null;
}

function buildCells(
  from: Date,
  days: number,
  data: MySchedulePayload,
): DayCell[] {
  const cells: DayCell[] = [];
  const assignmentProperty = new Map<string, string>();
  data.assignments.forEach((a) => {
    if (a.schedule_ruleset_id) assignmentProperty.set(a.schedule_ruleset_id, a.property_id);
  });
  const weeklyByDay = new Map<number, SelfWeeklyAvailabilitySlot>(
    data.weekly_availability.map((w) => [w.weekday, w]),
  );
  for (let i = 0; i < days; i++) {
    const d = addDays(from, i);
    const iso = isoDate(d);
    const wd = isoWeekday(d);
    const rota = data.slots
      .filter((s) => s.weekday === wd)
      .map((s) => ({
        slot: s,
        property_id: assignmentProperty.get(s.schedule_ruleset_id) ?? "",
      }))
      .filter((r) => r.property_id);
    const tasks = data.tasks
      .filter((t) => t.scheduled_start.slice(0, 10) === iso)
      .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
    const leaves = data.leaves.filter(
      (lv) => lv.starts_on <= iso && lv.ends_on >= iso,
    );
    const overrides = data.overrides.filter((ao) => ao.date === iso);
    const bookings = data.bookings
      .filter((b) => b.scheduled_start.slice(0, 10) === iso)
      .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
    cells.push({
      date: d,
      iso,
      rota,
      tasks,
      leaves,
      overrides,
      bookings,
      pattern: weeklyByDay.get(wd) ?? null,
    });
  }
  return cells;
}

// Concatenate `useInfiniteQuery` pages into the same shape one /me/
// schedule call would return. Per-page collections (tasks, bookings,
// leaves, overrides) get id-deduped — the API filters by date so
// duplicates are unlikely, but a refetch overlap shouldn't crash the
// drawer. Workspace-stable rows (properties, rulesets, assignments,
// slots, weekly_availability) come from the first page.
function mergeSchedulePages(pages: MySchedulePayload[]): MySchedulePayload | null {
  if (pages.length === 0) return null;
  const first = pages[0]!;
  if (pages.length === 1) return first;
  const last = pages[pages.length - 1]!;
  const dedup = <T,>(items: T[], key: (t: T) => string): T[] => {
    const seen = new Set<string>();
    const out: T[] = [];
    for (const it of items) {
      const k = key(it);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(it);
    }
    return out;
  };
  return {
    window: { from: first.window.from, to: last.window.to },
    user_id: first.user_id,
    weekly_availability: first.weekly_availability,
    rulesets: dedup(pages.flatMap((p) => p.rulesets), (r) => r.id),
    slots: dedup(pages.flatMap((p) => p.slots), (s) => s.id),
    assignments: dedup(pages.flatMap((p) => p.assignments), (a) => a.id),
    tasks: dedup(pages.flatMap((p) => p.tasks), (t) => t.id),
    properties: dedup(pages.flatMap((p) => p.properties), (p) => p.id),
    leaves: dedup(pages.flatMap((p) => p.leaves), (lv) => lv.id),
    overrides: dedup(pages.flatMap((p) => p.overrides), (o) => o.id),
    bookings: dedup(pages.flatMap((p) => p.bookings), (b) => b.id),
  };
}

function propertyColor(pid: string, data: MySchedulePayload): string {
  const idx = data.properties.findIndex((p) => p.id === pid);
  if (idx < 0) return "var(--moss-soft)";
  return PALETTE[idx % PALETTE.length] ?? PALETTE[0]!;
}

function propertySolid(pid: string, data: MySchedulePayload): string {
  const idx = data.properties.findIndex((p) => p.id === pid);
  if (idx < 0) return "var(--moss)";
  return PALETTE_SOLID[idx % PALETTE_SOLID.length] ?? PALETTE_SOLID[0]!;
}

function propertyName(pid: string, data: MySchedulePayload): string {
  return data.properties.find((p) => p.id === pid)?.name ?? "—";
}

type AvailTone = "moss" | "sand" | "rust" | "ghost";

// Single source of truth for the day's availability — used by the rail
// (bar + sideways text), the pending-day classname, and the empty-day
// fallback word. Returns a range in minutes-since-midnight so the rail
// bar can span exactly the shift's duration, in addition to the text
// and tone. `null` on the range means "no bar" (e.g. no shift set).
interface Availability {
  text: string;
  tone: AvailTone;
  startMin: number | null;
  endMin: number | null;
}

function availability(cell: DayCell): Availability {
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
function hoursLabel(cell: DayCell): { text: string; tone: AvailTone } {
  const a = availability(cell);
  return { text: a.text, tone: a.tone };
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

interface TimeWindow {
  startMin: number;
  endMin: number;
  pxPerMin: number;
  totalPx: number;
}

function hhmmToMin(s: string): number {
  const [h, m] = s.split(":").map((n) => Number(n));
  return (h ?? 0) * 60 + (m ?? 0);
}

function isoToMinOfDay(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

// Minimum pixel height a task chip needs to stay readable (time +
// one-line title). Used both to clamp chip size at render time and
// to compute how dense a booking's tasks are when picking the row
// scale.
const TASK_CHIP_MIN_PX = 20;
const TASK_CHIP_GAP_PX = 2;

function computeWindow(cells: DayCell[]): TimeWindow {
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

function posTop(minutes: number, window: TimeWindow): number {
  return (minutes - window.startMin) * window.pxPerMin;
}

function taskTime(task: SchedulerTaskView): string {
  return timeOfTask(task.scheduled_start);
}

// Bookings are the authoritative render of a rota slot for a date
// (§09 "Nightly materialiser"). A rota slot only renders when no
// booking covers its time range — so the worker sees tomorrow's shift
// even before the nightly job has cut the booking.
function uncoveredRotaFor(cell: DayCell): { slot: ScheduleRulesetSlot; property_id: string }[] {
  return cell.rota.filter((r) => {
    const rs = hhmmToMin(r.slot.starts_local);
    const re = hhmmToMin(r.slot.ends_local);
    return !cell.bookings.some((b) => {
      const bs = isoToMinOfDay(b.scheduled_start);
      const be = isoToMinOfDay(b.scheduled_end);
      return Math.max(bs, rs) < Math.min(be, re);
    });
  });
}

// A "container" is any block that can host tasks on the timeline — a
// materialised booking or an uncovered rota slot. Tasks bind to a
// container by property_id + time overlap (per §06: a task belongs to
// the shift it runs during, not to a standalone clock time).
interface TaskContainer {
  id: string;
  kind: "booking" | "rota";
  startMin: number;
  endMin: number;
  propertyId: string;
  tint: string;
  tintSolid: string;
}

function buildTaskLanes(
  cell: DayCell,
  data: MySchedulePayload,
): {
  containers: TaskContainer[];
  byContainer: Map<string, SchedulerTaskView[]>;
  orphans: SchedulerTaskView[];
} {
  const containers: TaskContainer[] = [
    ...cell.bookings.map<TaskContainer>((b) => ({
      id: `book-${b.id}`,
      kind: "booking",
      startMin: isoToMinOfDay(b.scheduled_start),
      endMin: isoToMinOfDay(b.scheduled_end),
      propertyId: b.property_id,
      tint: propertyColor(b.property_id, data),
      tintSolid: propertySolid(b.property_id, data),
    })),
    ...uncoveredRotaFor(cell).map<TaskContainer>((r) => ({
      id: `rota-${r.slot.id}`,
      kind: "rota",
      startMin: hhmmToMin(r.slot.starts_local),
      endMin: hhmmToMin(r.slot.ends_local),
      propertyId: r.property_id,
      tint: propertyColor(r.property_id, data),
      tintSolid: propertySolid(r.property_id, data),
    })),
  ];

  const byContainer = new Map<string, SchedulerTaskView[]>();
  const orphans: SchedulerTaskView[] = [];

  for (const t of cell.tasks) {
    const tStart = isoToMinOfDay(t.scheduled_start);
    const match = containers.find(
      (c) =>
        c.propertyId === t.property_id
        && tStart >= c.startMin
        && tStart < c.endMin,
    );
    if (match) {
      const arr = byContainer.get(match.id) ?? [];
      arr.push(t);
      byContainer.set(match.id, arr);
    } else {
      orphans.push(t);
    }
  }
  for (const arr of byContainer.values()) {
    arr.sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
  }
  orphans.sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
  return { containers, byContainer, orphans };
}

function DayTimelineBlocks({
  cell,
  data,
  window: w,
}: {
  cell: DayCell;
  data: MySchedulePayload;
  window: TimeWindow;
}) {
  // Tasks live INSIDE their parent container now — a task chip is a
  // child of its booking/rota block, positioned by clock time relative
  // to the block's start. Orphan tasks (no parent) render as
  // standalone chips pinned to their own clock time.
  const { containers, byContainer, orphans } = buildTaskLanes(cell, data);
  const bookingsById = new Map(cell.bookings.map((b) => [b.id, b]));
  const uncoveredById = new Map(
    uncoveredRotaFor(cell).map((r) => [r.slot.id, r]),
  );
  let flatIdx = 0;

  return (
    <>
      {containers.map((c, idx) => {
        const top = posTop(c.startMin, w);
        const height = Math.max(22, (c.endMin - c.startMin) * w.pxPerMin);
        const tasks = byContainer.get(c.id) ?? [];
        const isBooking = c.kind === "booking";
        const booking = isBooking
          ? bookingsById.get(c.id.slice("book-".length))
          : undefined;
        const rota = !isBooking
          ? uncoveredById.get(c.id.slice("rota-".length))
          : undefined;
        const pending = booking ? bookingNeedsAttention(booking) : false;
        const cancelled = booking
          ? booking.status === "cancelled_by_client"
            || booking.status === "cancelled_by_agency"
            || booking.status === "no_show_worker"
          : false;
        const mods =
          (isBooking ? " schedule-day__block--booking" : " schedule-day__block--rota")
          + (pending ? " schedule-day__block--pending" : "")
          + (cancelled ? " schedule-day__block--cancelled" : "");
        const timeText = booking
          ? `${fmtHM(booking.scheduled_start)}–${fmtHM(booking.scheduled_end)}`
          : rota
            ? `${rota.slot.starts_local}–${rota.slot.ends_local}`
            : "";
        return (
          <div
            key={c.id}
            className={`schedule-day__block${mods}`}
            style={{
              top: `${top}px`,
              height: `${height}px`,
              "--rota-tint": c.tint,
              "--rota-tint-solid": c.tintSolid,
              "--rota-idx": idx,
            } as React.CSSProperties}
            data-property={c.propertyId}
          >
            {/* Property name floats above the block — its baseline sits
                on the block's top border, like a ledger tab glued to a
                file folder. The block is the content; the label names
                the "where" in italic serif. */}
            <span className="schedule-day__block-prop">
              {propertyName(c.propertyId, data)}
            </span>
            {height >= 44 && timeText && (
              <span className="schedule-day__block-time">{timeText}</span>
            )}
            {pending && (
              <span className="schedule-day__block-flag">pending</span>
            )}
            {cancelled && booking && (
              <span className="schedule-day__block-flag schedule-day__block-flag--rust">
                {booking.status === "no_show_worker" ? "no-show" : "cancelled"}
              </span>
            )}
            {tasks.map((t) => {
              // Position the chip at its scheduled clock time,
              // measured from the block's top. computeWindow has
              // picked a row-wide pxPerMin that gives enough
              // vertical space between consecutive tasks inside any
              // booking, so chips inside one block rarely collide.
              const tStart = isoToMinOfDay(t.scheduled_start);
              const chipTop = Math.max(0, (tStart - c.startMin) * w.pxPerMin);
              return (
                <TaskChipLink
                  key={t.id}
                  task={t}
                  tint={c.tint}
                  solid={c.tintSolid}
                  idx={flatIdx++}
                  top={chipTop}
                />
              );
            })}
          </div>
        );
      })}
      {orphans.map((t) => {
        // Orphan tasks have no parent block to live inside — pin them
        // directly to the rota column at their own clock time. The
        // --orphan modifier gives them the rust dashed outline so they
        // read as "outside the shift" without needing a lane.
        const tStart = isoToMinOfDay(t.scheduled_start);
        const tint = propertyColor(t.property_id, data);
        const solid = propertySolid(t.property_id, data);
        return (
          <TaskChipLink
            key={`orphan-${t.id}`}
            task={t}
            tint={tint}
            solid={solid}
            idx={flatIdx++}
            top={posTop(tStart, w)}
            orphan
            standalone
          />
        );
      })}
    </>
  );
}

function TaskChipLink({
  task,
  tint,
  solid,
  idx,
  orphan,
  standalone,
  top,
}: {
  task: SchedulerTaskView;
  tint: string;
  solid: string;
  idx: number;
  orphan?: boolean;
  /** Orphan tasks live in the rota column (no parent block). The
   *  --standalone modifier keeps them visually distinct and sets the
   *  background against the column, not the tinted block. */
  standalone?: boolean;
  /** Pixels down from the positioned ancestor (block for in-block
   *  chips, rota column for orphans). Computed from the task's
   *  clock-time start so the chip sits on the right hour. */
  top: number;
}) {
  return (
    <Link
      to={"/task/" + task.id}
      className={
        `schedule-day__chip schedule-day__chip--${task.status}`
        + (orphan ? " schedule-day__chip--orphan" : "")
        + (standalone ? " schedule-day__chip--standalone" : "")
      }
      style={{
        top: `${top}px`,
        "--rota-tint": tint,
        "--rota-tint-solid": solid,
        "--rota-idx": idx,
      } as React.CSSProperties}
      data-property={task.property_id}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      <span className="schedule-day__chip-time">{taskTime(task)}</span>
      <span className="schedule-day__chip-title">{task.title}</span>
    </Link>
  );
}

function DayTimeline({
  cell,
  data,
  window: w,
}: {
  cell: DayCell;
  data: MySchedulePayload;
  window: TimeWindow;
}) {
  const avail = availability(cell);
  // Hour labels at EVERY hour in the visible window. Major hours
  // (every 3rd) render slightly bolder so the eye still gets anchor
  // points, while minor hours are rendered faint — together they
  // make rota blocks read as sitting on a specific clock time rather
  // than floating between unlabeled gridlines.
  const labels: { hour: number; top: number; major: boolean }[] = [];
  for (let h = Math.ceil(w.startMin / 60); h * 60 <= w.endMin; h++) {
    labels.push({ hour: h, top: posTop(h * 60, w), major: h % 3 === 0 });
  }
  // Rail duration bar. Clamp to the visible window — a 24h leave bar
  // fills the canvas; a 08:00–17:00 shift bar spans just that slice.
  // Null range (worker "Off") renders no bar, only the sideways text.
  const railBar = (() => {
    if (avail.startMin == null || avail.endMin == null) return null;
    const barStart = Math.max(avail.startMin, w.startMin);
    const barEnd = Math.min(avail.endMin, w.endMin);
    if (barEnd <= barStart) return null;
    return {
      top: posTop(barStart, w),
      height: (barEnd - barStart) * w.pxPerMin,
    };
  })();
  return (
    <div
      className="schedule-day__timeline"
      style={{
        height: `${w.totalPx}px`,
        "--px-per-hour": `${w.pxPerMin * 60}px`,
      } as React.CSSProperties}
    >
      <div
        className={`schedule-day__rail schedule-day__rail--${avail.tone}`}
        aria-label={`Availability ${avail.text}`}
      >
        {railBar && (
          <div
            className={`schedule-day__rail-bar schedule-day__rail-bar--${avail.tone}`}
            style={{ top: `${railBar.top}px`, height: `${railBar.height}px` }}
            aria-hidden
          />
        )}
        <span
          className="schedule-day__rail-text"
          style={railBar ? {
            top: `${railBar.top + railBar.height / 2}px`,
          } : undefined}
        >
          {avail.text}
        </span>
      </div>
      <div className="schedule-day__hours" aria-hidden>
        {labels.map((l) => (
          <span
            key={l.hour}
            className={
              "schedule-day__hour"
              + (l.major ? " schedule-day__hour--major" : "")
            }
            style={{ top: `${l.top}px` }}
          >
            {String(l.hour).padStart(2, "0")}
          </span>
        ))}
      </div>
      <div className="schedule-day__rota-col">
        <DayTimelineBlocks cell={cell} data={data} window={w} />
      </div>
    </div>
  );
}

function DayCellView({
  cell,
  data,
  onOpen,
  today,
  window: w,
  collapseEmpty,
}: {
  cell: DayCell;
  data: MySchedulePayload;
  onOpen: (iso: string) => void;
  today: Date;
  window: TimeWindow;
  /** Phone-only: when a day has no rota, bookings, or tasks, drop the
   *  timeline and render a short "rest" card. Desktop cells keep the
   *  full-height skeleton so the 7-col week row stays uniform. */
  collapseEmpty: boolean;
}) {
  const isToday = sameDate(cell.date, today);
  const label = dayLabel(cell.date);
  const pendingBookings = cell.bookings.filter(bookingNeedsAttention);
  const empty =
    cell.tasks.length === 0
    && cell.rota.length === 0
    && cell.bookings.length === 0;
  const { text: emptyAvail, tone: emptyTone } = hoursLabel(cell);
  const emptyLabel = (() => {
    if (!empty) return null;
    if (cell.leaves.some((lv) => lv.approved_at !== null)) return "leave";
    if (cell.overrides.some((o) => o.available === false && o.approved_at !== null)) return "off";
    if (emptyTone === "ghost") return "rest";
    return "quiet";
  })();
  return (
    <div
      role="button"
      tabIndex={0}
      data-schedule-iso={cell.iso}
      aria-label={
        `Open schedule for ${label.weekday} ${label.day} ${label.month}`
        + (pendingBookings.length > 0
          ? ` — ${pendingBookings.length} booking${pendingBookings.length === 1 ? "" : "s"} ${pendingBookings.length === 1 ? "needs" : "need"} attention`
          : "")
      }
      className={
        "schedule-day" +
        (isToday ? " schedule-day--today" : "") +
        (empty ? " schedule-day--empty" : "") +
        (empty && collapseEmpty ? " schedule-day--collapsed" : "") +
        (pendingBookings.length > 0 ? " schedule-day--pending" : "")
      }
      onClick={(e) => {
        // Nested <Link>s (task chips) keep their own navigation; a
        // click on the card background opens the drawer.
        if ((e.target as HTMLElement).closest("a")) return;
        onOpen(cell.iso);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(cell.iso);
        }
      }}
    >
      <div className="schedule-day__header">
        <span className="schedule-day__wd">{label.weekday}</span>
        <span className="schedule-day__num">{label.day}</span>
        <span className="schedule-day__mo">{label.month}</span>
      </div>
      {empty && collapseEmpty ? (
        <div className="schedule-day__empty">
          <span className={`schedule-day__empty-label schedule-day__empty-label--${emptyTone}`}>
            {emptyLabel}
          </span>
          {emptyAvail !== "Off" && (
            <span className="schedule-day__empty-hours">{emptyAvail}</span>
          )}
        </div>
      ) : (
        <DayTimeline cell={cell} data={data} window={w} />
      )}
      {empty && !collapseEmpty && (
        // Desktop-only: word centered over the ghost hour grid so the
        // cell reads as a paused page, not a broken render.
        <span
          className={`schedule-day__rest-word schedule-day__rest-word--${emptyTone}`}
          aria-hidden
        >
          {emptyLabel}
        </span>
      )}
    </div>
  );
}

// ── Day drawer ────────────────────────────────────────────────────────

function DayDrawer({
  cell,
  data,
  onClose,
  onRequestLeave,
  onRequestOverride,
  onProposeBooking,
}: {
  cell: DayCell | null;
  data: MySchedulePayload;
  onClose: () => void;
  onRequestLeave: (iso: string) => void;
  onRequestOverride: (iso: string) => void;
  onProposeBooking: (iso: string) => void;
}) {
  const qc = useQueryClient();

  // §09 amend and decline. Self-amend above the threshold goes
  // straight to `pending_amend_*`, below it mutates actuals directly
  // — the server decides, we just post. The mock endpoint does the
  // simpler "applies whatever you send" behaviour; production does
  // the real §09 gating.
  const amendMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      fetchJson<Booking>(`/api/v1/bookings/${id}/amend`, {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["my-schedule"] });
      qc.invalidateQueries({ queryKey: qk.bookings() });
    },
  });

  const declineMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      fetchJson<Booking>(`/api/v1/bookings/${id}/decline`, {
        method: "POST",
        body: { reason },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["my-schedule"] });
      qc.invalidateQueries({ queryKey: qk.bookings() });
    },
  });

  // A per-day window for the drawer's hero timeline — it sits in its
  // own context so it doesn't need to share scale with the agenda
  // behind it. The empty-day fallback keeps the section silent if the
  // worker opened a rest day (the hero would otherwise look broken).
  const drawerWindow = useMemo(() => (cell ? computeWindow([cell]) : null), [cell]);

  // Universal Esc-to-close — matches the inventory drawer, prompt
  // drawer, and everything else scrim-backed across the app.
  useCloseOnEscape(onClose, cell !== null && drawerWindow !== null);

  if (!cell || !drawerWindow) return null;
  const heading = cell.date.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
  const { text: hours, tone } = hoursLabel(cell);
  const canPropose = cell.bookings.length === 0 && cell.rota.length === 0;
  const drawerHasTimeline =
    cell.rota.length > 0 || cell.bookings.length > 0 || cell.tasks.length > 0;
  return (
    <>
      <div className="day-drawer__scrim" onClick={onClose} aria-hidden />
      <aside className="day-drawer" role="dialog" aria-label={"Schedule for " + heading}>
        <header className="day-drawer__head">
          <div>
            <div className="day-drawer__eyebrow">Schedule</div>
            <h2 className="day-drawer__title">{heading}</h2>
          </div>
          <button type="button" className="day-drawer__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="day-drawer__body">
          {drawerHasTimeline && (
            <section className="day-drawer__section day-drawer__section--hero">
              <DayTimeline cell={cell} data={data} window={drawerWindow} />
            </section>
          )}
          <section className="day-drawer__section">
            <h3 className="day-drawer__section-title">Availability</h3>
            <p className={"day-drawer__hours day-drawer__hours--" + tone}>{hours}</p>
            {cell.pattern?.starts_local && (
              <p className="day-drawer__muted">
                Weekly pattern: {cell.pattern.starts_local}–{cell.pattern.ends_local}
              </p>
            )}
            <div className="btn-group btn-group--split">
              <button
                type="button"
                className="btn btn--ghost btn--block"
                onClick={() => onRequestOverride(cell.iso)}
              >
                Adjust this day
              </button>
              <button
                type="button"
                className="btn btn--ghost btn--block"
                onClick={() => onRequestLeave(cell.iso)}
              >
                Request leave
              </button>
            </div>
          </section>

          {(cell.leaves.length > 0 || cell.overrides.length > 0) && (
            <section className="day-drawer__section">
              <h3 className="day-drawer__section-title">Pending requests</h3>
              <ul className="day-drawer__list">
                {cell.leaves.map((lv) => (
                  <li key={lv.id} className="day-drawer__row">
                    <strong>{lv.category}</strong>{" "}
                    <span className="day-drawer__muted">
                      {lv.starts_on}{lv.starts_on !== lv.ends_on ? ` → ${lv.ends_on}` : ""}
                    </span>
                    <span className={"chip chip--sm chip--" + (lv.approved_at ? "moss" : "sand")}>
                      {lv.approved_at ? "approved" : "pending"}
                    </span>
                    {lv.note && <div className="day-drawer__muted">{lv.note}</div>}
                  </li>
                ))}
                {cell.overrides.map((ao) => (
                  <li key={ao.id} className="day-drawer__row">
                    <strong>
                      {ao.available
                        ? ao.starts_local && ao.ends_local
                          ? `${ao.starts_local}–${ao.ends_local}`
                          : "Available"
                        : "Off"}
                    </strong>
                    <span className={"chip chip--sm chip--" + (ao.approved_at ? "moss" : "sand")}>
                      {ao.approved_at ? "approved" : "pending"}
                    </span>
                    {ao.reason && <div className="day-drawer__muted">{ao.reason}</div>}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="day-drawer__section">
            <h3 className="day-drawer__section-title">
              Rota · {cell.rota.length} slot{cell.rota.length === 1 ? "" : "s"}
            </h3>
            {cell.rota.length === 0 ? (
              <p className="day-drawer__muted">No rota on this day.</p>
            ) : (
              <ul className="day-drawer__list">
                {cell.rota.map((r) => (
                  <li key={r.slot.id} className="day-drawer__row">
                    <span
                      className="day-drawer__swatch"
                      style={{ "--rota-tint": propertyColor(r.property_id, data) } as React.CSSProperties}
                      aria-hidden
                    />
                    <strong>{r.slot.starts_local}–{r.slot.ends_local}</strong>
                    <span className="day-drawer__muted">{propertyName(r.property_id, data)}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="day-drawer__section">
            <h3 className="day-drawer__section-title">
              Bookings · {cell.bookings.length}
            </h3>
            {cell.bookings.length === 0 ? (
              <>
                {cell.rota.length > 0 ? (
                  // Rota exists but the nightly materialiser (§09) hasn't
                  // produced the booking yet. "No booking on this day"
                  // would read as a contradiction next to the rota row.
                  <p className="day-drawer__muted">
                    Rota scheduled — booking will be created automatically.
                  </p>
                ) : (
                  <p className="day-drawer__muted">No booking on this day.</p>
                )}
                {canPropose && (
                  <div className="day-drawer__actions">
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => onProposeBooking(cell.iso)}
                    >
                      Propose ad-hoc booking
                    </button>
                  </div>
                )}
              </>
            ) : (
              <ul className="booking-list">
                {cell.bookings.map((b) => {
                  const isFutureScheduled =
                    b.status === "scheduled"
                    && new Date(b.scheduled_end).getTime() > Date.now();
                  const canAmend =
                    (b.status === "scheduled"
                      || b.status === "completed"
                      || b.status === "adjusted")
                    && b.pending_amend_minutes == null;
                  return (
                    <li key={b.id} className={`booking-card booking-card--${b.status}`}>
                      <div className="booking-card__head">
                        <strong>{fmtHM(b.scheduled_start)}–{fmtHM(b.scheduled_end)}</strong>
                        <span className="booking-card__time">
                          {propertyName(b.property_id, data)}
                        </span>
                      </div>
                      <div className="booking-card__meta">
                        <span className="booking-card__pill">
                          {BOOKING_STATUS_LABEL[b.status]}
                        </span>
                        <span className="booking-card__dur">
                          {fmtDuration(bookingMinutes(b))}
                        </span>
                      </div>
                      {b.notes_md && <p className="booking-card__note">{b.notes_md}</p>}
                      {b.adjusted && b.adjustment_reason && (
                        <p className="booking-card__note">
                          <em>Edited:</em> {b.adjustment_reason}
                        </p>
                      )}
                      {b.pending_amend_minutes != null && (
                        <p className="booking-card__pending">
                          Pending manager approval:
                          {" "}{fmtDuration(b.pending_amend_minutes)}
                          {b.pending_amend_reason ? ` — ${b.pending_amend_reason}` : ""}
                        </p>
                      )}
                      {(canAmend || isFutureScheduled) && (
                        <div className="booking-card__actions">
                          {canAmend && (
                            <button
                              type="button"
                              className="btn btn--moss btn--sm"
                              disabled={amendMutation.isPending}
                              onClick={() =>
                                amendMutation.mutate({
                                  id: b.id,
                                  body: {
                                    actual_minutes: bookingMinutes(b) + 15,
                                    reason: "Stayed 15 min extra to finish",
                                  },
                                })
                              }
                            >
                              Amend (+15 min)
                            </button>
                          )}
                          {isFutureScheduled && (
                            <button
                              type="button"
                              className="btn btn--rust btn--sm"
                              disabled={declineMutation.isPending}
                              onClick={() =>
                                declineMutation.mutate({
                                  id: b.id,
                                  reason: "Sick today",
                                })
                              }
                            >
                              Decline
                            </button>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="day-drawer__section">
            <h3 className="day-drawer__section-title">
              Tasks · {cell.tasks.length}
            </h3>
            {cell.tasks.length === 0 ? (
              <p className="day-drawer__muted">Nothing scheduled.</p>
            ) : (
              <ul className="day-drawer__tasks">
                {cell.tasks.map((t) => (
                  <li key={t.id}>
                    <Link
                      to={"/task/" + t.id}
                      className={"day-drawer__task day-drawer__task--" + t.status}
                      style={{ "--rota-tint": propertyColor(t.property_id, data) } as React.CSSProperties}
                    >
                      <span className="day-drawer__task-time">
                        {timeOfTask(t.scheduled_start)}
                      </span>
                      <span className="day-drawer__task-title">{t.title}</span>
                      <span className="day-drawer__task-prop">
                        {propertyName(t.property_id, data)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </aside>
    </>
  );
}

// ── Page ──────────────────────────────────────────────────────────────

export default function SchedulePage() {
  const { role } = useRole();
  const isPhone = useIsPhone();
  // Manager always renders inside `.desk__main` (own scroll
  // container); the phone agenda assumes the document scrolls. So we
  // only run the phone code path for non-manager workers viewing on
  // a narrow viewport.
  const phoneMode = isPhone && role !== "manager";
  const today = useMemo(() => new Date(), []);
  const todayIso = useMemo(() => isoDate(today), [today]);
  const [selectedIso, setSelectedIso] = useState<string | null>(null);
  const [leaveIso, setLeaveIso] = useState<string | null>(null);
  const [overrideIso, setOverrideIso] = useState<string | null>(null);
  const [proposeIso, setProposeIso] = useState<string | null>(null);

  // Fetched for invalidation scope on dialog submits — /me's leave
  // panel reads `/api/v1/employees/{empId}/leaves`, which is keyed
  // off the v0-era `employee_id`.
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const empId = meQ.data?.employee.id ?? null;

  const title = "Schedule";
  const sub = role === "manager"
    ? "Your rota, hours, and time off — request changes inline."
    : "Your week at a glance. Tap a day to see tasks or request time off.";

  const body: ReactNode = (
    <InfiniteScheduleBody
      variant={phoneMode ? "phone" : "desktop"}
      today={today}
      todayIso={todayIso}
      empId={empId}
      selectedIso={selectedIso}
      setSelectedIso={setSelectedIso}
      leaveIso={leaveIso}
      setLeaveIso={setLeaveIso}
      overrideIso={overrideIso}
      setOverrideIso={setOverrideIso}
      proposeIso={proposeIso}
      setProposeIso={setProposeIso}
    />
  );

  if (role === "manager") {
    return (
      <DeskPage title={title} sub={sub}>
        {body}
      </DeskPage>
    );
  }
  return (
    <>
      <PageHeader title={title} sub={sub} />
      <div className="page-stack">{body}</div>
    </>
  );
}

interface BodyProps {
  today: Date;
  empId: string | null;
  selectedIso: string | null;
  setSelectedIso: (iso: string | null) => void;
  leaveIso: string | null;
  setLeaveIso: (iso: string | null) => void;
  overrideIso: string | null;
  setOverrideIso: (iso: string | null) => void;
  proposeIso: string | null;
  setProposeIso: (iso: string | null) => void;
}

// ── Infinite body — bidirectional agenda shared by phone + desktop ────
//
// Loads 7-day pages on demand. On first paint the worker lands on
// today (centred under the sticky monthbar). IntersectionObserver
// sentinels at the top and bottom of the list trigger
// `fetchPreviousPage` / `fetchNextPage`. Scroll position is
// preserved when prepending so the world doesn't jump under the
// thumb.
//
// Phone stacks one day card per row; desktop stacks one 7-column
// Mon..Sun week grid per row (see `variant`). Both paths share the
// query, the sentinels, the monthbar, the anchor-to-today settle
// phase, and the Today FAB — the only thing that differs is how a
// week's cells lay out inside the week group.
//
// Scroll root is detected once on mount by walking up from the
// agenda container and picking the nearest scrollable ancestor.
// That lands on `.desk__main` for manager /schedule (own-scroll
// pane), on `.phone__body` for worker /schedule at desktop width
// (`.phone__body` has `overflow-y: auto` above 720px), and on
// `window` / the document for phone (`.phone__body` is
// `display: contents` there, deferring scroll to `<html>`). Every
// IntersectionObserver, scroll-preservation delta, and scroll-by
// is then scoped to that root — a single code path that works
// for all three surfaces without the body caring whether its
// viewport is the document or a container.
//
// Why this matters: `/schedule` is the single view a worker hits
// to know where they are working today and tomorrow — it has to
// feel fast, land in the right place, and keep working when the
// worker idly thumbs back to last Tuesday. A weekNav "Prev / Next"
// button that stalls for a network round-trip is strictly worse
// than scrolling, and a manual page paginator means a busy worker
// can miss tomorrow's booking sitting one tap away.

type ScheduleVariant = "phone" | "desktop";

// Walk up from `start` until we hit an element with `overflow-y` of
// `auto`, `scroll`, or `overlay`. Returns that element, or `null`
// meaning "the document itself scrolls, use `window`". Caught at
// mount once; the ancestor chain doesn't change within a page.
function findScrollRoot(start: HTMLElement): HTMLElement | null {
  let node: HTMLElement | null = start.parentElement;
  while (node) {
    const oy = getComputedStyle(node).overflowY;
    if (oy === "auto" || oy === "scroll" || oy === "overlay") return node;
    node = node.parentElement;
  }
  return null;
}

function InfiniteScheduleBody({
  variant,
  today,
  todayIso,
  empId,
  selectedIso,
  setSelectedIso,
  leaveIso,
  setLeaveIso,
  overrideIso,
  setOverrideIso,
  proposeIso,
  setProposeIso,
}: BodyProps & { todayIso: string; variant: ScheduleVariant }) {
  const initialMondayIso = useMemo(
    () => isoDate(startOfIsoWeek(today)),
    [today],
  );

  const q = useInfiniteQuery({
    // Single key for the whole infinite stream so React Query keeps
    // accumulated pages across re-renders. Mutations elsewhere
    // invalidate `["my-schedule", ...]` by prefix and pick this one
    // up too.
    queryKey: ["my-schedule", "infinite", initialMondayIso] as const,
    initialPageParam: initialMondayIso,
    queryFn: ({ pageParam }) => {
      const fromIso = pageParam;
      const toIso = isoDate(addDays(parseIsoDate(pageParam), 6));
      return fetchJson<MySchedulePayload>(
        `/api/v1/me/schedule?from_=${fromIso}&to=${toIso}`,
      );
    },
    getNextPageParam: (_last, _all, lastParam) =>
      isoDate(addDays(parseIsoDate(lastParam), 7)),
    getPreviousPageParam: (_first, _all, firstParam) =>
      isoDate(addDays(parseIsoDate(firstParam), -7)),
  });

  const merged = useMemo(
    () => (q.data ? mergeSchedulePages(q.data.pages) : null),
    [q.data],
  );

  const firstParam = (q.data?.pageParams[0] as string | undefined) ?? initialMondayIso;
  const totalDays = (q.data?.pageParams.length ?? 1) * 7;

  const cells = useMemo(() => {
    if (!merged) return [];
    return buildCells(parseIsoDate(firstParam), totalDays, merged);
  }, [merged, firstParam, totalDays]);

  const selectedCell = useMemo(
    () => (selectedIso ? cells.find((c) => c.iso === selectedIso) ?? null : null),
    [selectedIso, cells],
  );

  // ── Scroll plumbing ────────────────────────────────────────────────

  const containerRef = useRef<HTMLDivElement | null>(null);
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null);

  // `null` ⇒ the document is the scroll container (phone only, where
  // `.phone__body { display: contents }` defers overflow to `<html>`).
  // An HTMLElement here means an ancestor with its own overflow owns
  // scroll: `.phone__body` at desktop width for worker /schedule, or
  // `.desk__main` for manager /schedule. Every observer, height read,
  // and scroll-by has to target that root rather than `window`.
  //
  // Captured via a callback ref so the detection fires the moment the
  // container mounts — not in a `useLayoutEffect([])`, which runs once
  // after the *first* render. The first render currently commits
  // `<Loading />` (see wrapper below), so a mount-only effect would
  // fire with `containerRef.current === null` and never re-run once
  // the real container appears on the next render. Detection via
  // callback ref fires after the ref is actually assigned.
  const [scrollRoot, setScrollRoot] = useState<HTMLElement | null>(null);
  const setContainerEl = useCallback((node: HTMLDivElement | null) => {
    containerRef.current = node;
    if (node) setScrollRoot(findScrollRoot(node));
  }, []);

  // `null` root = use `window` / document. Any non-null root is an
  // element whose own overflow owns scroll.
  const getScrollHeight = useCallback(
    () => scrollRoot?.scrollHeight ?? document.documentElement.scrollHeight,
    [scrollRoot],
  );
  const scrollByDelta = useCallback((delta: number) => {
    const target: Element | Window = scrollRoot ?? window;
    target.scrollBy({ top: delta, behavior: "instant" as ScrollBehavior });
  }, [scrollRoot]);

  // Preserve scroll position when prepending. Captured BEFORE
  // `fetchPreviousPage` runs and consumed once the new first page
  // appears in `q.data.pages`.
  const heightBeforePrependRef = useRef<number | null>(null);
  const prevFirstParamRef = useRef<string | null>(null);

  // The initial paint loads today's week, but the bottom (and top)
  // sentinels then fire concurrently and pull in 1-3 adjacent weeks.
  // Each prepend shifts the document, and a single
  // `scrollIntoView({block:"start"})` only positions today *once* —
  // by the time the prefetches settle today has drifted ~half a
  // screen down. So we keep re-anchoring today to the top until
  // either (a) all the auto-prefetches have settled or (b) the
  // worker has scrolled today out of view themselves.
  const settledRef = useRef(false);

  // Bottom sentinel — extend the future when the worker thumbs down.
  // `root` is the scrollRoot element or `null` (document).
  useEffect(() => {
    const node = bottomSentinelRef.current;
    if (!node) return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (
            e.isIntersecting
            && q.hasNextPage
            && !q.isFetchingNextPage
            && !q.isFetching
          ) {
            q.fetchNextPage();
          }
        }
      },
      { root: scrollRoot, rootMargin: "600px 0px 600px 0px" },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [
    scrollRoot,
    q.hasNextPage,
    q.isFetchingNextPage,
    q.isFetching,
    q.fetchNextPage,
  ]);

  // Top sentinel — extend the past, capturing scroll height so we
  // can compensate after the prepend.
  useEffect(() => {
    const node = topSentinelRef.current;
    if (!node) return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (
            e.isIntersecting
            && q.hasPreviousPage
            && !q.isFetchingPreviousPage
            && !q.isFetching
          ) {
            heightBeforePrependRef.current = getScrollHeight();
            q.fetchPreviousPage();
          }
        }
      },
      { root: scrollRoot, rootMargin: "600px 0px 600px 0px" },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [
    scrollRoot,
    getScrollHeight,
    q.hasPreviousPage,
    q.isFetchingPreviousPage,
    q.isFetching,
    q.fetchPreviousPage,
  ]);

  // After a prepend lands and we are *past* the initial settle, keep
  // the worker's visual position by compensating for the scroll
  // root's growth. During settle the re-anchor below takes priority
  // instead — running both isn't harmful but the re-anchor is what
  // actually pins today, so we skip the scrollBy work then.
  useLayoutEffect(() => {
    if (!q.data) return;
    const first = q.data.pageParams[0] as string;
    if (
      settledRef.current
      && prevFirstParamRef.current !== null
      && prevFirstParamRef.current !== first
      && heightBeforePrependRef.current !== null
    ) {
      const delta = getScrollHeight() - heightBeforePrependRef.current;
      if (delta > 0) scrollByDelta(delta);
    }
    if (
      prevFirstParamRef.current !== null
      && prevFirstParamRef.current !== first
    ) {
      heightBeforePrependRef.current = null;
    }
    prevFirstParamRef.current = first;
  }, [q.data, getScrollHeight, scrollByDelta]);

  // Re-anchor today on every cells change while we are still in the
  // initial settle window. Bails out as soon as the worker scrolls
  // today materially out of view — they are now driving.
  useLayoutEffect(() => {
    if (settledRef.current) return;
    if (cells.length === 0) return;
    const node = (containerRef.current ?? document).querySelector(
      `[data-schedule-iso="${todayIso}"]`,
    ) as HTMLElement | null;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const drift = rect.top;
    // If today has drifted off-screen by more than ~one viewport in
    // either direction, the worker is actively reading another week.
    // Stop fighting them. Use the scroll root's client height on
    // manager / worker desktop (where `.desk__main` / `.phone__body`
    // is smaller than the window) so the threshold tracks the pane
    // the user actually sees, not the outer window.
    const paneHeight = scrollRoot?.clientHeight ?? window.innerHeight;
    if (drift > paneHeight * 1.5 || rect.bottom < -paneHeight * 0.5) {
      settledRef.current = true;
      return;
    }
    node.scrollIntoView({ block: "start", behavior: "instant" as ScrollBehavior });
  }, [cells, todayIso, scrollRoot]);

  // End the settle window 200ms after all initial fetches have
  // calmed down. Past that point auto-anchoring stops and the
  // prepend scroll-preserver above takes over.
  useEffect(() => {
    if (settledRef.current) return;
    if (cells.length === 0) return;
    const stillFetching =
      q.isFetching || q.isFetchingPreviousPage || q.isFetchingNextPage;
    if (stillFetching) return;
    const t = window.setTimeout(() => {
      settledRef.current = true;
    }, 200);
    return () => window.clearTimeout(t);
  }, [
    cells.length,
    q.isFetching,
    q.isFetchingPreviousPage,
    q.isFetchingNextPage,
  ]);

  // ── Sticky month label + Today FAB ─────────────────────────────────

  const [topVisibleIso, setTopVisibleIso] = useState<string>(todayIso);
  const [todayInView, setTodayInView] = useState<boolean>(true);

  // One observer per cell row — the topmost intersecting cell drives
  // the monthbar label, and the today cell drives the FAB visibility.
  useEffect(() => {
    if (cells.length === 0) return;
    const root = containerRef.current;
    if (!root) return;
    const nodes = Array.from(
      root.querySelectorAll<HTMLElement>("[data-schedule-iso]"),
    );
    if (nodes.length === 0) return;

    const intersecting = new Set<string>();
    const obs = new IntersectionObserver(
      (entries) => {
        let nextTodayInView: boolean | null = null;
        for (const e of entries) {
          const iso = (e.target as HTMLElement).dataset.scheduleIso;
          if (!iso) continue;
          if (e.isIntersecting) intersecting.add(iso);
          else intersecting.delete(iso);
          if (iso === todayIso) nextTodayInView = e.isIntersecting;
        }
        if (intersecting.size > 0) {
          let earliest: string | null = null;
          for (const iso of intersecting) {
            if (earliest === null || iso < earliest) earliest = iso;
          }
          if (earliest) setTopVisibleIso(earliest);
        }
        if (nextTodayInView !== null) setTodayInView(nextTodayInView);
      },
      // Crop to the area between the sticky monthbar and the bottom
      // of the viewport. ≈64px is the monthbar height; adjust here
      // if the bar grows. `root` is the scrollRoot (or null = document),
      // which matters for manager /schedule where the viewport is
      // `.desk__main` rather than the window.
      { root: scrollRoot, rootMargin: "-64px 0px -40% 0px", threshold: [0, 1] },
    );
    nodes.forEach((n) => obs.observe(n));
    return () => obs.disconnect();
  }, [scrollRoot, cells, todayIso]);

  const monthLabel = useMemo(() => {
    const d = parseIsoDate(topVisibleIso);
    return d.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  }, [topVisibleIso]);

  const scrollToToday = useCallback(() => {
    const node = (containerRef.current ?? document).querySelector(
      `[data-schedule-iso="${todayIso}"]`,
    ) as HTMLElement | null;
    if (!node) return;
    // The worker explicitly tapped Today — they want a deliberate,
    // smoothly-animated jump back. Mark settled so the auto-anchor
    // doesn't snap them somewhere else mid-scroll.
    settledRef.current = true;
    node.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [todayIso]);

  // ── Render ─────────────────────────────────────────────────────────
  //
  // We always mount the `.schedule` wrapper so the callback ref above
  // fires on first paint and captures the scroll root — even while
  // the initial query is in flight. Loading / failure states render
  // inside the wrapper instead of early-returning in place of it.

  if (q.isPending) {
    return (
      <div ref={setContainerEl} className={`schedule schedule--${variant}`}>
        <Loading />
      </div>
    );
  }
  if (!merged) {
    return (
      <div ref={setContainerEl} className={`schedule schedule--${variant}`}>
        <p className="muted">Failed to load schedule.</p>
      </div>
    );
  }
  const data = merged;

  const { allPending, firstPendingIso, bannerParts } = computePendingState(
    data.bookings,
  );

  // Group cells by ISO week so we can drop a small separator between
  // weeks ("20 Apr – 26 Apr"). Workers reading across a 3-week span
  // otherwise lose the week boundary; the separator keeps them
  // oriented without inflating row height.
  const groups: { weekStartIso: string; weekLabel: string; cells: DayCell[] }[] = [];
  for (const cell of cells) {
    const ws = isoDate(startOfIsoWeek(cell.date));
    const last = groups[groups.length - 1];
    if (!last || last.weekStartIso !== ws) {
      const wsDate = parseIsoDate(ws);
      const weDate = addDays(wsDate, 6);
      const label =
        wsDate.toLocaleDateString("en-GB", { day: "numeric", month: "short" })
        + " – "
        + weDate.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
      groups.push({ weekStartIso: ws, weekLabel: label, cells: [cell] });
    } else {
      last.cells.push(cell);
    }
  }

  return (
    <>
      <div ref={setContainerEl} className={`schedule schedule--${variant}`}>
        <div className="schedule__sticky-top">
          {bannerParts.length > 0 && (
            <ScheduleBanner
              allPending={allPending}
              bannerParts={bannerParts}
              firstPendingIso={firstPendingIso}
              onReview={setSelectedIso}
            />
          )}
          <div
            className="schedule__monthbar"
            aria-live="polite"
            aria-atomic="true"
          >
            <span className="schedule__monthbar-label">{monthLabel}</span>
            {!todayInView && (
              <button
                type="button"
                className="schedule__monthbar-jump"
                onClick={scrollToToday}
              >
                Today
              </button>
            )}
          </div>
        </div>

        {variant === "desktop" && (
          // Desktop-only: the colour legend + help line that used to
          // sit inside the old grid-panel footer. Rendered above the
          // agenda so it's seen on first paint and then scrolls away
          // as the worker thumbs through weeks; the monthbar above
          // stays pinned. Phone drops it — cards are labelled in-line.
          <div className="schedule__intro">
            <div className="schedule__legend">
              {data.properties.map((p) => (
                <span
                  key={p.id}
                  className="schedule__legend-item"
                  style={{ "--rota-tint": propertyColor(p.id, data) } as React.CSSProperties}
                >
                  <span className="schedule__legend-swatch" aria-hidden />
                  {p.name}
                </span>
              ))}
            </div>
            <p className="muted schedule__intro-help">
              Click any day to see tasks, adjust hours, or request leave.
              Reducing availability needs manager approval (§06).
            </p>
          </div>
        )}

        <div className="schedule__agenda" role="list">
          <div
            ref={topSentinelRef}
            className="schedule__sentinel schedule__sentinel--top"
            aria-hidden
          >
            {q.isFetchingPreviousPage ? (
              <span className="schedule__sentinel-spinner">Loading earlier…</span>
            ) : (
              <span className="schedule__sentinel-hint">Scroll up for past weeks</span>
            )}
          </div>

          {groups.map((group, gi) => (
            <Fragment key={group.weekStartIso}>
              {gi > 0 && (
                <div className="schedule__weekgap" aria-hidden>
                  <span>{group.weekLabel}</span>
                </div>
              )}
              {variant === "desktop" ? (
                <ScheduleWeekGrid
                  cells={group.cells}
                  data={data}
                  today={today}
                  onOpen={setSelectedIso}
                  label={group.weekLabel}
                  hideLabel={gi > 0}
                />
              ) : (
                <SchedulePhoneWeek
                  group={group}
                  data={data}
                  today={today}
                  onOpen={setSelectedIso}
                />
              )}
            </Fragment>
          ))}

          <div
            ref={bottomSentinelRef}
            className="schedule__sentinel schedule__sentinel--bot"
            aria-hidden
          >
            {q.isFetchingNextPage ? (
              <span className="schedule__sentinel-spinner">Loading next week…</span>
            ) : (
              <span className="schedule__sentinel-hint">Keep scrolling for more</span>
            )}
          </div>
        </div>

        {!todayInView && (
          <button
            type="button"
            className="schedule__today-fab"
            onClick={scrollToToday}
            aria-label="Jump to today"
          >
            Today
          </button>
        )}
      </div>

      <ScheduleDialogsFooter
        data={data}
        empId={empId}
        selectedCell={selectedCell}
        setSelectedIso={setSelectedIso}
        leaveIso={leaveIso}
        setLeaveIso={setLeaveIso}
        overrideIso={overrideIso}
        setOverrideIso={setOverrideIso}
        proposeIso={proposeIso}
        setProposeIso={setProposeIso}
      />
    </>
  );
}

// ── Shared helpers ────────────────────────────────────────────────────

function computePendingState(bookings: Booking[]): {
  allPending: Booking[];
  firstPendingIso: string | null;
  bannerParts: string[];
} {
  // §14 "Pending banner" — count of bookings in the visible window
  // that need manager attention. Two buckets: proposal
  // (pending_approval) and self-amend (pending_amend_minutes). The
  // first day with any of either is the scroll target.
  const pendingProposal = bookings.filter((b) => b.status === "pending_approval");
  const pendingAmend = bookings.filter((b) => b.pending_amend_minutes != null);
  const allPending = [...pendingProposal, ...pendingAmend];
  const firstPendingIso =
    allPending.map((b) => b.scheduled_start.slice(0, 10)).sort()[0] ?? null;
  const bannerParts: string[] = [];
  if (pendingProposal.length > 0) {
    bannerParts.push(`${pendingProposal.length} awaiting manager approval`);
  }
  if (pendingAmend.length > 0) {
    bannerParts.push(
      `${pendingAmend.length} amendment${pendingAmend.length === 1 ? "" : "s"} pending`,
    );
  }
  return { allPending, firstPendingIso, bannerParts };
}

function ScheduleBanner({
  allPending,
  bannerParts,
  firstPendingIso,
  onReview,
}: {
  allPending: Booking[];
  bannerParts: string[];
  firstPendingIso: string | null;
  onReview: (iso: string) => void;
}) {
  return (
    <div className="schedule-banner schedule-banner--pending" role="status">
      <span className="schedule-banner__text">
        <strong>
          {allPending.length} booking{allPending.length === 1 ? "" : "s"}{" "}
          need{allPending.length === 1 ? "s" : ""} attention
        </strong>
        <span className="schedule-banner__detail"> · {bannerParts.join(" · ")}</span>
      </span>
      {firstPendingIso && (
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => onReview(firstPendingIso)}
        >
          Review
        </button>
      )}
    </div>
  );
}

function ScheduleDialogsFooter({
  data,
  empId,
  selectedCell,
  setSelectedIso,
  leaveIso,
  setLeaveIso,
  overrideIso,
  setOverrideIso,
  proposeIso,
  setProposeIso,
}: {
  data: MySchedulePayload;
  empId: string | null;
  selectedCell: DayCell | null;
  setSelectedIso: (iso: string | null) => void;
  leaveIso: string | null;
  setLeaveIso: (iso: string | null) => void;
  overrideIso: string | null;
  setOverrideIso: (iso: string | null) => void;
  proposeIso: string | null;
  setProposeIso: (iso: string | null) => void;
}) {
  return (
    <>
      <DayDrawer
        cell={selectedCell}
        data={data}
        onClose={() => setSelectedIso(null)}
        onRequestLeave={(iso) => { setSelectedIso(null); setLeaveIso(iso); }}
        onRequestOverride={(iso) => { setSelectedIso(null); setOverrideIso(iso); }}
        onProposeBooking={(iso) => { setSelectedIso(null); setProposeIso(iso); }}
      />
      <OverrideDialog
        iso={overrideIso}
        employeeId={empId}
        pattern={
          overrideIso
            ? (data.weekly_availability.find(
                (w) => w.weekday === isoWeekday(parseIsoDate(overrideIso)),
              ) ?? null)
            : null
        }
        onClose={() => setOverrideIso(null)}
      />
      <LeaveDialog
        iso={leaveIso}
        employeeId={empId}
        onClose={() => setLeaveIso(null)}
      />
      <BookingProposeDialog
        iso={proposeIso}
        properties={data.properties}
        onClose={() => setProposeIso(null)}
      />
    </>
  );
}

function SchedulePhoneWeek({
  group,
  data,
  today,
  onOpen,
}: {
  group: { weekStartIso: string; weekLabel: string; cells: DayCell[] };
  data: MySchedulePayload;
  today: Date;
  onOpen: (iso: string) => void;
}) {
  // Phone uses the same per-week window so vertically scrolling from
  // Monday to Sunday feels like one continuous planner page. Empty
  // days collapse to a short "rest" card — they don't claim the
  // shared row height because the cards are stacked, not gridded.
  const win = useMemo(() => computeWindow(group.cells), [group.cells]);
  return (
    <>
      {group.cells.map((cell) => (
        <div key={cell.iso} role="listitem">
          <DayCellView
            cell={cell}
            data={data}
            onOpen={onOpen}
            today={today}
            window={win}
            collapseEmpty={true}
          />
        </div>
      ))}
    </>
  );
}

function ScheduleWeekGrid({
  cells,
  data,
  today,
  onOpen,
  label,
  hideLabel = false,
}: {
  cells: DayCell[];
  data: MySchedulePayload;
  today: Date;
  onOpen: (iso: string) => void;
  label: string;
  /** Hide the inline week label when a `.schedule__weekgap` separator
   *  above the grid already shows the date range. The first week in
   *  the infinite stream has no separator above it, so it still
   *  renders the label for orientation on first paint. */
  hideLabel?: boolean;
}) {
  // One window per week so every cell in the row shares top/bottom
  // hours and identical height (see §14 "Shared time window per ISO
  // week"). Next week recomputes — a quiet week doesn't inherit an
  // on-call week's stretched scale.
  const win = useMemo(() => computeWindow(cells), [cells]);
  return (
    <div className="schedule-week" role="grid" aria-label={label}>
      {!hideLabel && <div className="schedule-week__label">{label}</div>}
      <div className="schedule-week__header-row">
        {cells.map((c) => {
          const { day } = dayLabel(c.date);
          return (
            <div key={c.iso} className="schedule-week__header">
              <strong>{WEEKDAYS[isoWeekday(c.date)]!.short}</strong>
              <span>{day}</span>
            </div>
          );
        })}
      </div>
      <div className="schedule-week__row">
        {cells.map((c) => (
          <DayCellView
            key={c.iso}
            cell={c}
            data={data}
            onOpen={onOpen}
            today={today}
            window={win}
            collapseEmpty={false}
          />
        ))}
      </div>
    </div>
  );
}

// §09 "Ad-hoc bookings" — worker proposes an unscheduled booking
// (swung by for laundry, covered a gap). Always lands with
// `status = pending_approval`; the manager sees it in the queue and
// approves or rejects. The mock implements the minimum viable form;
// the production shell will expand it to match the full §09 body.
function BookingProposeDialog({
  iso,
  properties,
  onClose,
}: {
  iso: string | null;
  properties: { id: string; name: string; timezone: string }[];
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const qc = useQueryClient();
  const [propertyId, setPropertyId] = useState<string>("");
  const [starts, setStarts] = useState<string>("09:00");
  const [ends, setEnds] = useState<string>("12:00");
  const [notes, setNotes] = useState<string>("");

  useEffect(() => {
    if (iso === null) return;
    setPropertyId(properties[0]?.id ?? "");
    setStarts("09:00");
    setEnds("12:00");
    setNotes("");
    const d = dialogRef.current;
    if (d && !d.open) d.showModal();
    return () => {
      if (d && d.open) d.close();
    };
  }, [iso, properties]);

  const m = useMutation({
    mutationFn: (body: unknown) =>
      fetchJson<Booking>("/api/v1/bookings", {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["my-schedule"] });
      qc.invalidateQueries({ queryKey: qk.bookings() });
      onClose();
    },
  });

  if (!iso) return null;

  return (
    <dialog className="modal" ref={dialogRef} onClose={onClose}>
      <form
        className="modal__body"
        onSubmit={(e) => {
          e.preventDefault();
          if (!propertyId || !starts || !ends || ends <= starts) return;
          m.mutate({
            property_id: propertyId,
            scheduled_start: `${iso}T${starts}:00`,
            scheduled_end: `${iso}T${ends}:00`,
            notes_md: notes.trim() || null,
          });
        }}
      >
        <h3 className="modal__title">Propose ad-hoc booking</h3>
        <p className="modal__sub">
          {iso} · Sent to your manager for approval.
        </p>

        <label className="field">
          <span>Property</span>
          <select
            value={propertyId}
            onChange={(e) => setPropertyId(e.target.value)}
            required
          >
            {properties.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </label>

        <div className="avail-hours">
          <label className="field">
            <span>From</span>
            <input type="time" value={starts} onChange={(e) => setStarts(e.target.value)} required />
          </label>
          <label className="field">
            <span>Until</span>
            <input type="time" value={ends} onChange={(e) => setEnds(e.target.value)} required />
          </label>
        </div>

        <label className="field">
          <span>Notes (optional)</span>
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Swung by for forgotten laundry…"
          />
        </label>

        <div className="modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--moss" disabled={m.isPending}>
            {m.isPending ? "Submitting…" : "Propose"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
