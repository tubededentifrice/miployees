// Bidirectional infinite agenda body for `/schedule` (§14 "Schedule
// view"). Phone stacks one day card per row; desktop stacks one
// 7-column Mon..Sun week grid per row (see `variant`). Both paths
// share the query, the sentinels, the monthbar, the anchor-to-today
// settle phase, and the Today FAB — the only thing that differs is
// how a week's cells lay out inside the week group.
//
// All scroll plumbing (callback ref → `findScrollRoot`, top + bottom
// IntersectionObservers, scroll-preservation deltas, today re-anchor
// loop, monthbar topmost-cell observer) lives in `useInfiniteAgenda`
// — this body just renders the bag of values + handlers it returns.
// Sticky chrome (banner, monthbar, dialogs footer) lives in
// `ScheduleChrome`.
//
// Why this layering matters: `/schedule` is the single view a worker
// hits to know where they are working today and tomorrow — it has to
// feel fast, land in the right place, and keep working when the
// worker idly thumbs back to last Tuesday. A stalled prepend or a
// monthbar that can't decide which month it's looking at would
// shake the worker's confidence in the page.

import { Fragment, useMemo } from "react";
import { Loading } from "@/components/common";
import { ScheduleWeekGrid } from "./DesktopAgenda";
import { SchedulePhoneWeek } from "./PhoneDay";
import {
  ScheduleBanner,
  ScheduleDialogsFooter,
  computePendingState,
} from "./ScheduleChrome";
import type { DayCell } from "./lib/buildCells";
import { addDays, isoDate, parseIsoDate, startOfIsoWeek } from "./lib/dateHelpers";
import { propertyColor } from "./lib/palette";
import { useInfiniteAgenda } from "./lib/useInfiniteAgenda";

type ScheduleVariant = "phone" | "desktop";

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

export function InfiniteScheduleBody({
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
  const {
    q,
    merged,
    cells,
    containerRef,
    topSentinelRef,
    bottomSentinelRef,
    monthLabel,
    todayInView,
    scrollToToday,
  } = useInfiniteAgenda(today, todayIso);

  const selectedCell = useMemo(
    () => (selectedIso ? cells.find((c) => c.iso === selectedIso) ?? null : null),
    [selectedIso, cells],
  );

  // ── Render ─────────────────────────────────────────────────────────
  //
  // We always mount the `.schedule` wrapper so the callback ref above
  // fires on first paint and captures the scroll root — even while
  // the initial query is in flight. Loading / failure states render
  // inside the wrapper instead of early-returning in place of it.

  if (q.isPending) {
    return (
      <div ref={containerRef} className={`schedule schedule--${variant}`}>
        <Loading />
      </div>
    );
  }
  if (!merged) {
    return (
      <div ref={containerRef} className={`schedule schedule--${variant}`}>
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
      <div ref={containerRef} className={`schedule schedule--${variant}`}>
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
