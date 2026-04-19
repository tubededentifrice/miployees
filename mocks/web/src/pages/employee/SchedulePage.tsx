import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import PageHeader from "@/components/PageHeader";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import { LeaveDialog, OverrideDialog } from "@/components/ScheduleDialogs";
import { useRole } from "@/context/RoleContext";
import type {
  AvailabilityOverride,
  Leave,
  Me,
  MySchedulePayload,
  ScheduleRulesetSlot,
  SchedulerTaskView,
  SelfWeeklyAvailabilitySlot,
} from "@/types/api";

// §14 "Schedule view". Self-only calendar hub that replaces the old
// `/week` flat list and the `/me/schedule` alias. Phone renders an
// agenda (one row per day, 14-day window); desktop renders a week
// grid (Mon..Sun, one row). Click a day anywhere to open the shared
// day drawer with rota, tasks, and the Request-leave / Request-
// override forms. See spec §06 for the approval rules.

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
  return d.toISOString().slice(0, 10);
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
    cells.push({
      date: d,
      iso,
      rota,
      tasks,
      leaves,
      overrides,
      pattern: weeklyByDay.get(wd) ?? null,
    });
  }
  return cells;
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
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Open schedule for ${label.weekday} ${label.day} ${label.month}`}
      className={
        "schedule-day" +
        (isToday ? " schedule-day--today" : "") +
        (cell.tasks.length === 0 && cell.rota.length === 0 ? " schedule-day--quiet" : "")
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
        <div className={"schedule-day__hours schedule-day__hours--" + tone}>{hours}</div>
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
}: {
  cell: DayCell | null;
  data: MySchedulePayload;
  onClose: () => void;
  onRequestLeave: (iso: string) => void;
  onRequestOverride: (iso: string) => void;
}) {
  if (!cell) return null;
  const heading = cell.date.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
  const { text: hours, tone } = hoursLabel(cell);
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
            <div className="day-drawer__actions">
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => onRequestOverride(cell.iso)}
              >
                Adjust this day
              </button>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
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
  const today = useMemo(() => new Date(), []);
  const [weekStart, setWeekStart] = useState<Date>(() => startOfIsoWeek(today));
  const [selectedIso, setSelectedIso] = useState<string | null>(null);
  const [leaveIso, setLeaveIso] = useState<string | null>(null);
  const [overrideIso, setOverrideIso] = useState<string | null>(null);

  const windowDays = 14;
  const windowEnd = addDays(weekStart, windowDays - 1);
  const from = isoDate(weekStart);
  const to = isoDate(windowEnd);

  const q = useQuery({
    queryKey: qk.mySchedule(from, to),
    queryFn: () => fetchJson<MySchedulePayload>(`/api/v1/me/schedule?from_=${from}&to=${to}`),
  });

  // Fetched for invalidation scope on dialog submits — /me's leave
  // panel reads `/api/v1/employees/{empId}/leaves`, which is keyed
  // off the v0-era `employee_id`.
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const empId = meQ.data?.employee.id ?? null;

  const cells = useMemo(
    () => (q.data ? buildCells(weekStart, windowDays, q.data) : []),
    [q.data, weekStart],
  );
  const selectedCell = useMemo(
    () => (selectedIso ? cells.find((c) => c.iso === selectedIso) ?? null : null),
    [selectedIso, cells],
  );

  const title = "Schedule";
  const sub = role === "manager"
    ? "Your rota, hours, and time off — request changes inline."
    : "Your week at a glance. Tap a day to see tasks or request time off.";

  const weekNav = (
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
  );

  const body: ReactNode = (() => {
    if (q.isPending) return <Loading />;
    if (!q.data) return <p className="muted">Failed to load schedule.</p>;

    const nextWeek = cells.slice(7);
    return (
      <>
        {weekNav}
        <div className="schedule">
          <div className="schedule__agenda" role="list">
            {cells.map((cell) => (
              <div key={cell.iso} role="listitem">
                <DayCellView cell={cell} data={q.data!} onOpen={setSelectedIso} today={today} />
              </div>
            ))}
          </div>

          <div className="schedule__grid-panel panel">
            <ScheduleWeekGrid
              cells={cells.slice(0, 7)}
              data={q.data}
              today={today}
              onOpen={setSelectedIso}
              label="This week"
            />
            {nextWeek.length > 0 && (
              <ScheduleWeekGrid
                cells={nextWeek}
                data={q.data}
                today={today}
                onOpen={setSelectedIso}
                label="Next week"
              />
            )}
            <div className="schedule__legend">
              {q.data.properties.map((p) => (
                <span
                  key={p.id}
                  className="schedule__legend-item"
                  style={{ "--rota-tint": propertyColor(p.id, q.data!) } as React.CSSProperties}
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

        <DayDrawer
          cell={selectedCell}
          data={q.data}
          onClose={() => setSelectedIso(null)}
          onRequestLeave={(iso) => { setSelectedIso(null); setLeaveIso(iso); }}
          onRequestOverride={(iso) => { setSelectedIso(null); setOverrideIso(iso); }}
        />

        <OverrideDialog
          iso={overrideIso}
          employeeId={empId}
          pattern={
            overrideIso
              ? (q.data.weekly_availability.find(
                  (w) => w.weekday === isoWeekday(new Date(overrideIso)),
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
      </>
    );
  })();

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
