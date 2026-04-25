// Phone variant of the `/schedule` agenda (§14 "Schedule view").
// Stacks one day card per row, one full row per ISO week. The same
// per-week time window is shared across the seven days so vertically
// scrolling from Monday to Sunday feels like one continuous planner
// page. Empty days collapse to a short "rest" card — they don't claim
// the shared row height because the cards are stacked, not gridded.

import { useMemo } from "react";
import type { MySchedulePayload } from "@/types/api";
import { DayCellView } from "./DayCell";
import { computeWindow } from "./lib/bookingHelpers";
import type { DayCell } from "./lib/buildCells";

export function SchedulePhoneWeek({
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
