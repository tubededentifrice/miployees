import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { Property, Task } from "@/types/api";

// §06 quick-add. Clicking the button opens a <dialog> (same pattern as
// the task skip modal in TaskDetailPage) and POSTs to /api/v1/tasks.
// Default is `is_personal = true` — a flip-to-team toggle lives in the
// modal so team tasks still take one click.

interface NewTaskBody {
  title: string;
  scheduled_start: string;
  property_id?: string;
  area?: string;
  is_personal: boolean;
}

export default function NewTaskButton() {
  const ref = useRef<HTMLDialogElement>(null);
  const qc = useQueryClient();
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const todayIso = new Date().toISOString().slice(0, 10);
  const [title, setTitle] = useState("");
  const [due, setDue] = useState(todayIso);
  const [propertyId, setPropertyId] = useState("");
  const [area, setArea] = useState("");
  const [personal, setPersonal] = useState(true);

  const reset = () => {
    setTitle("");
    setDue(todayIso);
    setPropertyId("");
    setArea("");
    setPersonal(true);
  };

  const create = useMutation({
    mutationFn: (payload: NewTaskBody) =>
      fetchJson<Task>("/api/v1/tasks", { method: "POST", body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.today() });
      qc.invalidateQueries({ queryKey: qk.week() });
      qc.invalidateQueries({ queryKey: qk.tasks() });
      ref.current?.close();
      reset();
    },
  });

  return (
    <>
      <button
        type="button"
        className="btn btn--moss btn--sm"
        onClick={() => ref.current?.showModal()}
      >
        + New task
      </button>

      <dialog className="modal" ref={ref} onClose={reset}>
        <form
          className="modal__body"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = title.trim();
            if (!trimmed) return;
            const scheduled = new Date(due + "T09:00:00");
            create.mutate({
              title: trimmed,
              scheduled_start: scheduled.toISOString(),
              property_id: propertyId || undefined,
              area: area.trim() || undefined,
              is_personal: personal,
            });
          }}
        >
          <h3 className="modal__title">New task</h3>
          <p className="modal__sub">
            {personal
              ? "Personal — only you can see this."
              : "Team task — visible to your manager."}
          </p>

          <label className="field">
            <span>Title</span>
            <input
              autoFocus
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Call back Maria about the stay"
            />
          </label>

          <label className="field">
            <span>Due</span>
            <input
              type="date"
              value={due}
              onChange={(e) => setDue(e.target.value)}
            />
          </label>

          <label className="field">
            <span>Property</span>
            <select
              value={propertyId}
              onChange={(e) => setPropertyId(e.target.value)}
            >
              <option value="">No property</option>
              {(propsQ.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </label>

          {propertyId && (
            <label className="field">
              <span>Area (optional)</span>
              <input
                value={area}
                onChange={(e) => setArea(e.target.value)}
                placeholder="e.g. Kitchen"
              />
            </label>
          )}

          <label className="field field--inline">
            <input
              type="checkbox"
              checked={personal}
              onChange={(e) => setPersonal(e.target.checked)}
            />
            <span>Keep this personal (only I can see it)</span>
          </label>

          <div className="modal__actions">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => ref.current?.close()}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={create.isPending || !title.trim()}
            >
              {create.isPending ? "Adding…" : "Add task"}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}
