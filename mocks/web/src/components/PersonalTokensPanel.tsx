import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, KeyRound, Trash2 } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDateTime } from "@/lib/dates";
import { Loading } from "@/components/common";
import TokenRevealPanel from "@/components/TokenRevealPanel";
import type { ApiToken, ApiTokenCreated } from "@/types/api";

// §03 Personal access tokens. Scoped to `me:*` only; subject narrowing
// is anchored on the session, so a maid who mints a token can only
// ever see her own rows through it. The manager /tokens page hides
// these entirely — this is the only surface where they appear.

const ME_SCOPES: { key: string; hint: string; verb: string }[] = [
  { key: "me.tasks:read",     verb: "Read",  hint: "Your assigned tasks and the unassigned tasks you can claim." },
  { key: "me.bookings:read",  verb: "Read",  hint: "Your bookings, amend history, and payslips." },
  { key: "me.expenses:read",  verb: "Read",  hint: "Your expense claims." },
  { key: "me.expenses:write", verb: "Write", hint: "Create or edit your own expense drafts." },
  { key: "me.profile:read",   verb: "Read",  hint: "Your display name, timezone, avatar." },
  { key: "me.profile:write",  verb: "Write", hint: "Update the self-editable fields on your profile." },
];

export default function PersonalTokensPanel() {
  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: qk.meApiTokens(),
    queryFn: () => fetchJson<ApiToken[]>("/api/v1/me/tokens"),
  });

  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("my-script");
  const [picked, setPicked] = useState<Set<string>>(new Set(["me.tasks:read"]));
  const [justCreated, setJustCreated] = useState<ApiTokenCreated | null>(null);

  const createM = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      fetchJson<ApiTokenCreated>("/api/v1/me/tokens", { method: "POST", body }),
    onSuccess: (created) => {
      setJustCreated(created);
      setShowCreate(false);
      qc.invalidateQueries({ queryKey: qk.meApiTokens() });
    },
  });

  const revokeM = useMutation({
    mutationFn: (id: string) =>
      fetchJson<ApiToken>(`/api/v1/me/tokens/${id}/revoke`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.meApiTokens() }),
  });

  const rows = listQ.data ?? [];
  const live = rows.filter((t) => !t.revoked_at);
  const overCap = live.length >= 5;

  function togglePick(key: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function submitCreate(e: React.FormEvent) {
    e.preventDefault();
    const expires = new Date(Date.now() + 90 * 864e5).toISOString();
    createM.mutate({
      name,
      scopes: Array.from(picked),
      expires_at: expires,
    });
  }

  return (
    <section className="panel">
      <header className="panel__head">
        <div className="panel__head-stack">
          <h2>Personal access tokens</h2>
          <p className="panel__sub">
            For your own small scripts — print your tasks on a home printer, export your shifts
            to a spreadsheet, or log expenses from your phone. Limited to{" "}
            <code className="inline-code">me:*</code> scopes; a personal token can only ever
            reach your own rows.
          </p>
        </div>
        <div>
          <span className={"tokens-meter" + (overCap ? " tokens-meter--warn" : "")}>
            <span className="tokens-meter__value">{live.length}/5</span>
            <span className="tokens-meter__label">active</span>
          </span>
          <button
            type="button"
            className="btn btn--moss btn--sm"
            disabled={overCap}
            onClick={() => {
              setJustCreated(null);
              setShowCreate((v) => !v);
            }}
          >
            + New token
          </button>
        </div>
      </header>

      <div className="panel-stack">
        {justCreated && (
          <TokenRevealPanel
            created={justCreated}
            onDismiss={() => setJustCreated(null)}
            kind="personal"
          />
        )}

        {showCreate && (
          <form className="tokens-form" onSubmit={submitCreate}>
            <div className="tokens-form__section">
              <label className="tokens-form__legend" htmlFor="pat-name">Name</label>
              <input
                id="pat-name"
                type="text"
                className="tokens-name-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="kitchen-printer"
                maxLength={80}
                required
              />
            </div>

            <div className="tokens-form__section">
              <div className="tokens-form__legend">
                Scopes
                <span className="tokens-form__legend-hint">
                  {picked.size} selected — each one only reads/writes your own data
                </span>
              </div>
              <ul className="tokens-scope-list">
                {ME_SCOPES.map((s) => {
                  const on = picked.has(s.key);
                  return (
                    <li key={s.key}>
                      <label
                        className={
                          "tokens-scope-list__item" +
                          (on ? " tokens-scope-list__item--on" : "")
                        }
                      >
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={() => togglePick(s.key)}
                        />
                        <span className="tokens-scope-list__check" aria-hidden="true">
                          <Check size={12} strokeWidth={2.5} />
                        </span>
                        <span className="tokens-scope-list__key">{s.key}</span>
                        <span className="tokens-scope-list__badge">{s.verb}</span>
                        <span className="tokens-scope-list__hint">{s.hint}</span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            </div>

            {createM.isError && (
              <p className="tokens-form__error">
                {(createM.error as Error)?.message ?? "Create failed"}
              </p>
            )}

            <div className="tokens-form__actions">
              <div className="tokens-form__actions-hint">
                Default expiry is 90 days. The plaintext is shown exactly once on the next screen.
              </div>
              <div className="tokens-form__actions-buttons">
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => setShowCreate(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn--moss"
                  disabled={createM.isPending || picked.size === 0}
                >
                  {createM.isPending ? "Creating…" : "Create token"}
                </button>
              </div>
            </div>
          </form>
        )}

        {listQ.isPending ? (
          <Loading />
        ) : rows.length === 0 ? (
          <div className="tokens-empty">
            <span className="tokens-empty__glyph" aria-hidden="true">
              <KeyRound size={20} strokeWidth={1.75} />
            </span>
            <p className="tokens-empty__title">No personal tokens yet</p>
            <p className="tokens-empty__sub">
              Click <strong>+ New token</strong> to mint your first one.
            </p>
          </div>
        ) : (
          <ul className="entry-cards">
            {rows.map((t) => (
              <li
                key={t.id}
                className={"entry-card" + (t.revoked_at ? " entry-card--revoked" : "")}
              >
                <div className="entry-card__head">
                  <span className="entry-card__name">{t.name}</span>
                  {t.scopes.map((s) => (
                    <span key={s} className="tokens-scopes__pill tokens-scopes__pill--me">
                      {s}
                    </span>
                  ))}
                  <div className="entry-card__action">
                    {t.revoked_at ? (
                      <span className="tokens-status tokens-status--revoked">revoked</span>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--sm btn--rust"
                        onClick={() => revokeM.mutate(t.id)}
                      >
                        <Trash2 size={13} strokeWidth={2} /> Revoke
                      </button>
                    )}
                  </div>
                </div>

                <div className="entry-card__prefix">
                  <span className="entry-card__prefix-label">prefix</span>
                  <span>{t.prefix}…</span>
                </div>

                <div className="entry-card__meta">
                  <span>
                    <span className="entry-card__meta-label">Created</span>
                    {fmtDateTime(t.created_at)}
                  </span>
                  <span>
                    <span className="entry-card__meta-label">Last used</span>
                    {t.last_used_at ? fmtDateTime(t.last_used_at) : "never"}
                  </span>
                  {t.expires_at && (
                    <span>
                      <span className="entry-card__meta-label">Expires</span>
                      {fmtDateTime(t.expires_at)}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
