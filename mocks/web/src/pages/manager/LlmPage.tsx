import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import type { LLMCall, ModelAssignment } from "@/types/api";

interface AssignmentsPayload {
  assignments: ModelAssignment[];
  total_spent: number;
  total_budget: number;
  total_calls: number;
}

const STATUS_TONE: Record<LLMCall["status"], "moss" | "rust" | "sand"> = {
  ok: "moss",
  error: "rust",
  redacted_block: "sand",
};

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export default function LlmPage() {
  const assignQ = useQuery({
    queryKey: qk.llmAssignments(),
    queryFn: () => fetchJson<AssignmentsPayload>("/api/v1/llm/assignments"),
  });
  const callsQ = useQuery({
    queryKey: qk.llmCalls(),
    queryFn: () => fetchJson<LLMCall[]>("/api/v1/llm/calls"),
  });

  const sub = "Per-capability model assignment. Budgets are soft — the system warns and stops when exceeded.";
  const actions = (
    <>
      <button className="btn btn--ghost">Prompt library</button>
      <button className="btn btn--moss">+ Provider</button>
    </>
  );

  if (assignQ.isPending || callsQ.isPending) {
    return <DeskPage title="LLM & agents" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!assignQ.data || !callsQ.data) {
    return <DeskPage title="LLM & agents" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const { assignments, total_spent, total_budget, total_calls } = assignQ.data;
  const calls = callsQ.data;

  return (
    <DeskPage title="LLM & agents" sub={sub} actions={actions}>
      <section className="grid grid--stats">
        <StatCard
          label="24h spend"
          value={formatMoney(Math.round(total_spent * 100), "USD")}
          sub={"of " + formatMoney(Math.round(total_budget * 100), "USD") + " budget"}
        />
        <StatCard
          label="Calls (24h)"
          value={total_calls}
          sub={"across " + assignments.length + " capabilities"}
        />
        <StatCard label="PII redactions" value={1} sub="blocked outbound in 24h" />
        <StatCard label="Default model" value="gemma-4-31b-it" sub="via OpenRouter" />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Capabilities</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Capability</th><th>Model</th><th>24h calls</th><th>Budget</th><th>Spent</th><th></th>
            </tr>
          </thead>
          <tbody>
            {assignments.map((a) => {
              const pct = a.daily_budget_usd > 0
                ? (a.spent_24h_usd / a.daily_budget_usd) * 100
                : 0;
              return (
                <tr key={a.capability}>
                  <td>
                    <code className="inline-code">{a.capability}</code>
                    <div className="table__sub">{a.description}</div>
                  </td>
                  <td className="mono">
                    {a.model_id}
                    <div className="table__sub">{a.provider}</div>
                  </td>
                  <td className="mono">{a.calls_24h}</td>
                  <td className="mono">{formatMoney(Math.round(a.daily_budget_usd * 100), "USD")}</td>
                  <td className="mono">
                    <ProgressBar value={pct} slim />{" "}
                    <span>{formatMoney(Math.round(a.spent_24h_usd * 100), "USD")}</span>
                  </td>
                  <td>
                    {a.enabled ? (
                      <Chip tone="moss" size="sm">on</Chip>
                    ) : (
                      <Chip tone="ghost" size="sm">off</Chip>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Recent calls</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>When</th><th>Capability</th><th>Model</th><th>Tokens (in / out)</th>
              <th>Cost</th><th>Latency</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((c, idx) => (
              <tr key={idx}>
                <td className="mono">{hms(c.at)}</td>
                <td><code className="inline-code">{c.capability}</code></td>
                <td className="mono muted">{c.model_id}</td>
                <td className="mono">{c.input_tokens} / {c.output_tokens}</td>
                <td className="mono">{formatMoney(c.cost_cents, "USD")}</td>
                <td className="mono">{c.latency_ms} ms</td>
                <td><Chip tone={STATUS_TONE[c.status]} size="sm">{c.status}</Chip></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
