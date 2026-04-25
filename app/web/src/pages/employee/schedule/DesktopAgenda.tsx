// Desktop variant of the `/schedule` agenda (§14 "Schedule view").
// One ISO week per row, rendered as a 7-col Mon..Sun grid; every cell
// shares the same per-week `TimeWindow` so heights line up across the
// row. Stack many of these vertically and the bidirectional infinite
// agenda's groups appear as a continuous, glanceable planner page.

import { useMemo } from "react";
import type { MySchedulePayload } from "@/types/api";
import { DayCellView } from "./DayCell";
import { computeWindow } from "./lib/bookingHelpers";
import type { DayCell } from "./lib/buildCells";
import { dayLabel, isoWeekday } from "./lib/dateHelpers";
import { WEEKDAYS } from "./lib/palette";

export function ScheduleWeekGrid({
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
