import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { AgentMessage } from "@/types/api";

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function ChatPage() {
  const qc = useQueryClient();
  const logRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState("");

  const q = useQuery({
    queryKey: qk.agentEmployeeLog(),
    queryFn: () => fetchJson<AgentMessage[]>("/api/v1/agent/employee/log"),
  });

  const send = useMutation({
    mutationFn: (body: string) =>
      fetchJson<AgentMessage>("/api/v1/agent/employee/message", {
        method: "POST", body: { body },
      }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: qk.agentEmployeeLog() });
      const prev = qc.getQueryData<AgentMessage[]>(qk.agentEmployeeLog()) ?? [];
      const optimistic: AgentMessage = { at: new Date().toISOString(), kind: "user", body };
      qc.setQueryData<AgentMessage[]>(qk.agentEmployeeLog(), [...prev, optimistic]);
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.agentEmployeeLog(), ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.agentEmployeeLog() }),
  });

  const decide = useMutation({
    mutationFn: ({ idx, decision }: { idx: number; decision: "approve" | "details" }) =>
      fetchJson<AgentMessage[]>("/api/v1/chat/action/" + idx + "/" + decision, { method: "POST" }),
    onSuccess: (log) => qc.setQueryData(qk.agentEmployeeLog(), log),
  });

  // Scroll to bottom on message count change.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [q.data?.length]);

  const autogrow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed) return;
    send.mutate(trimmed);
    setDraft("");
    if (textRef.current) {
      textRef.current.style.height = "auto";
    }
  };

  return (
    <>
      <section className="chat-screen">
        <div className="chat-screen__header">
          <div>
            <h2 className="chat-screen__title">Assistant</h2>
            <span className="chat-screen__status">
              <span className="chat-screen__dot" aria-hidden="true" />
              online
            </span>
          </div>
        </div>

        <div className="chat-log" role="log" aria-live="polite" ref={logRef}>
          {q.data?.map((m, idx) => {
            if (m.kind === "action") {
              return (
                <div key={idx} className="chat-msg chat-msg--action">
                  <span className="chat-msg__body">{m.body}</span>
                  <div className="chat-msg__ctas">
                    <button
                      className="btn btn--moss btn--sm"
                      type="button"
                      onClick={() => decide.mutate({ idx, decision: "approve" })}
                    >
                      Approve
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      onClick={() => decide.mutate({ idx, decision: "details" })}
                    >
                      Details
                    </button>
                  </div>
                  <span className="chat-msg__time">{hhmm(m.at)}</span>
                </div>
              );
            }
            return (
              <div key={idx} className={"chat-msg chat-msg--" + m.kind}>
                <span className="chat-msg__body">{m.body}</span>
                {m.channel_kind === "offapp_whatsapp" && (
                  <span
                    className="chat-msg__channel chat-msg__channel--wa"
                    aria-label="via WhatsApp"
                    title="Arrived via WhatsApp"
                  >
                    WA
                  </span>
                )}
                <span className="chat-msg__time">{hhmm(m.at)}</span>
              </div>
            );
          })}
        </div>
      </section>

      <form className="chat-composer" onSubmit={handleSubmit}>
        <button type="button" className="chat-composer__icon" aria-label="Attach file">
          <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
               strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21.44 11.05 12.25 20.24a6 6 0 0 1-8.49-8.49l9.2-9.19a4 4 0 0 1 5.66 5.65l-9.2 9.19a2 2 0 0 1-2.83-2.82l8.49-8.49" />
          </svg>
        </button>
        <div className="chat-composer__field">
          <textarea
            ref={textRef}
            rows={1}
            placeholder="Message"
            aria-label="Message"
            value={draft}
            onChange={(e) => { setDraft(e.target.value); autogrow(e.currentTarget); }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button
            type="button"
            className="chat-composer__icon chat-composer__icon--inline"
            aria-label="Emoji"
          >
            <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
                 strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx={12} cy={12} r={9} />
              <path d="M8.5 14s1.5 2 3.5 2 3.5-2 3.5-2" />
              <line x1={9} y1={9.5} x2={9} y2={9.5} />
              <line x1={15} y1={9.5} x2={15} y2={9.5} />
            </svg>
          </button>
        </div>
        <button
          type="submit"
          className={"chat-composer__send" + (draft.trim() ? " chat-composer__send--ready" : "")}
          aria-label="Send"
        >
          <span className="chat-composer__send-icon chat-composer__send-icon--mic" aria-hidden="true">
            <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
                 strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <rect x={9} y={3} width={6} height={12} rx={3} />
              <path d="M5 11a7 7 0 0 0 14 0" />
              <line x1={12} y1={18} x2={12} y2={22} />
            </svg>
          </span>
          <span className="chat-composer__send-icon chat-composer__send-icon--send" aria-hidden="true">
            <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor"
                 strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="m4 12 16-8-6 18-3-8-7-2Z" />
            </svg>
          </span>
        </button>
      </form>
    </>
  );
}
