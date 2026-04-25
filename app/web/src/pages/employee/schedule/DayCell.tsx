// `DayCellView` for `/schedule` (§14 "Schedule view"). One cell is
// the atomic row unit shared by `PhoneDay` (stacked cards) and
// `DesktopAgenda` (7-col grid). Inside, `DayTimeline` paints the
// rail + hour grid + property-tinted blocks; this component owns
// only the per-day chrome (header, click target, empty-day fallback).

import type { MySchedulePayload } from "@/types/api";
import { DayTimeline } from "./DayTimeline";
import { hoursLabel } from "./lib/availability";
import { bookingNeedsAttention, type TimeWindow } from "./lib/bookingHelpers";
import type { DayCell } from "./lib/buildCells";
import { dayLabel, sameDate } from "./lib/dateHelpers";

export function DayCellView({
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
