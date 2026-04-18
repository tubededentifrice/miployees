import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import type {
  AgentPreference,
  AgentPreferenceScope,
} from "@/types/api";

// gpt-tokenizer/model/gpt-4o — o200k_base BPE. Gemma uses a different
// SentencePiece tokenizer, but on typical prose (mixed English + French)
// the two agree to within a few percent. Good enough for a UI counter;
// the server's count stays authoritative for hard-cap enforcement.
// Lazy-loaded so the ~2 MB merge table stays out of the main bundle.
let tokenizerLoad: Promise<(s: string) => number> | null = null;
function loadTokenizer(): Promise<(s: string) => number> {
  if (!tokenizerLoad) {
    tokenizerLoad = import("gpt-tokenizer/model/gpt-4o").then(
      (m) => (s: string) => m.encode(s).length,
    );
  }
  return tokenizerLoad;
}

function useTokenCount(text: string): { count: number; ready: boolean } {
  const [tok, setTok] = useState<((s: string) => number) | null>(null);
  useEffect(() => {
    let alive = true;
    void loadTokenizer().then((fn) => { if (alive) setTok(() => fn); });
    return () => { alive = false; };
  }, []);
  const count = useMemo(
    () => (tok ? tok(text) : Math.ceil(text.length / 3.8)),
    [tok, text],
  );
  return { count, ready: tok !== null };
}

// §11 — CLAUDE.md-style free-form guidance stacked into the LLM
// system prompt. Three layers (workspace / property / user); this
// component edits a single layer and is reused by SettingsPage,
// PropertyDetailPage, and the worker "Me" page.
//
// The server still records a revision history on every save (§02);
// it just isn't surfaced in this UI — read it via the CLI or the
// REST endpoint if you need audit context.

type Variant = "panel" | "phone";

interface Props {
  scope: AgentPreferenceScope;
  scopeId?: string;     // omitted for workspace + me
  title: string;
  subtitle: string;
  variant?: Variant;
}

function endpointFor(scope: AgentPreferenceScope, scopeId?: string): string {
  if (scope === "workspace") return "/api/v1/agent_preferences/workspace";
  if (scope === "user") return "/api/v1/agent_preferences/me";
  return `/api/v1/agent_preferences/property/${scopeId}`;
}

function saveKey(scope: AgentPreferenceScope, scopeId?: string) {
  return scope === "property" ? qk.agentPrefs("property", scopeId) :
    scope === "user" ? qk.agentPrefs("me") : qk.agentPrefs("workspace");
}

export default function AgentPreferencesPanel({
  scope, scopeId, title, subtitle, variant = "panel",
}: Props) {
  const qc = useQueryClient();
  const key = saveKey(scope, scopeId);
  const q = useQuery({
    queryKey: key,
    queryFn: () => fetchJson<AgentPreference>(endpointFor(scope, scopeId)),
  });

  const [draft, setDraft] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (q.data) setDraft(q.data.body_md);
  }, [q.data?.updated_at]);  // resync on server-confirmed updates

  const save = useMutation({
    mutationFn: (body_md: string) =>
      fetchJson<AgentPreference>(endpointFor(scope, scopeId), {
        method: "PUT",
        body: { body_md },
      }),
    onSuccess: (next) => {
      qc.setQueryData(key, next);
      setError(null);
    },
    onError: (e: unknown) => {
      if (e instanceof ApiError && e.body && typeof e.body === "object") {
        const body = e.body as { error?: string; pattern?: string; token_count?: number; hard_cap?: number };
        if (body.error === "preference_contains_secret") {
          setError(`Refused to save — matched ${body.pattern}. Remove the secret and try again.`);
        } else if (body.error === "preference_too_large") {
          setError(`Too long (${body.token_count}/${body.hard_cap} tokens).`);
        } else if (body.error === "forbidden") {
          setError("You don't have permission to edit this layer.");
        } else {
          setError("Save failed.");
        }
      } else {
        setError("Save failed.");
      }
    },
  });

  const pref = q.data;
  const { count: draftTokens, ready: tokReady } = useTokenCount(draft);
  const dirty = pref ? draft !== pref.body_md : false;

  const wrapperClass =
    variant === "phone" ? "phone__section agent-prefs" : "panel agent-prefs";
  const headingId = `agent-prefs-${scope}-${scopeId ?? "self"}`;
  const textareaId = `agent-prefs-body-${scope}-${scopeId ?? "self"}`;

  const heading =
    variant === "phone" ? (
      <h2 className="section-title" id={headingId}>{title}</h2>
    ) : (
      <header className="panel__head"><h2 id={headingId}>{title}</h2></header>
    );

  if (q.isPending) {
    return (
      <section className={wrapperClass}>
        {heading}
        <p className="muted">Loading…</p>
      </section>
    );
  }
  if (!pref) {
    return (
      <section className={wrapperClass}>
        {heading}
        <p className="muted">Failed to load preferences.</p>
      </section>
    );
  }

  const softOver = draftTokens > pref.soft_cap;
  const hardOver = draftTokens > pref.hard_cap;
  const counterTone = hardOver ? "rust" : softOver ? "sand" : "moss";

  return (
    <section className={wrapperClass} aria-labelledby={headingId}>
      {heading}
      <p className="muted">{subtitle}</p>

      <div className="agent-prefs__banner" role="note">
        Preferences are <strong>sent to the model as written.</strong> Do not paste
        passwords, door codes, or account numbers — the save endpoint will refuse
        them. Hard rules belong in <em>Settings</em>; this area carries soft
        guidance only.
      </div>

      {pref.writable ? (
        <>
          <label className="agent-prefs__label" htmlFor={textareaId}>
            Guidance (Markdown)
          </label>
          <AutoGrowTextarea
            id={textareaId}
            className="agent-prefs__textarea"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            maxHeight={600}
            spellCheck
          />
          <div className={`agent-prefs__meta agent-prefs__meta--${counterTone}`}>
            <span>
              {draftTokens} / {pref.soft_cap} tokens
              {tokReady ? "" : " (estimate)"}
              {hardOver ? " — over hard cap" : softOver ? " — over soft cap" : ""}
            </span>
            <span className="muted">
              {pref.updated_at
                ? `Last saved ${new Date(pref.updated_at).toLocaleString()}`
                : "Never saved."}
            </span>
          </div>
          <div className="agent-prefs__actions">
            <button
              className="btn btn--moss"
              disabled={!dirty || hardOver || save.isPending}
              onClick={() => save.mutate(draft)}
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
          </div>
          {error && <p className="agent-prefs__error">{error}</p>}
        </>
      ) : (
        <div className="agent-prefs__readonly-notice">
          <p>
            These preferences shape your agent's behaviour on this scope but are
            authored by a manager. The full text is not shown here to keep this
            page from doubling as a browsing surface for casual observers.
          </p>
          <p className="muted">
            Read the raw Markdown via{" "}
            <code className="inline-code">crewday agent-prefs show {scope}
              {scopeId ? " " + scopeId : ""}</code>{" "}
            or <code className="inline-code">GET /api/v1/agent_preferences/
              {scope === "user" ? "me" : scope + (scopeId ? "/" + scopeId : "")}</code>.
            {pref.updated_at && (
              <> Last updated {new Date(pref.updated_at).toLocaleDateString()}
                {" "}({pref.token_count} tokens).</>
            )}
          </p>
        </div>
      )}
    </section>
  );
}
