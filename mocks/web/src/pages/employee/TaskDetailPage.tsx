import { useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Ban, Camera, Check, SkipForward } from "lucide-react";
import { Chip, Loading } from "@/components/common";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import ChatLog from "@/components/chat/ChatLog";
import ChatComposer from "@/components/chat/ChatComposer";
import PageHeader from "@/components/PageHeader";
import { fmtTime } from "@/lib/dates";
import type { AgentMessage, Instruction, Property, Task } from "@/types/api";

interface TaskPayload {
  task: Task;
  property: Property | null;
  instructions: Instruction[];
}

const STATUS_TONE: Record<Task["status"], "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  scheduled: "ghost",
  skipped: "rust",
  cancelled: "rust",
  overdue: "rust",
};

export default function TaskDetailPage() {
  const { tid = "" } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const modalRef = useRef<HTMLDialogElement>(null);
  const [skipReason, setSkipReason] = useState("");
  const [chatDraft, setChatDraft] = useState("");

  const q = useQuery({
    queryKey: qk.task(tid),
    queryFn: () => fetchJson<TaskPayload>("/api/v1/tasks/" + tid),
    enabled: Boolean(tid),
  });

  const chatQ = useQuery({
    queryKey: qk.agentTaskChat(tid),
    queryFn: () => fetchJson<AgentMessage[]>("/api/v1/tasks/" + tid + "/chat/log"),
    enabled: Boolean(tid),
  });

  const chatSend = useMutation({
    mutationFn: (body: string) =>
      fetchJson<AgentMessage>("/api/v1/tasks/" + tid + "/chat/message", {
        method: "POST", body: { body },
      }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: qk.agentTaskChat(tid) });
      const prev = qc.getQueryData<AgentMessage[]>(qk.agentTaskChat(tid)) ?? [];
      const optimistic: AgentMessage = { at: new Date().toISOString(), kind: "user", body };
      qc.setQueryData<AgentMessage[]>(qk.agentTaskChat(tid), [...prev, optimistic]);
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.agentTaskChat(tid), ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.agentTaskChat(tid) }),
  });

  const chatDecide = useMutation({
    mutationFn: ({ idx, decision }: { idx: number; decision: "approve" | "details" }) =>
      fetchJson<AgentMessage[]>(
        "/api/v1/tasks/" + tid + "/chat/action/" + idx + "/" + decision,
        { method: "POST" },
      ),
    onSuccess: (log) => qc.setQueryData(qk.agentTaskChat(tid), log),
  });

  const checkMutation = useMutation({
    mutationFn: (idx: number) =>
      fetchJson<Task>("/api/v1/tasks/" + tid + "/check/" + idx, { method: "POST" }),
    onMutate: async (idx) => {
      await qc.cancelQueries({ queryKey: qk.task(tid) });
      const prev = qc.getQueryData<TaskPayload>(qk.task(tid));
      if (prev) {
        const next = {
          ...prev,
          task: {
            ...prev.task,
            checklist: prev.task.checklist.map((it, i) =>
              i === idx ? { ...it, done: !it.done } : it,
            ),
          },
        };
        qc.setQueryData(qk.task(tid), next);
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.task(tid), ctx.prev);
    },
    onSuccess: (task) => {
      qc.setQueryData<TaskPayload>(qk.task(tid), (prev) => (prev ? { ...prev, task } : prev));
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  const complete = useMutation({
    mutationFn: () =>
      fetchJson<Task>("/api/v1/tasks/" + tid + "/complete", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.task(tid) });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  const skip = useMutation({
    mutationFn: (reason: string) =>
      fetchJson<Task>("/api/v1/tasks/" + tid + "/skip", {
        method: "POST",
        body: { reason },
      }),
    onSuccess: () => {
      modalRef.current?.close();
      setSkipReason("");
      qc.invalidateQueries({ queryKey: qk.task(tid) });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  if (q.isPending) {
    return (
      <>
        <PageHeader title="Task" />
        <section className="phone__section"><Loading /></section>
      </>
    );
  }
  if (q.isError || !q.data) {
    nav("/today", { replace: true });
    return null;
  }

  const { task, property, instructions } = q.data;
  const terminal = task.status === "completed" || task.status === "skipped";

  return (
    <>
      <PageHeader
        title={task.title}
        overflow={
          terminal
            ? undefined
            : [
                {
                  label: "Skip this task",
                  icon: <SkipForward size={18} strokeWidth={1.8} aria-hidden="true" />,
                  onSelect: () => modalRef.current?.showModal(),
                  destructive: true,
                },
              ]
        }
      />
      <section className="phone__section phone__section--detail">
        {!terminal && (
          <div className="task-detail__sticky">
            <form
              className="task-detail__sticky-form"
              onSubmit={(e) => { e.preventDefault(); complete.mutate(); }}
            >
              <button className="btn btn--moss btn--lg" type="submit">
                {task.photo_evidence === "required" ? (
                  <><Camera size={18} strokeWidth={1.8} aria-hidden="true" /> Complete with photo</>
                ) : (
                  "Mark done"
                )}
              </button>
            </form>
          </div>
        )}

        <header className="task-detail__head">
          <div className="task-detail__chips">
            {property ? (
              <Chip tone={property.color}>{property.name}</Chip>
            ) : task.is_personal ? (
              <Chip tone="ghost">Personal</Chip>
            ) : null}
            {task.area && <Chip tone="ghost">{task.area}</Chip>}
            {(task.priority === "high" || task.priority === "urgent") && (
              <Chip tone="rust">{task.priority === "urgent" ? "Urgent" : "High"}</Chip>
            )}
            {task.photo_evidence === "required" ? (
              <Chip tone="sand"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> required</Chip>
            ) : task.photo_evidence === "optional" ? (
              <Chip tone="ghost" size="sm"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> optional</Chip>
            ) : null}
            <Chip tone={STATUS_TONE[task.status]} size="sm">
              {task.status.replace("_", " ")}
            </Chip>
          </div>
          <div className="task-detail__meta">
            {fmtTime(task.scheduled_start)} · est. {task.estimated_minutes} min
          </div>
        </header>

      {task.checklist.length > 0 && (
        <div className="checklist">
          <h3 className="section-title section-title--sm">Checklist</h3>
          <ul>
            {task.checklist.map((item, idx) => (
              <li
                key={idx}
                className={"checklist__item" + (item.done ? " checklist__item--done" : "")}
                onClick={() => checkMutation.mutate(idx)}
              >
                <span className="checklist__box" aria-hidden="true"><Check size={12} strokeWidth={2.5} /></span>
                <span className="checklist__label">{item.label}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {instructions.length > 0 && (
        <section className="instructions">
          <h3 className="section-title section-title--sm">Instructions</h3>
          {instructions.map((i, idx) => (
            <details key={i.id} className="instruction-card" open={idx === 0}>
              <summary>
                <span className="instruction-card__title">{i.title}</span>
                <Chip tone="ghost" size="sm">
                  {i.scope === "area"
                    ? i.area
                    : i.scope === "property"
                      ? (property?.name ?? "Property")
                      : "House-wide"}
                </Chip>
              </summary>
              <div className="instruction-card__body">{i.body_md}</div>
            </details>
          ))}
        </section>
      )}

      {(task.photo_evidence === "optional" || task.photo_evidence === "required") && (
        <section className="evidence">
          <h3 className="section-title section-title--sm">
            Evidence {task.photo_evidence === "required" && (
              <Chip tone="sand" size="sm">required</Chip>
            )}
          </h3>
          <label className="evidence__picker">
            <input type="file" accept="image/*" capture="environment" />
            <span className="evidence__picker-cta"><Camera size={16} strokeWidth={1.8} aria-hidden="true" /> Take photo</span>
            <span className="evidence__picker-sub">or choose from your gallery</span>
          </label>
          <p className="evidence__note-hint muted">
            Anything the manager should know? Tell the assistant below — it'll
            log a note on this task when it matters.
          </p>
        </section>
      )}

      <section className="comments task-chat">
        <h3 className="section-title section-title--sm">Notes (chat)</h3>
        <p className="muted">
          Messages to and from your workspace assistant — scoped to this task.
        </p>
        <ChatLog
          messages={chatQ.data}
          onDecideAction={(idx, decision) => chatDecide.mutate({ idx, decision })}
          variant="inline"
          ariaLabel="Task conversation with assistant"
        />
        <ChatComposer
          value={chatDraft}
          onChange={setChatDraft}
          onSubmit={(trimmed) => {
            chatSend.mutate(trimmed);
            setChatDraft("");
          }}
          placeholder="Ask about this task or share what you saw…"
          ariaLabel="Message the assistant about this task"
          variant="inline"
        />
      </section>

      {task.status === "completed" && (
        <div className="done-banner">
          <Check size={16} strokeWidth={2.25} aria-hidden="true" /> Completed
        </div>
      )}
      {task.status === "skipped" && (
        <div className="done-banner done-banner--rust">
          <Ban size={16} strokeWidth={2.25} aria-hidden="true" /> Skipped
        </div>
      )}

      <dialog id="skip-modal" className="modal" ref={modalRef}>
        <form
          className="modal__body"
          onSubmit={(e) => { e.preventDefault(); skip.mutate(skipReason); }}
        >
          <h3 className="modal__title">Skip this task?</h3>
          <p className="modal__sub">Give a quick reason so the manager knows. It'll go in the audit log.</p>
          <label className="field">
            <span>Reason</span>
            <AutoGrowTextarea
              required
              placeholder="e.g. Guest still in the room — came back early from their day."
              value={skipReason}
              onChange={(e) => setSkipReason(e.target.value)}
            />
          </label>
          <div className="modal__actions">
            <button
              className="btn btn--ghost"
              type="button"
              onClick={() => modalRef.current?.close()}
            >
              Cancel
            </button>
            <button className="btn btn--rust" type="submit">Skip task</button>
          </div>
        </form>
      </dialog>
      </section>
    </>
  );
}
