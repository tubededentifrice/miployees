// §09 "Ad-hoc bookings" — worker proposes an unscheduled booking
// (swung by for laundry, covered a gap). Always lands with
// `status = pending_approval`; the manager sees it in the queue and
// approves or rejects. The mock implements the minimum viable form;
// the production shell will expand it to match the full §09 body.

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { Booking } from "@/types/api";

export function BookingProposeDialog({
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

  // Re-init only when the dialog OPENS (iso flips from null to a date).
  // We deliberately don't depend on `properties`: once the dialog is
  // open, an SSE-driven `["my-schedule"]` invalidation regenerates the
  // merged payload (and hence `properties` array reference) on every
  // event — depending on it would clobber the worker's half-typed
  // form mid-edit. Properties are reachable on first paint (the dialog
  // only opens from a loaded day cell), so the empty fallback below
  // never triggers in practice.
  const propertiesRef = useRef(properties);
  propertiesRef.current = properties;
  useEffect(() => {
    if (iso === null) return;
    setPropertyId(propertiesRef.current[0]?.id ?? "");
    setStarts("09:00");
    setEnds("12:00");
    setNotes("");
    const d = dialogRef.current;
    if (d && !d.open) d.showModal();
    return () => {
      if (d && d.open) d.close();
    };
  }, [iso]);

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
