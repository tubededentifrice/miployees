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
// `/bookings` page. Phone renders a continuous agenda backed by a
// bidirectional infinite query (7-day pages): the worker lands on
// today, scrolls up to past weeks, scrolls down to load the next.
// Desktop renders a Mon..Sun week grid with explicit prev/next
// navigation. Click a day anywhere to open the shared day drawer
// with rota, tasks, bookings (§09, amend/decline inline), plus the
// Request-leave / Request-override forms. A pending banner sits
// above the agenda whenever any booking in the loaded window is
// pending_approval or has a pending self-amend — so a stale
// approval can't fall off-screen. See spec §06 for the approval
// rules and §09 for the booking lifecycle.

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
// used by `.schedule__agenda` / `.schedule__grid-panel` in CSS so the
// JS-side fetching strategy lines up with what is actually visible.
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

function propertyName(pid: string, data: MySchedulePayload): string {
  return data.properties.find((p) => p.id === pid)?.name ?? "—";
}

function hoursLabel(cell: DayCell): { text: string; tone: "moss" | "sand" | "rust" | "ghost" } {
  const approvedLeave = cell.leaves.find((lv) => lv.approved_at !== null);
  if (approvedLeave) return { text: approvedLeave.category.toUpperCase(), tone: "rust" };
  const pendingLeave = cell.leaves.find((lv) => lv.approved_at === null);
  if (pendingLeave) return { text: `${pendingLeave.category.toUpperCase()} · pending`, tone: "sand" };

  const approvedOverride = cell.overrides.find((o) => o.approved_at !== null);
  if (approvedOverride) {
    if (!approvedOverride.available) return { text: "Off (override)", tone: "rust" };
    const s = approvedOverride.starts_local ?? cell.pattern?.starts_local ?? null;
    const e = approvedOverride.ends_local ?? cell.pattern?.ends_local ?? null;
    if (s && e) return { text: `${s}–${e}`, tone: "moss" };
  }
  const pendingOverride = cell.overrides.find((o) => o.approved_at === null);
  if (pendingOverride) {
    if (!pendingOverride.available) return { text: "Off · pending", tone: "sand" };
    const s = pendingOverride.starts_local ?? cell.pattern?.starts_local ?? null;
    const e = pendingOverride.ends_local ?? cell.pattern?.ends_local ?? null;
    if (s && e) return { text: `${s}–${e} · pending`, tone: "sand" };
  }
  if (cell.pattern && cell.pattern.starts_local && cell.pattern.ends_local) {
    return { text: `${cell.pattern.starts_local}–${cell.pattern.ends_local}`, tone: "moss" };
  }
  return { text: "Off", tone: "ghost" };
}

function TaskChip({ task, data }: { task: SchedulerTaskView; data: MySchedulePayload }) {
  return (
    <Link
      to={"/task/" + task.id}
      className={"schedule-task schedule-task--" + task.status}
      data-property={task.property_id}
      style={{ "--rota-tint": propertyColor(task.property_id, data) } as React.CSSProperties}
    >
      <span className="schedule-task__time">{timeOfTask(task.scheduled_start)}</span>
      <span className="schedule-task__title">{task.title}</span>
    </Link>
  );
}

function DayCellView({
  cell,
  data,
  onOpen,
  today,
}: {
  cell: DayCell;
  data: MySchedulePayload;
  onOpen: (iso: string) => void;
  today: Date;
}) {
  const { text: hours, tone } = hoursLabel(cell);
  const isToday = sameDate(cell.date, today);
  const label = dayLabel(cell.date);
  const pendingBookings = cell.bookings.filter(bookingNeedsAttention);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={
        `Open schedule for ${label.weekday} ${label.day} ${label.month}`
        + (pendingBookings.length > 0
          ? ` — ${pendingBookings.length} booking${pendingBookings.length === 1 ? "" : "s"} ${pendingBookings.length === 1 ? "needs" : "need"} attention`
          : "")
      }
      className={
        "schedule-day" +
        (isToday ? " schedule-day--today" : "") +
        (cell.tasks.length === 0 && cell.rota.length === 0 && cell.bookings.length === 0
          ? " schedule-day--quiet"
          : "") +
        (pendingBookings.length > 0 ? " schedule-day--pending" : "")
      }
      onClick={(e) => {
        // Nested <Link>s for individual tasks keep their own
        // navigation; clicking the cell background opens the drawer.
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
      <div className="schedule-day__date">
        <span className="schedule-day__wd">{label.weekday}</span>
        <span className="schedule-day__num">{label.day}</span>
        <span className="schedule-day__mo">{label.month}</span>
      </div>
      <div className="schedule-day__body">
        <div className="schedule-day__hours-row">
          <span className={"schedule-day__hours schedule-day__hours--" + tone}>{hours}</span>
          {pendingBookings.length > 0 && (
            <span
              className="schedule-day__pending-dot"
              aria-label={`${pendingBookings.length} booking${pendingBookings.length === 1 ? "" : "s"} ${pendingBookings.length === 1 ? "needs" : "need"} attention`}
              title={`${pendingBookings.length} booking${pendingBookings.length === 1 ? "" : "s"} ${pendingBookings.length === 1 ? "needs" : "need"} attention`}
            >
              {pendingBookings.length}
            </span>
          )}
        </div>
        {cell.rota.length > 0 && (
          <div className="schedule-day__rota">
            {cell.rota.map((r) => (
              <span
                key={r.slot.id}
                className="schedule-day__slot"
                style={{ "--rota-tint": propertyColor(r.property_id, data) } as React.CSSProperties}
              >
                <span className="schedule-day__slot-time">
                  {r.slot.starts_local}–{r.slot.ends_local}
                </span>
                <span className="schedule-day__slot-prop">{propertyName(r.property_id, data)}</span>
              </span>
            ))}
          </div>
        )}
        {cell.tasks.length > 0 && (
          <div className="schedule-day__tasks">
            {cell.tasks.slice(0, 3).map((t) => (
              <TaskChip key={t.id} task={t} data={data} />
            ))}
            {cell.tasks.length > 3 && (
              <span className="schedule-day__more">+{cell.tasks.length - 3} more</span>
            )}
          </div>
        )}
      </div>
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

  if (!cell) return null;
  const heading = cell.date.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
  const { text: hours, tone } = hoursLabel(cell);
  const canPropose = cell.bookings.length === 0 && cell.rota.length === 0;
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

  const body: ReactNode = phoneMode ? (
    <PhoneAgendaBody
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
  ) : (
    <DesktopWeekBody
      today={today}
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

// ── Desktop body — Mon..Sun grid (current behaviour preserved) ────────

function DesktopWeekBody({
  today,
  empId,
  selectedIso,
  setSelectedIso,
  leaveIso,
  setLeaveIso,
  overrideIso,
  setOverrideIso,
  proposeIso,
  setProposeIso,
}: BodyProps) {
  const [weekStart, setWeekStart] = useState<Date>(() => startOfIsoWeek(today));
  const windowDays = 14;
  const windowEnd = addDays(weekStart, windowDays - 1);
  const from = isoDate(weekStart);
  const to = isoDate(windowEnd);

  const q = useQuery({
    queryKey: qk.mySchedule(from, to),
    queryFn: () => fetchJson<MySchedulePayload>(`/api/v1/me/schedule?from_=${from}&to=${to}`),
  });

  const cells = useMemo(
    () => (q.data ? buildCells(weekStart, windowDays, q.data) : []),
    [q.data, weekStart],
  );
  const selectedCell = useMemo(
    () => (selectedIso ? cells.find((c) => c.iso === selectedIso) ?? null : null),
    [selectedIso, cells],
  );

  if (q.isPending) return <Loading />;
  if (!q.data) return <p className="muted">Failed to load schedule.</p>;
  const data = q.data;

  const nextWeek = cells.slice(7);
  const { allPending, firstPendingIso, bannerParts } = computePendingState(data.bookings);

  return (
    <>
      {bannerParts.length > 0 && (
        <ScheduleBanner
          allPending={allPending}
          bannerParts={bannerParts}
          firstPendingIso={firstPendingIso}
          onReview={setSelectedIso}
        />
      )}
      <div className="scheduler-weeknav">
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => setWeekStart((w) => addDays(w, -7))}
        >
          ← Previous
        </button>
        <span className="scheduler-weeknav__label">
          {weekStart.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}
          {" – "}
          {windowEnd.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}
        </span>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => setWeekStart(startOfIsoWeek(today))}
        >
          This week
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => setWeekStart((w) => addDays(w, 7))}
        >
          Next →
        </button>
      </div>
      <div className="schedule">
        <div className="schedule__grid-panel panel">
          <ScheduleWeekGrid
            cells={cells.slice(0, 7)}
            data={data}
            today={today}
            onOpen={setSelectedIso}
            label="This week"
          />
          {nextWeek.length > 0 && (
            <ScheduleWeekGrid
              cells={nextWeek}
              data={data}
              today={today}
              onOpen={setSelectedIso}
              label="Next week"
            />
          )}
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
          <p className="muted">
            Click any day to see tasks, adjust hours, or request leave.
            Reducing availability needs manager approval (§06).
          </p>
        </div>
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

// ── Phone body — bidirectional infinite agenda ────────────────────────
//
// Loads 7-day pages on demand. On first paint the worker lands on
// today (centred under the sticky monthbar). IntersectionObserver
// sentinels at the top and bottom of the list trigger
// `fetchPreviousPage` / `fetchNextPage`. Scroll position is
// preserved when prepending so the world doesn't jump under the
// thumb.
//
// Why this matters: this is the single view a worker hits to know
// where they are working today and tomorrow — it has to feel fast,
// it has to land in the right place, and it has to keep working
// when the worker idly thumbs back to last Tuesday. A weekNav
// "Prev / Next" button that stalls for a network round-trip is
// strictly worse on a phone, and a manual page paginator means a
// busy worker can miss tomorrow's booking sitting one tap away.

function PhoneAgendaBody({
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
}: BodyProps & { todayIso: string }) {
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

  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null);

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
      { rootMargin: "600px 0px 600px 0px" },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [q.hasNextPage, q.isFetchingNextPage, q.isFetching, q.fetchNextPage]);

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
            heightBeforePrependRef.current =
              document.documentElement.scrollHeight;
            q.fetchPreviousPage();
          }
        }
      },
      { rootMargin: "600px 0px 600px 0px" },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [
    q.hasPreviousPage,
    q.isFetchingPreviousPage,
    q.isFetching,
    q.fetchPreviousPage,
  ]);

  // After a prepend lands and we are *past* the initial settle, keep
  // the worker's visual position by compensating for the document
  // growth. During settle the re-anchor below takes priority instead
  // — running both isn't harmful but the re-anchor is what actually
  // pins today, so we skip the scrollBy work then.
  useLayoutEffect(() => {
    if (!q.data) return;
    const first = q.data.pageParams[0] as string;
    if (
      settledRef.current
      && prevFirstParamRef.current !== null
      && prevFirstParamRef.current !== first
      && heightBeforePrependRef.current !== null
    ) {
      const delta =
        document.documentElement.scrollHeight - heightBeforePrependRef.current;
      if (delta > 0) {
        window.scrollBy({ top: delta, behavior: "instant" as ScrollBehavior });
      }
    }
    if (
      prevFirstParamRef.current !== null
      && prevFirstParamRef.current !== first
    ) {
      heightBeforePrependRef.current = null;
    }
    prevFirstParamRef.current = first;
  }, [q.data]);

  // Re-anchor today on every cells change while we are still in the
  // initial settle window. Bails out as soon as the worker scrolls
  // today materially out of view — they are now driving.
  useLayoutEffect(() => {
    if (settledRef.current) return;
    if (cells.length === 0) return;
    const node = document.querySelector(
      `[data-schedule-iso="${todayIso}"]`,
    ) as HTMLElement | null;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const drift = rect.top;
    // If today has drifted off-screen by more than ~one viewport in
    // either direction, the worker is actively reading another week.
    // Stop fighting them.
    if (drift > window.innerHeight * 1.5 || rect.bottom < -window.innerHeight * 0.5) {
      settledRef.current = true;
      return;
    }
    node.scrollIntoView({ block: "start", behavior: "instant" as ScrollBehavior });
  }, [cells, todayIso]);

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
    const nodes = Array.from(
      document.querySelectorAll<HTMLElement>("[data-schedule-iso]"),
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
      // if the bar grows.
      { rootMargin: "-64px 0px -40% 0px", threshold: [0, 1] },
    );
    nodes.forEach((n) => obs.observe(n));
    return () => obs.disconnect();
  }, [cells, todayIso]);

  const monthLabel = useMemo(() => {
    const d = parseIsoDate(topVisibleIso);
    return d.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  }, [topVisibleIso]);

  const scrollToToday = useCallback(() => {
    const node = document.querySelector(
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

  if (q.isPending) return <Loading />;
  if (!merged) return <p className="muted">Failed to load schedule.</p>;
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
      {bannerParts.length > 0 && (
        <ScheduleBanner
          allPending={allPending}
          bannerParts={bannerParts}
          firstPendingIso={firstPendingIso}
          onReview={setSelectedIso}
        />
      )}

      <div className="schedule schedule--phone">
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
              {group.cells.map((cell) => (
                <div key={cell.iso} role="listitem" data-schedule-iso={cell.iso}>
                  <DayCellView
                    cell={cell}
                    data={data}
                    onOpen={setSelectedIso}
                    today={today}
                  />
                </div>
              ))}
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

function ScheduleWeekGrid({
  cells,
  data,
  today,
  onOpen,
  label,
}: {
  cells: DayCell[];
  data: MySchedulePayload;
  today: Date;
  onOpen: (iso: string) => void;
  label: string;
}) {
  return (
    <div className="schedule-week" role="grid" aria-label={label}>
      <div className="schedule-week__label">{label}</div>
      <div className="schedule-week__header-row">
        {cells.map((c) => {
          const { weekday, day } = dayLabel(c.date);
          return (
            <div key={c.iso} className="schedule-week__header">
              <strong>{WEEKDAYS[isoWeekday(c.date)]!.short}</strong>
              <span>{weekday === "Mon" ? day : day}</span>
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
