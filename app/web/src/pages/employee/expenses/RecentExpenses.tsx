import { useQuery } from "@tanstack/react-query";
import { qk } from "@/lib/queryKeys";
import { fetchAllExpenseClaims } from "@/lib/expenses";
import { formatMoney } from "@/lib/money";
import { fmtDate } from "@/lib/dates";
import { Loading } from "@/components/common";
import { STATUS_TONE } from "./lib/expenseHelpers";

// "My recent expenses" — always visible below the form so the worker
// can see what the past week's claims look like (and their status)
// without leaving the page. Pending / approved / reimbursed all flow
// through the same list; the chip tone (`STATUS_TONE`) is the only
// thing that distinguishes them.
//
// Reads `GET /api/v1/expenses` with no `user_id` — the cd-t6y2 service
// defaults to the caller's own claims (per
// `app.domain.expenses.claims.list_for_user`). The legacy
// `?mine=true` query string was a mock-era artefact; the production
// endpoint never honoured it, and FastAPI silently ignored the param.
// Tracked separately under cd-qcj2 if "explicit self-list" semantics
// ever resurface.

export default function RecentExpenses() {
  const q = useQuery({
    queryKey: qk.expenses("mine"),
    queryFn: () => fetchAllExpenseClaims(),
  });

  return (
    <section className="phone__section">
      <h2 className="section-title">My recent expenses</h2>
      {q.isPending ? (
        <Loading />
      ) : q.isError || !q.data ? (
        <p className="muted">Failed to load.</p>
      ) : (
        <ul className="expense-list">
          {q.data.map((x) => {
            // Drafts have no `submitted_at`; fall back to the
            // purchase date so the row always anchors to a moment.
            const stamp = x.submitted_at ?? x.purchased_at;
            return (
              <li key={x.id} className="expense-row">
                <div className="expense-row__main">
                  <strong>{x.vendor}</strong>
                  <span className="expense-row__note">{x.note_md}</span>
                  <span className="expense-row__time">
                    {fmtDate(stamp)}
                  </span>
                </div>
                <div className="expense-row__side">
                  <span className="expense-row__amount">
                    {formatMoney(x.total_amount_cents, x.currency)}
                  </span>
                  <span className={"chip chip--sm chip--" + STATUS_TONE[x.state]}>
                    {x.state}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
