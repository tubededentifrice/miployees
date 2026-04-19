import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, StatCard } from "@/components/common";
import type {
  LLMCall,
  LlmAssignment,
  LlmGraphPayload,
  LlmPromptTemplate,
  LlmSyncPricingResult,
} from "@/types/api";

type Column = "provider" | "model" | "assignment" | "capability";

interface Selection {
  column: Column;
  id: string;
}

const CAPABILITY_TAG_LABEL: Record<string, string> = {
  chat: "chat",
  vision: "vision",
  audio_input: "audio",
  reasoning: "reasoning",
  function_calling: "tools",
  json_mode: "json",
  streaming: "stream",
};

const CALL_STATUS_TONE: Record<LLMCall["status"], "moss" | "rust" | "sand"> = {
  ok: "moss",
  error: "rust",
  redacted_block: "sand",
};

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function AdminLlmPage() {
  const graphQ = useQuery({
    queryKey: qk.adminLlmGraph(),
    queryFn: () => fetchJson<LlmGraphPayload>("/admin/api/v1/llm/graph"),
  });
  const callsQ = useQuery({
    queryKey: qk.adminLlmCalls(),
    queryFn: () => fetchJson<LLMCall[]>("/admin/api/v1/llm/calls"),
  });
  const promptsQ = useQuery({
    queryKey: qk.adminLlmPrompts(),
    queryFn: () => fetchJson<LlmPromptTemplate[]>("/admin/api/v1/llm/prompts"),
  });

  const [selection, setSelection] = useState<Selection | null>(null);
  const [hover, setHover] = useState<Selection | null>(null);
  const [promptsOpen, setPromptsOpen] = useState(false);

  const qc = useQueryClient();
  const syncMut = useMutation({
    mutationFn: () =>
      fetchJson<LlmSyncPricingResult>("/admin/api/v1/llm/sync-pricing", {
        method: "POST",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminLlmGraph() }),
  });

  const graph = graphQ.data;

  // Indexes — built once per fetch, cheap.
  const indexes = useMemo(() => {
    if (!graph) return null;
    const providersById = new Map(graph.providers.map((p) => [p.id, p]));
    const modelsById = new Map(graph.models.map((m) => [m.id, m]));
    const pmById = new Map(graph.provider_models.map((pm) => [pm.id, pm]));
    const capabilitiesByKey = new Map(graph.capabilities.map((c) => [c.key, c]));
    const inheritanceByChild = new Map(
      graph.inheritance.map((edge) => [edge.capability, edge.inherits_from]),
    );
    const assignmentsByCapability = new Map<string, LlmAssignment[]>();
    for (const cap of graph.capabilities) {
      assignmentsByCapability.set(cap.key, []);
    }
    for (const a of graph.assignments) {
      const list = assignmentsByCapability.get(a.capability) ?? [];
      list.push(a);
      assignmentsByCapability.set(a.capability, list);
    }
    for (const list of assignmentsByCapability.values()) {
      list.sort((x, y) => x.priority - y.priority);
    }
    const issuesByAssignment = new Map(
      graph.assignment_issues.map((i) => [i.assignment_id, i.missing_capabilities]),
    );
    return {
      providersById,
      modelsById,
      pmById,
      capabilitiesByKey,
      inheritanceByChild,
      assignmentsByCapability,
      issuesByAssignment,
    };
  }, [graph]);

  const active = hover ?? selection;

  // Compute the highlighted set whenever hover or selection changes.
  const highlighted = useMemo(() => {
    const emptySet = {
      providers: new Set<string>(),
      models: new Set<string>(),
      providerModels: new Set<string>(),
      assignments: new Set<string>(),
      capabilities: new Set<string>(),
    };
    if (!graph || !indexes || !active) return emptySet;
    const providers = new Set<string>();
    const models = new Set<string>();
    const providerModels = new Set<string>();
    const assignments = new Set<string>();
    const capabilities = new Set<string>();

    const reachableAssignmentsByPm = new Map<string, LlmAssignment[]>();
    for (const a of graph.assignments) {
      const bucket = reachableAssignmentsByPm.get(a.provider_model_id) ?? [];
      bucket.push(a);
      reachableAssignmentsByPm.set(a.provider_model_id, bucket);
    }

    if (active.column === "provider") {
      providers.add(active.id);
      for (const pm of graph.provider_models) {
        if (pm.provider_id !== active.id) continue;
        providerModels.add(pm.id);
        models.add(pm.model_id);
        for (const a of reachableAssignmentsByPm.get(pm.id) ?? []) {
          assignments.add(a.id);
          capabilities.add(a.capability);
        }
      }
    } else if (active.column === "model") {
      models.add(active.id);
      for (const pm of graph.provider_models) {
        if (pm.model_id !== active.id) continue;
        providerModels.add(pm.id);
        providers.add(pm.provider_id);
        for (const a of reachableAssignmentsByPm.get(pm.id) ?? []) {
          assignments.add(a.id);
          capabilities.add(a.capability);
        }
      }
    } else if (active.column === "assignment") {
      assignments.add(active.id);
      const a = graph.assignments.find((x) => x.id === active.id);
      if (a) {
        capabilities.add(a.capability);
        const pm = indexes.pmById.get(a.provider_model_id);
        if (pm) {
          providerModels.add(pm.id);
          models.add(pm.model_id);
          providers.add(pm.provider_id);
        }
      }
    } else if (active.column === "capability") {
      capabilities.add(active.id);
      for (const a of indexes.assignmentsByCapability.get(active.id) ?? []) {
        assignments.add(a.id);
        const pm = indexes.pmById.get(a.provider_model_id);
        if (pm) {
          providerModels.add(pm.id);
          models.add(pm.model_id);
          providers.add(pm.provider_id);
        }
      }
    }
    return { providers, models, providerModels, assignments, capabilities };
  }, [graph, indexes, active]);

  const hasActive = active !== null;
  // ── Edge measurements ──────────────────────────────────────────
  // We draw SVG bezier curves between provider → model and model →
  // assignment rung. Each node registers a ref; a ResizeObserver +
  // scroll listener recomputes positions so the overlay stays aligned
  // with the DOM.
  const graphRef = useRef<HTMLDivElement | null>(null);
  const providerRefs = useRef<Map<string, HTMLElement>>(new Map());
  const modelRefs = useRef<Map<string, HTMLElement>>(new Map());
  const rungRefs = useRef<Map<string, HTMLElement>>(new Map());
  const setRef = (map: typeof providerRefs) => (id: string) => (el: HTMLElement | null) => {
    if (el) map.current.set(id, el);
    else map.current.delete(id);
  };

  interface EdgeLayout {
    id: string;
    kind: "pm" | "assign";
    providerId: string;
    modelId: string;
    providerModelId: string;
    assignmentId?: string;
    capability?: string;
    d: string;
    invalid: boolean;
  }
  const [edges, setEdges] = useState<EdgeLayout[]>([]);
  const [canvas, setCanvas] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  // Re-measure whenever the payload or viewport changes.
  const recomputeEdges = () => {
    const host = graphRef.current;
    if (!host || !graph || !indexes) return;
    const hostBox = host.getBoundingClientRect();
    setCanvas({ w: hostBox.width, h: hostBox.height });
    const next: EdgeLayout[] = [];
    const issues = new Set(graph.assignment_issues.map((i) => i.assignment_id));

    // Provider → Model edges, one per provider-model join.
    for (const pm of graph.provider_models) {
      const provider = providerRefs.current.get(pm.provider_id);
      const model = modelRefs.current.get(pm.model_id);
      if (!provider || !model) continue;
      const pBox = provider.getBoundingClientRect();
      const mBox = model.getBoundingClientRect();
      const x1 = pBox.right - hostBox.left;
      const y1 = pBox.top + pBox.height / 2 - hostBox.top;
      const x2 = mBox.left - hostBox.left;
      const y2 = mBox.top + mBox.height / 2 - hostBox.top;
      const dx = Math.max(40, (x2 - x1) * 0.55);
      next.push({
        id: "pm-" + pm.id,
        kind: "pm",
        providerId: pm.provider_id,
        modelId: pm.model_id,
        providerModelId: pm.id,
        d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
        invalid: false,
      });
    }

    // Model → Assignment-rung edges, one per assignment row.
    for (const a of graph.assignments) {
      const pm = indexes.pmById.get(a.provider_model_id);
      if (!pm) continue;
      const model = modelRefs.current.get(pm.model_id);
      const rung = rungRefs.current.get(a.id);
      if (!model || !rung) continue;
      const mBox = model.getBoundingClientRect();
      const rBox = rung.getBoundingClientRect();
      const x1 = mBox.right - hostBox.left;
      const y1 = mBox.top + mBox.height / 2 - hostBox.top;
      const x2 = rBox.left - hostBox.left;
      const y2 = rBox.top + rBox.height / 2 - hostBox.top;
      const dx = Math.max(40, (x2 - x1) * 0.55);
      next.push({
        id: "a-" + a.id,
        kind: "assign",
        providerId: pm.provider_id,
        modelId: pm.model_id,
        providerModelId: pm.id,
        assignmentId: a.id,
        capability: a.capability,
        d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
        invalid: issues.has(a.id),
      });
    }
    setEdges(next);
  };

  // Measure after render. `graph` being present is the trigger — the
  // layout pass runs once the DOM nodes exist.
  useLayoutEffect(() => {
    recomputeEdges();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  useEffect(() => {
    if (!graphRef.current) return;
    const ro = new ResizeObserver(() => recomputeEdges());
    ro.observe(graphRef.current);
    const onWinResize = () => recomputeEdges();
    const onScroll = () => recomputeEdges();
    window.addEventListener("resize", onWinResize);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onWinResize);
      window.removeEventListener("scroll", onScroll, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  const edgeIsHighlighted = (e: EdgeLayout): boolean => {
    if (!active) return false;
    if (active.column === "provider") return e.providerId === active.id;
    if (active.column === "model") return e.modelId === active.id;
    if (active.column === "assignment") {
      return e.assignmentId === active.id || e.providerModelId === (graph?.assignments.find((x) => x.id === active.id)?.provider_model_id);
    }
    if (active.column === "capability") {
      if (e.kind === "assign") return e.capability === active.id;
      // provider-model edge: highlight when any of the active capability's
      // chain runs through this provider-model.
      const chain = indexes?.assignmentsByCapability.get(active.id) ?? [];
      return chain.some((a) => a.provider_model_id === e.providerModelId);
    }
    return false;
  };

  const nodeClass = (col: Column, id: string) => {
    const set = {
      provider: highlighted.providers,
      model: highlighted.models,
      assignment: highlighted.assignments,
      capability: highlighted.capabilities,
    }[col];
    const isOn = set.has(id);
    const isActive = active?.column === col && active.id === id;
    const dim = hasActive && !isOn;
    return [
      "llm-graph-node",
      `llm-graph-node--${col}`,
      isActive ? "is-active" : "",
      isOn && !isActive ? "is-linked" : "",
      dim ? "is-dim" : "",
    ]
      .filter(Boolean)
      .join(" ");
  };

  const actions = <button className="btn btn--moss">+ Provider</button>;
  const overflow = [
    {
      label: "Prompts",
      onSelect: () => setPromptsOpen(true),
    },
    {
      label: syncMut.isPending ? "Syncing…" : "Sync pricing",
      onSelect: () => {
        if (!syncMut.isPending) syncMut.mutate();
      },
    },
  ];
  const sub =
    "Deployment-wide LLM config: providers, models, provider-model pricing, capability assignment chains, and the prompt library. Shared by every workspace.";

  if (graphQ.isPending || callsQ.isPending || promptsQ.isPending) {
    return (
      <DeskPage title="LLM & agents" sub={sub} actions={actions} overflow={overflow}>
        <Loading />
      </DeskPage>
    );
  }
  if (!graph || !callsQ.data || !promptsQ.data || !indexes) {
    return (
      <DeskPage title="LLM & agents" sub={sub} actions={actions} overflow={overflow}>
        Failed to load.
      </DeskPage>
    );
  }

  const calls = callsQ.data;
  const prompts = promptsQ.data;
  const syncResult = syncMut.data;

  const unassigned = graph.totals.unassigned_capabilities;

  return (
    <DeskPage title="LLM & agents" sub={sub} actions={actions} overflow={overflow}>
      <section className="grid grid--stats">
        <StatCard
          label="Spend (30d)"
          value={formatMoney(Math.round(graph.totals.spend_usd_30d * 100), "USD")}
          sub={graph.totals.calls_30d + " calls"}
        />
        <StatCard
          label="Providers"
          value={graph.providers.length}
          sub={graph.providers.filter((p) => p.is_enabled).length + " enabled"}
        />
        <StatCard
          label="Models"
          value={graph.models.length}
          sub={graph.models.filter((m) => m.is_active).length + " active"}
        />
        <StatCard
          label="Capabilities"
          value={graph.totals.capability_count}
          sub={unassigned.length ? unassigned.length + " unassigned" : "all assigned"}
        />
      </section>

      {unassigned.length > 0 ? (
        <div className="llm-graph-alert llm-graph-alert--warn">
          <strong>Unassigned capabilities:</strong>{" "}
          {unassigned.map((k) => (
            <code key={k} className="inline-code">
              {k}
            </code>
          ))}
          <div className="llm-graph-alert__sub">
            Assign a provider-model from column 3, or add a capability-inheritance
            edge so this capability falls back to a parent's chain.
          </div>
        </div>
      ) : null}

      {graph.assignment_issues.length > 0 ? (
        <div className="llm-graph-alert llm-graph-alert--error">
          <strong>Missing required capabilities:</strong>{" "}
          {graph.assignment_issues.length} assignment
          {graph.assignment_issues.length === 1 ? "" : "s"} point at a model that
          lacks one of the tags the capability needs. Hover the red rows in the
          Assignments column for details.
        </div>
      ) : null}

      {syncResult ? (
        <div className="llm-graph-alert llm-graph-alert--info">
          <strong>Pricing sync:</strong> {syncResult.updated} updated,{" "}
          {syncResult.skipped} unchanged, {syncResult.errors} errors
          <span className="muted"> — started at {syncResult.started_at}</span>
        </div>
      ) : null}

      <div className="llm-graph" ref={graphRef}>
        <svg
          className="llm-graph__edges"
          width={canvas.w}
          height={canvas.h}
          aria-hidden="true"
        >
          {edges.map((e) => {
            const highlighted = edgeIsHighlighted(e);
            const dim = hasActive && !highlighted;
            const cls = [
              "llm-graph__edge",
              `llm-graph__edge--${e.kind}`,
              highlighted ? "is-linked" : "",
              dim ? "is-dim" : "",
              e.invalid ? "is-error" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return <path key={e.id} className={cls} d={e.d} />;
          })}
        </svg>

        <div className="llm-graph__col-header">
          <span className="llm-graph__col-title">Providers</span>
          <span className="llm-graph__col-count">{graph.providers.length}</span>
        </div>
        <div className="llm-graph__col-header">
          <span className="llm-graph__col-title">Models</span>
          <span className="llm-graph__col-count">{graph.models.length}</span>
        </div>
        <div className="llm-graph__col-header">
          <span className="llm-graph__col-title">Assignments</span>
          <span className="llm-graph__col-count">
            {graph.totals.capability_count}
          </span>
        </div>

        <div className="llm-graph__col">
          {graph.providers.map((p) => (
            <article
              key={p.id}
              ref={setRef(providerRefs)(p.id)}
              className={nodeClass("provider", p.id)}
              onMouseEnter={() => setHover({ column: "provider", id: p.id })}
              onMouseLeave={() => setHover(null)}
              onClick={() =>
                setSelection(
                  selection?.column === "provider" && selection.id === p.id
                    ? null
                    : { column: "provider", id: p.id },
                )
              }
            >
              <header className="llm-graph-node__head">
                <span className="llm-graph-node__name">{p.name}</span>
                <Chip tone={p.is_enabled ? "moss" : "ghost"} size="sm">
                  {p.is_enabled ? "on" : "off"}
                </Chip>
              </header>
              <div className="llm-graph-node__meta">
                <span className="llm-graph-node__type">{p.provider_type}</span>
                <span className="llm-graph-node__endpoint mono">
                  {p.endpoint || "(unset)"}
                </span>
              </div>
              <footer className="llm-graph-node__foot">
                <span>
                  {p.provider_model_count} model
                  {p.provider_model_count === 1 ? "" : "s"}
                </span>
                {p.api_key_status === "missing" ? (
                  <Chip tone="rust" size="sm">
                    no key
                  </Chip>
                ) : p.api_key_status === "rotating" ? (
                  <Chip tone="sand" size="sm">
                    rotating
                  </Chip>
                ) : (
                  <Chip tone="sky" size="sm">
                    key set
                  </Chip>
                )}
              </footer>
            </article>
          ))}
        </div>

        <div className="llm-graph__col">
          {graph.models.map((m) => (
            <article
              key={m.id}
              ref={setRef(modelRefs)(m.id)}
              className={nodeClass("model", m.id)}
              onMouseEnter={() => setHover({ column: "model", id: m.id })}
              onMouseLeave={() => setHover(null)}
              onClick={() =>
                setSelection(
                  selection?.column === "model" && selection.id === m.id
                    ? null
                    : { column: "model", id: m.id },
                )
              }
            >
              <header className="llm-graph-node__head">
                <span className="llm-graph-node__name">{m.display_name}</span>
                <span className="llm-graph-node__vendor">{m.vendor}</span>
              </header>
              <div className="llm-graph-node__meta mono">{m.canonical_name}</div>
              <div className="llm-graph-node__tags">
                {m.capabilities.map((tag) => (
                  <Chip key={tag} tone="ghost" size="sm">
                    {CAPABILITY_TAG_LABEL[tag] ?? tag}
                  </Chip>
                ))}
              </div>
              <footer className="llm-graph-node__foot">
                <span>
                  {m.provider_model_count} provider
                  {m.provider_model_count === 1 ? "" : "s"}
                </span>
                {m.context_window ? (
                  <span className="muted">
                    {(m.context_window / 1000).toFixed(0)}k ctx
                  </span>
                ) : null}
              </footer>
            </article>
          ))}
        </div>

        <div className="llm-graph__col">
          {graph.capabilities.map((cap) => {
            const chain = indexes.assignmentsByCapability.get(cap.key) ?? [];
            const inheritsFrom = indexes.inheritanceByChild.get(cap.key);
            const isUnassigned = chain.length === 0 && !inheritsFrom;
            const isInheriting = chain.length === 0 && inheritsFrom;
            return (
              <article
                key={cap.key}
                className={nodeClass("capability", cap.key)}
                onMouseEnter={() => setHover({ column: "capability", id: cap.key })}
                onMouseLeave={() => setHover(null)}
                onClick={() =>
                  setSelection(
                    selection?.column === "capability" && selection.id === cap.key
                      ? null
                      : { column: "capability", id: cap.key },
                  )
                }
              >
                <header className="llm-graph-node__head">
                  <code className="llm-graph-node__name inline-code">{cap.key}</code>
                  {isUnassigned ? (
                    <Chip tone="rust" size="sm">
                      unassigned
                    </Chip>
                  ) : isInheriting ? (
                    <Chip tone="sand" size="sm">
                      inherits
                    </Chip>
                  ) : (
                    <Chip tone="moss" size="sm">
                      {chain.length} rung{chain.length === 1 ? "" : "s"}
                    </Chip>
                  )}
                </header>
                <div className="llm-graph-node__meta">{cap.description}</div>
                {isInheriting ? (
                  <div className="llm-graph-node__inherits">
                    ↳ falls through to{" "}
                    <code className="inline-code">{inheritsFrom}</code>
                  </div>
                ) : null}
                <ol className="llm-graph-chain">
                  {chain.map((a) => {
                    const pm = indexes.pmById.get(a.provider_model_id);
                    const model = pm ? indexes.modelsById.get(pm.model_id) : null;
                    const provider = pm
                      ? indexes.providersById.get(pm.provider_id)
                      : null;
                    const missing = indexes.issuesByAssignment.get(a.id) ?? [];
                    const rungClass = [
                      "llm-graph-chain__rung",
                      hasActive && !highlighted.assignments.has(a.id) ? "is-dim" : "",
                      missing.length ? "is-error" : "",
                      a.priority === 0 ? "is-primary" : "",
                    ]
                      .filter(Boolean)
                      .join(" ");
                    return (
                      <li
                        key={a.id}
                        ref={setRef(rungRefs)(a.id)}
                        className={rungClass}
                        onMouseEnter={(e) => {
                          e.stopPropagation();
                          setHover({ column: "assignment", id: a.id });
                        }}
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelection({ column: "assignment", id: a.id });
                        }}
                        title={
                          missing.length
                            ? `Missing required capability: ${missing.join(", ")}`
                            : undefined
                        }
                      >
                        <span className="llm-graph-chain__prio">
                          {a.priority === 0 ? "P" : a.priority}
                        </span>
                        <span className="llm-graph-chain__model mono">
                          {model?.canonical_name ?? "(missing model)"}
                        </span>
                        <span className="llm-graph-chain__provider muted">
                          via {provider?.name ?? "?"}
                        </span>
                        <span className="llm-graph-chain__spend mono">
                          {formatMoney(Math.round(a.spend_usd_30d * 100), "USD")}
                        </span>
                      </li>
                    );
                  })}
                </ol>
              </article>
            );
          })}
        </div>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Provider-model pricing</h2>
          <span className="muted">
            From OpenRouter weekly; pinned rows skip the sync.
          </span>
        </header>
        <table className="table">
          <thead>
            <tr>
              <th>Provider × Model</th>
              <th>API model id</th>
              <th>Input / 1M</th>
              <th>Output / 1M</th>
              <th>Last synced</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {graph.provider_models.map((pm) => {
              const provider = indexes.providersById.get(pm.provider_id);
              const model = indexes.modelsById.get(pm.model_id);
              const pinned = pm.price_source_override === "none";
              const free =
                pm.input_cost_per_million === 0 && pm.output_cost_per_million === 0;
              return (
                <tr key={pm.id}>
                  <td>
                    {provider?.name ?? "?"}
                    <span className="muted"> × </span>
                    {model?.display_name ?? "?"}
                  </td>
                  <td className="mono">{pm.api_model_id}</td>
                  <td className="mono">${pm.input_cost_per_million.toFixed(3)}</td>
                  <td className="mono">${pm.output_cost_per_million.toFixed(3)}</td>
                  <td className="mono muted">
                    {pm.price_last_synced_at ? hms(pm.price_last_synced_at) : "—"}
                  </td>
                  <td>
                    {pinned ? (
                      <Chip tone="sand" size="sm">
                        manual
                      </Chip>
                    ) : free ? (
                      <Chip tone="sky" size="sm">
                        free-tier
                      </Chip>
                    ) : (
                      <Chip tone="ghost" size="sm">
                        auto
                      </Chip>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Recent calls</h2>
        </header>
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>Capability</th>
              <th>Model</th>
              <th>Tokens (in / out)</th>
              <th>Cost</th>
              <th>Latency</th>
              <th>Chain</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((c, idx) => (
              <tr key={idx}>
                <td className="mono">{hms(c.at)}</td>
                <td>
                  <code className="inline-code">{c.capability}</code>
                </td>
                <td className="mono muted">{c.model_id}</td>
                <td className="mono">
                  {c.input_tokens} / {c.output_tokens}
                </td>
                <td className="mono">{formatMoney(c.cost_cents, "USD")}</td>
                <td className="mono">{c.latency_ms} ms</td>
                <td className="mono">
                  {c.fallback_attempts && c.fallback_attempts > 0
                    ? `fallback #${c.fallback_attempts}`
                    : "primary"}
                </td>
                <td>
                  <Chip tone={CALL_STATUS_TONE[c.status]} size="sm">
                    {c.status}
                  </Chip>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {promptsOpen ? (
        <div
          className="llm-prompt-drawer-backdrop"
          onClick={() => setPromptsOpen(false)}
          role="presentation"
        >
          <aside
            className="llm-prompt-drawer"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="llm-prompt-drawer__head">
              <h2>Prompt library</h2>
              <button
                className="btn btn--ghost"
                onClick={() => setPromptsOpen(false)}
              >
                Close
              </button>
            </header>
            <p className="llm-prompt-drawer__hint muted">
              Hash-self-seeding: code defaults seed the row; unmodified prompts
              auto-upgrade when code changes; customisations are preserved.
            </p>
            <ul className="llm-prompt-list">
              {prompts.map((p) => (
                <li key={p.id} className="llm-prompt-list__item">
                  <div className="llm-prompt-list__head">
                    <code className="inline-code">{p.capability}</code>
                    <span className="llm-prompt-list__name">{p.name}</span>
                    <span className="llm-prompt-list__ver mono muted">
                      v{p.version}
                    </span>
                    {p.is_customised ? (
                      <Chip tone="sand" size="sm">
                        customised
                      </Chip>
                    ) : (
                      <Chip tone="ghost" size="sm">
                        default
                      </Chip>
                    )}
                  </div>
                  <p className="llm-prompt-list__preview">{p.preview}</p>
                  <footer className="llm-prompt-list__foot muted">
                    <span>{p.revisions_count} revision{p.revisions_count === 1 ? "" : "s"}</span>
                    <span>hash {p.default_hash}</span>
                  </footer>
                </li>
              ))}
            </ul>
          </aside>
        </div>
      ) : null}
    </DeskPage>
  );
}
