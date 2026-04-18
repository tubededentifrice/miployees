import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { persistAgentCollapsed, readAgentCollapsedCookie } from "@/lib/preferences";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import type { AgentAction, AgentMessage, Role } from "@/types/api";

// CRITICAL: AgentSidebar MUST mount as a SIBLING of <Outlet /> in
// EmployeeLayout and ManagerLayout, never inside a route's subtree.
// React Router only remounts the outlet's subtree on navigation;
// siblings survive. That's what gives us a persistent chat log
// (scrollTop, composer draft, EventSource-fed cache) across page
// changes.
//
// `mobileOpen` / `onMobileClose` drive the off-canvas drawer used at
// tablet and phone widths. Desktop ignores them — the sidebar renders
// inline from the layout's grid. `role` selects the per-role agent
// log/message endpoints and gates the manager-only "Pending approvals"
// block (employees never see it).
interface AgentSidebarProps {
  role: Role;
  mobileOpen?: boolean;
  onMobileClose?: () => void;
}

export default function AgentSidebar({ role, mobileOpen = false, onMobileClose }: AgentSidebarProps) {
  const [collapsed, setCollapsed] = useState<boolean>(() => readAgentCollapsedCookie());
  const [draft, setDraft] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();

  const isManager = role === "manager";
  const logKey = isManager ? qk.agentManagerLog() : qk.agentEmployeeLog();
  const actionsKey = qk.agentManagerActions();
  const logUrl = isManager ? "/api/v1/agent/manager/log" : "/api/v1/agent/employee/log";
  const messageUrl = isManager ? "/api/v1/agent/manager/message" : "/api/v1/agent/employee/message";

  const log = useQuery({
    queryKey: logKey,
    queryFn: () => fetchJson<AgentMessage[]>(logUrl),
  });
  const actions = useQuery({
    queryKey: actionsKey,
    queryFn: () => fetchJson<AgentAction[]>("/api/v1/agent/manager/actions"),
    enabled: isManager,
  });

  const sendMessage = useMutation({
    mutationFn: (body: string) =>
      fetchJson<AgentMessage>(messageUrl, { method: "POST", body: { body } }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: logKey });
      const prev = qc.getQueryData<AgentMessage[]>(logKey) ?? [];
      const optimistic: AgentMessage = { at: new Date().toISOString(), kind: "user", body };
      qc.setQueryData<AgentMessage[]>(logKey, [...prev, optimistic]);
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(logKey, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: logKey }),
  });

  const decideAction = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "deny" }) =>
      fetchJson<{ ok: true }>("/api/v1/agent/manager/action/" + id + "/" + decision, {
        method: "POST",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: actionsKey });
      qc.invalidateQueries({ queryKey: logKey });
    },
  });

  // Scroll-to-bottom on new messages.
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [log.data?.length]);

  const toggle = useCallback(() => {
    setCollapsed((c) => {
      const next = !c;
      persistAgentCollapsed(next ? "collapsed" : "open");
      // Mirror state onto either layout's root for grid recalculation
      // in browsers without :has() support.
      const host = document.querySelector(".desk, .phone");
      if (host) host.setAttribute("data-agent-collapsed", next ? "true" : "false");
      return next;
    });
  }, []);

  const handleSend = useCallback(
    (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const trimmed = draft.trim();
      if (!trimmed) return;
      sendMessage.mutate(trimmed);
      setDraft("");
    },
    [draft, sendMessage],
  );

  const className =
    "desk__agent" +
    (collapsed ? " desk__agent--collapsed" : "") +
    (mobileOpen ? " desk__agent--mobile-open" : "");

  return (
    <aside className={className} aria-label="Agent sidebar">
      <button
        type="button"
        className="desk__agent-head"
        onClick={mobileOpen && onMobileClose ? onMobileClose : toggle}
        aria-expanded={mobileOpen || !collapsed}
        aria-controls="agent-body"
      >
        <span className="desk__agent-title">Agent</span>
        <span className="desk__agent-status">
          <span className="desk__agent-dot" aria-hidden="true" />
          <span>online</span>
        </span>
        <span className="desk__agent-chevron" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
               strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <polyline points={mobileOpen ? "18 15 12 9 6 15" : "6 9 12 15 18 9"} />
          </svg>
        </span>
      </button>

      <div className="desk__agent-body" id="agent-body">
        <div className="agent-log" ref={logRef} role="log" aria-live="polite">
          {log.data?.map((msg, i) => (
            <div key={i} className={"agent-msg agent-msg--" + msg.kind}>
              <span className="agent-msg__body">{msg.body}</span>
              <span className="agent-msg__time">
                {new Date(msg.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </span>
            </div>
          ))}
        </div>

        {isManager && actions.data && actions.data.length > 0 && (
          <div className="agent-actions" aria-label="Pending agent actions">
            <div className="agent-actions__title">
              <span>Pending approvals</span>
              <span className="agent-actions__count">{actions.data.length}</span>
            </div>
            <div className="agent-actions__list">
              {actions.data.map((a) => (
                <div key={a.id} className={"agent-action agent-action--" + a.risk}>
                  <div className="agent-action__title">{a.card_summary || a.title}</div>
                  {a.card_fields.length > 0 && (
                    <dl className="agent-action__fields">
                      {a.card_fields.map(([k, v]) => (
                        <div key={k} className="agent-action__field">
                          <dt>{k}</dt>
                          <dd>{v}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  <div className="agent-action__detail">{a.detail}</div>
                  <div className="agent-action__ctas">
                    <button type="button" className="btn btn--approve"
                            onClick={() => decideAction.mutate({ id: a.id, decision: "approve" })}>
                      Confirm
                    </button>
                    <button type="button" className="btn btn--deny"
                            onClick={() => decideAction.mutate({ id: a.id, decision: "deny" })}>
                      Reject
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <form className="agent-composer" onSubmit={handleSend}>
          <div className="agent-composer__field">
            <AutoGrowTextarea
              rows={1}
              maxHeight={140}
              placeholder="Ask the agent…"
              aria-label="Message agent"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
            />
          </div>
          <button type="submit" className="agent-composer__send" aria-label="Send">
            <svg viewBox="0 0 24 24" width={18} height={18} fill="none" stroke="currentColor"
                 strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="m4 12 16-8-6 18-3-8-7-2Z" />
            </svg>
          </button>
        </form>
      </div>
    </aside>
  );
}
