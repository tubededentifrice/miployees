// `DayTimeline` for `/schedule` (§14 "Schedule view"). Paints the
// rail (availability bar + sideways text), the hour grid, and the
// rota column (property-tinted blocks hosting `TaskChip`s). Used by
// both `DayCellView` (cell body on phone + desktop) and `DayDrawer`
// (per-day hero timeline).
//
// Bookings are the authoritative render of a rota slot for a date
// (§09 "Nightly materialiser"). A rota slot only renders when no
// booking covers its time range — so the worker sees tomorrow's shift
// even before the nightly job has cut the booking.

import type { MySchedulePayload, ScheduleRulesetSlot, SchedulerTaskView } from "@/types/api";
import { TaskChipLink } from "./TaskChip";
import { availability } from "./lib/availability";
import {
  bookingNeedsAttention,
  fmtHM,
  hhmmToMin,
  isoToMinOfDay,
  posTop,
  type TimeWindow,
} from "./lib/bookingHelpers";
import type { DayCell } from "./lib/buildCells";
import { propertyColor, propertyName, propertySolid } from "./lib/palette";

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

export function DayTimeline({
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
