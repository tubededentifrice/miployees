import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import { fmtTime } from "@/lib/dates";
import type { Booking, BookingStatus, Me } from "@/types/api";

const STATUS_LABEL: Record<BookingStatus, string> = {
  pending_approval: "Pending approval",
  scheduled: "Scheduled",
  completed: "Completed",
  cancelled_by_client: "Cancelled (client)",
  cancelled_by_agency: "Cancelled (agency)",
  no_show_worker: "No-show",
  adjusted: "Completed (edited)",
};

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function fmtRange(b: Booking): string {
  return `${fmtTime(b.scheduled_start)} – ${fmtTime(b.scheduled_end)}`;
}

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

export default function BookingsPage() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const bookingsQ = useQuery({
    queryKey: qk.bookings(),
    queryFn: () => fetchJson<Booking[]>("/api/v1/bookings"),
  });

  const amend = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      fetchJson<Booking>(`/api/v1/bookings/${id}/amend`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.bookings() });
    },
  });

  const decline = useMutation({
    mutationFn: (id: string) =>
      fetchJson<Booking>(`/api/v1/bookings/${id}/decline`, {
        method: "POST",
        body: JSON.stringify({ reason: "Sick today" }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.bookings() });
    },
  });

  const header = (
    <PageHeader
      title="My bookings"
      sub="Your booked work — duration is the contract; tap a row to amend or decline."
    />
  );

  if (me.isPending || bookingsQ.isPending) {
    return (
      <>
        {header}
        <section className="phone__section"><Loading /></section>
      </>
    );
  }
  if (me.isError || !me.data || bookingsQ.isError || !bookingsQ.data) {
    return (
      <>
        {header}
        <section className="phone__section">
          <p className="muted">Failed to load.</p>
        </section>
      </>
    );
  }

  const myEmployeeId = me.data.employee.id;
  const myBookings = bookingsQ.data
    .filter((b) => b.employee_id === myEmployeeId)
    .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));

  const upcoming = myBookings.filter((b) => b.status === "scheduled" || b.status === "pending_approval");
  const past = myBookings.filter((b) => b.status !== "scheduled" && b.status !== "pending_approval");

  return (
    <>
      {header}

      <section className="phone__section">
        <h2 className="section-title">Upcoming</h2>
        {upcoming.length === 0 ? (
          <p className="muted">Nothing scheduled. Quiet day.</p>
        ) : (
          <ul className="booking-list">
            {upcoming.map((b) => (
              <li key={b.id} className={`booking-card booking-card--${b.status}`}>
                <div className="booking-card__head">
                  <strong>{fmtDate(b.scheduled_start)}</strong>
                  <span className="booking-card__time">{fmtRange(b)}</span>
                </div>
                <div className="booking-card__meta">
                  <span className="booking-card__pill">
                    {STATUS_LABEL[b.status]}
                  </span>
                  <span className="booking-card__dur">
                    {fmtDuration(bookingMinutes(b))}
                  </span>
                </div>
                {b.notes_md && <p className="booking-card__note">{b.notes_md}</p>}
                {b.status === "scheduled" && (
                  <div className="booking-card__actions">
                    <button
                      type="button"
                      className="btn btn--moss btn--sm"
                      onClick={() =>
                        amend.mutate({
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
                    <button
                      type="button"
                      className="btn btn--rust btn--sm"
                      onClick={() => decline.mutate(b.id)}
                    >
                      Decline
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="phone__section">
        <h2 className="section-title">Recent</h2>
        {past.length === 0 ? (
          <p className="muted">No bookings yet.</p>
        ) : (
          <ul className="booking-list">
            {past.slice().reverse().map((b) => (
              <li key={b.id} className={`booking-card booking-card--${b.status}`}>
                <div className="booking-card__head">
                  <strong>{fmtDate(b.scheduled_start)}</strong>
                  <span className="booking-card__time">{fmtRange(b)}</span>
                </div>
                <div className="booking-card__meta">
                  <span className="booking-card__pill">
                    {STATUS_LABEL[b.status]}
                  </span>
                  <span className="booking-card__dur">
                    {fmtDuration(bookingMinutes(b))}
                  </span>
                </div>
                {b.adjusted && b.adjustment_reason && (
                  <p className="booking-card__note">
                    <em>Edited:</em> {b.adjustment_reason}
                  </p>
                )}
                {b.pending_amend_minutes != null && (
                  <p className="booking-card__pending">
                    Pending manager approval: {fmtDuration(b.pending_amend_minutes)}{" "}
                    — {b.pending_amend_reason}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
