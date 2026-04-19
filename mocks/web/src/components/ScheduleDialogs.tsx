import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  AvailabilityOverride,
  Leave,
  SelfWeeklyAvailabilitySlot,
} from "@/types/api";

// Shared leave-request and availability-override dialogs. `/schedule`
// (§14 "Schedule view") is the canonical surface; same approval
// semantics (§06 "Approval logic (hybrid model)"), cross-invalidating
// the query keys each surface reads from.

const LEAVE_CATEGORIES: { value: Leave["category"]; label: string }[] = [
  { value: "vacation",    label: "Vacation" },
  { value: "sick",        label: "Sick" },
  { value: "personal",    label: "Personal" },
  { value: "bereavement", label: "Bereavement" },
  { value: "other",       label: "Other" },
];

function toMin(hhmm: string): number {
  const [h, m] = hhmm.split(":");
  return Number(h) * 60 + Number(m);
}

function invalidateScheduleQueries(
  qc: ReturnType<typeof useQueryClient>,
  empId: string | null,
): void {
  qc.invalidateQueries({ queryKey: ["my-schedule"] });
  qc.invalidateQueries({ queryKey: qk.meOverrides() });
  qc.invalidateQueries({ queryKey: qk.me() });
  if (empId) qc.invalidateQueries({ queryKey: qk.employeeLeaves(empId) });
  qc.invalidateQueries({ queryKey: qk.leaves() });
}

export function OverrideDialog({
  iso,
  pattern,
  employeeId,
  onClose,
}: {
  iso: string | null;
  pattern: SelfWeeklyAvailabilitySlot | null;
  employeeId: string | null;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const qc = useQueryClient();
  const [available, setAvailable] = useState(true);
  const [starts, setStarts] = useState<string>("09:00");
  const [ends, setEnds] = useState<string>("17:00");
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (iso === null) return;
    setAvailable(true);
    setStarts(pattern?.starts_local ?? "09:00");
    setEnds(pattern?.ends_local ?? "17:00");
    setReason("");
    const d = dialogRef.current;
    if (d && !d.open) d.showModal();
    return () => {
      if (d && d.open) d.close();
    };
  }, [iso, pattern]);

  const m = useMutation({
    mutationFn: (body: unknown) =>
      fetchJson<AvailabilityOverride>("/api/v1/me/availability_overrides", {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      invalidateScheduleQueries(qc, employeeId);
      onClose();
    },
  });

  const wouldNarrow = (() => {
    if (!pattern || pattern.starts_local === null) {
      return !available;
    }
    if (!available) return true;
    const pm = toMin(pattern.starts_local);
    const pe = toMin(pattern.ends_local!);
    return toMin(starts) > pm || toMin(ends) < pe;
  })();

  if (!iso) return null;

  return (
    <dialog className="modal" ref={dialogRef} onClose={onClose}>
      <form
        className="modal__body"
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate({
            date: iso,
            available,
            starts_local: available ? starts : null,
            ends_local: available ? ends : null,
            reason: reason.trim() || null,
          });
        }}
      >
        <h3 className="modal__title">Adjust this day</h3>
        <p className="modal__sub">
          {iso} · {wouldNarrow ? (
            <span className="avail-note avail-note--warn">
              You're reducing availability — this needs manager approval.
            </span>
          ) : (
            <span className="avail-note avail-note--ok">
              You're adding availability — this is auto-approved.
            </span>
          )}
        </p>

        <fieldset className="avail-toggle">
          <label>
            <input
              type="radio"
              name="avail"
              checked={available}
              onChange={() => setAvailable(true)}
            />
            <span>Working</span>
          </label>
          <label>
            <input
              type="radio"
              name="avail"
              checked={!available}
              onChange={() => setAvailable(false)}
            />
            <span>Off</span>
          </label>
        </fieldset>

        {available && (
          <div className="avail-hours">
            <label className="field">
              <span>From</span>
              <input type="time" value={starts} onChange={(e) => setStarts(e.target.value)} />
            </label>
            <label className="field">
              <span>Until</span>
              <input type="time" value={ends} onChange={(e) => setEnds(e.target.value)} />
            </label>
          </div>
        )}

        <label className="field">
          <span>Reason (optional)</span>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Doctor appointment, covering Maria…"
          />
        </label>

        <div className="modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--moss" disabled={m.isPending}>
            {m.isPending ? "Submitting…" : wouldNarrow ? "Request change" : "Save"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

export function LeaveDialog({
  iso,
  employeeId,
  onClose,
}: {
  iso: string | null;
  employeeId: string | null;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const qc = useQueryClient();
  const [starts, setStarts] = useState<string>("");
  const [ends, setEnds] = useState<string>("");
  const [category, setCategory] = useState<Leave["category"]>("vacation");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (iso === null) return;
    setStarts(iso);
    setEnds(iso);
    setCategory("vacation");
    setNote("");
    const d = dialogRef.current;
    if (d && !d.open) d.showModal();
    return () => {
      if (d && d.open) d.close();
    };
  }, [iso]);

  const m = useMutation({
    mutationFn: (body: unknown) =>
      fetchJson<Leave>("/api/v1/me/leaves", { method: "POST", body }),
    onSuccess: () => {
      invalidateScheduleQueries(qc, employeeId);
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
          if (!starts || !ends || ends < starts) return;
          m.mutate({ category, starts_on: starts, ends_on: ends, note_md: note.trim() || null });
        }}
      >
        <h3 className="modal__title">Request leave</h3>
        <p className="modal__sub">Approval required. Your manager will get a notification.</p>

        <div className="avail-hours">
          <label className="field">
            <span>From</span>
            <input type="date" value={starts} required onChange={(e) => setStarts(e.target.value)} />
          </label>
          <label className="field">
            <span>Until</span>
            <input type="date" value={ends} required onChange={(e) => setEnds(e.target.value)} />
          </label>
        </div>

        <label className="field">
          <span>Category</span>
          <select value={category} onChange={(e) => setCategory(e.target.value as Leave["category"])}>
            {LEAVE_CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>{c.label}</option>
            ))}
          </select>
        </label>

        <label className="field">
          <span>Note (optional)</span>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Family visit, medical…" />
        </label>

        <div className="modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--moss" disabled={m.isPending}>
            {m.isPending ? "Submitting…" : "Request leave"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
