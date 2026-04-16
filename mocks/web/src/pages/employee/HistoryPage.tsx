import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate, fmtDateTime } from "@/lib/dates";
import { Loading } from "@/components/common";
import type { HistoryPayload, Property } from "@/types/api";

type Tab = "tasks" | "chats" | "expenses" | "leaves";

const TABS: [Tab, string][] = [
  ["tasks", "Tasks"],
  ["chats", "Chats"],
  ["expenses", "Expenses"],
  ["leaves", "Leaves"],
];

function isTab(v: string): v is Tab {
  return v === "tasks" || v === "chats" || v === "expenses" || v === "leaves";
}

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export default function HistoryPage() {
  const [params] = useSearchParams();
  const raw = params.get("tab") ?? "tasks";
  const tab: Tab = isTab(raw) ? raw : "tasks";

  const q = useQuery({
    queryKey: qk.history(tab),
    queryFn: () => fetchJson<HistoryPayload>("/api/v1/history?tab=" + tab),
  });

  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const propsById = new Map((propsQ.data ?? []).map((p) => [p.id, p]));

  return (
    <section className="phone__section">
      <Link to="/me" className="back-link">← Back to Me</Link>
      <h2 className="section-title">History</h2>
      <p className="muted">
        Everything already wrapped up — tasks, chats, expenses and leaves.
      </p>

      <nav className="tabs" aria-label="History tabs">
        {TABS.map(([key, label]) => (
          <Link
            key={key}
            to={"/history?tab=" + key}
            className={"tab-link" + (tab === key ? " tab-link--active" : "")}
          >
            {label}
          </Link>
        ))}
      </nav>

      {q.isPending ? (
        <Loading />
      ) : q.isError || !q.data ? (
        <p className="muted">Failed to load.</p>
      ) : tab === "tasks" ? (
        <ul className="task-list">
          {q.data.tasks.length === 0 ? (
            <li className="empty-state empty-state--quiet">No past tasks.</li>
          ) : (
            q.data.tasks.map((t) => {
              const prop = propsById.get(t.property_id);
              return (
                <li key={t.id} className="stack-row">
                  <div>
                    <strong>{t.title}</strong>
                    <div className="stack-row__sub">
                      {prop ? prop.name : t.property_id} · {fmtDateTime(t.scheduled_start)}
                    </div>
                  </div>
                  <span
                    className={
                      "chip chip--sm chip--" +
                      (t.status === "completed" ? "moss" : "rust")
                    }
                  >
                    {cap(t.status)}
                  </span>
                </li>
              );
            })
          )}
        </ul>
      ) : tab === "chats" ? (
        <ul className="task-list">
          {q.data.chats.length === 0 ? (
            <li className="empty-state empty-state--quiet">No archived chats.</li>
          ) : (
            q.data.chats.map((c) => (
              <li key={c.id} className="stack-row">
                <div>
                  <strong>{c.title}</strong>
                  <div className="stack-row__sub">{c.summary}</div>
                </div>
                <span className="chip chip--sm chip--ghost">{c.last_at}</span>
              </li>
            ))
          )}
        </ul>
      ) : tab === "expenses" ? (
        <ul className="task-list">
          {q.data.expenses.length === 0 ? (
            <li className="empty-state empty-state--quiet">No past expenses.</li>
          ) : (
            q.data.expenses.map((x) => (
              <li key={x.id} className="stack-row">
                <div>
                  <strong>
                    {x.merchant} · {formatMoney(x.amount_cents, x.currency)}
                  </strong>
                  <div className="stack-row__sub">
                    {fmtDate(x.submitted_at)} · {x.note}
                  </div>
                </div>
                <span
                  className={
                    "chip chip--sm chip--" +
                    (x.status === "reimbursed" ? "moss" : "sky")
                  }
                >
                  {cap(x.status)}
                </span>
              </li>
            ))
          )}
        </ul>
      ) : (
        <ul className="task-list">
          {q.data.leaves.length === 0 ? (
            <li className="empty-state empty-state--quiet">No past leaves.</li>
          ) : (
            q.data.leaves.map((lv) => (
              <li key={lv.id} className="stack-row">
                <div>
                  <strong>
                    {fmtDate(lv.starts_on)} → {fmtDate(lv.ends_on)}
                  </strong>
                  <div className="stack-row__sub">
                    {cap(lv.category)} · {lv.note}
                  </div>
                </div>
                <span className="chip chip--sm chip--moss">Approved</span>
              </li>
            ))
          )}
        </ul>
      )}
    </section>
  );
}
