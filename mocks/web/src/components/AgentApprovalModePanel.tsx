import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { AgentApprovalMode } from "@/types/api";

// §11 — the user's own agent-approval mode. The three options are
// the "signal level" the user wants from their embedded chat agent
// before it commits a mutation. Cards are rendered from the
// `x-agent-confirm` annotation on the REST route itself (§12), so
// the copy is authored once and shared by the CLI, REST middleware,
// and this UI.
interface ModeChoice {
  value: AgentApprovalMode;
  label: string;
  tagline: string;
  body: string;
}

const CHOICES: ModeChoice[] = [
  {
    value: "bypass",
    label: "Bypass",
    tagline: "Never pause",
    body:
      "Run every action I could run myself, without asking. The workspace's always-gated actions (money routing, vendor invoices) still need a manager's approval in /approvals — bypass only covers my own self-confirmations.",
  },
  {
    value: "auto",
    label: "Auto",
    tagline: "Pause on impactful actions",
    body:
      "Ask me before doing things I'd want to double-check — creating an expense, completing a task, restocking inventory. The list is declared on each API route; less-impactful work happens silently.",
  },
  {
    value: "strict",
    label: "Strict",
    tagline: "Pause on every change",
    body:
      "Confirm every single write the agent proposes. Reads still happen silently. Good for onboarding and for agents you don't yet trust; expect more taps.",
  },
];

export default function AgentApprovalModePanel({
  variant = "desktop",
}: {
  variant?: "desktop" | "phone";
}) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: qk.agentApprovalMode(),
    queryFn: () => fetchJson<{ mode: AgentApprovalMode }>("/api/v1/me/agent_approval_mode"),
  });

  const set = useMutation({
    mutationFn: (mode: AgentApprovalMode) =>
      fetchJson<{ mode: AgentApprovalMode }>("/api/v1/me/agent_approval_mode", {
        method: "PUT",
        body: { mode },
      }),
    onMutate: async (mode) => {
      await qc.cancelQueries({ queryKey: qk.agentApprovalMode() });
      const prev = qc.getQueryData<{ mode: AgentApprovalMode }>(qk.agentApprovalMode());
      qc.setQueryData(qk.agentApprovalMode(), { mode });
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.agentApprovalMode(), ctx.prev);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.me() });
    },
  });

  const current = q.data?.mode ?? "strict";

  const wrapperClass = variant === "phone" ? "phone__section" : "panel";

  return (
    <section className={wrapperClass} aria-labelledby="agent-approval-mode-heading">
      {variant === "phone" ? (
        <h2 className="section-title" id="agent-approval-mode-heading">
          Agent approval mode
        </h2>
      ) : (
        <header className="panel__head">
          <h2 id="agent-approval-mode-heading">Agent approval mode</h2>
        </header>
      )}
      <p className="muted">
        Controls when your embedded chat agent pauses for a confirmation card
        before making changes on your behalf. Workspace policy (§11) still gates
        committee-level actions regardless of the mode you pick.
      </p>
      <fieldset className="agent-mode-choices">
        <legend className="sr-only">Choose your agent approval mode</legend>
        {CHOICES.map((c) => {
          const selected = current === c.value;
          return (
            <label
              key={c.value}
              className={"agent-mode-choice" + (selected ? " agent-mode-choice--selected" : "")}
            >
              <input
                type="radio"
                name="agent-approval-mode"
                value={c.value}
                checked={selected}
                onChange={() => set.mutate(c.value)}
                className="agent-mode-choice__input"
              />
              <div className="agent-mode-choice__body">
                <div className="agent-mode-choice__head">
                  <strong>{c.label}</strong>
                  <span className="muted">{c.tagline}</span>
                </div>
                <p className="agent-mode-choice__desc">{c.body}</p>
              </div>
            </label>
          );
        })}
      </fieldset>
    </section>
  );
}
