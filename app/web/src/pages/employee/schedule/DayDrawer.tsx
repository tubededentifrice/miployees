// Day drawer for `/schedule` (§14 "Schedule view"). Opens when a day
// cell is clicked anywhere on phone or desktop; hosts the canonical
// per-day surface (rota, bookings, tasks) plus the inline §09
// amend/decline actions and the request-leave / request-override /
// propose-booking dialogs.

import { Link } from "react-router-dom";
import { useMemo } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import type { Booking, MySchedulePayload } from "@/types/api";
import { DayTimeline } from "./DayTimeline";
import { hoursLabel } from "./lib/availability";
import {
  BOOKING_STATUS_LABEL,
  bookingMinutes,
  computeWindow,
  fmtDuration,
  fmtHM,
} from "./lib/bookingHelpers";
import type { DayCell } from "./lib/buildCells";
import { timeOfTask } from "./lib/dateHelpers";
import { propertyColor, propertyName } from "./lib/palette";

export { BookingProposeDialog } from "./BookingProposeDialog";

export function DayDrawer({
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
