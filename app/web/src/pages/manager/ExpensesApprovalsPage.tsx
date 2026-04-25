import { useQuery } from "@tanstack/react-query";
import { qk } from "@/lib/queryKeys";
import { fetchAllExpenseClaims } from "@/lib/expenses";
import { useDecideMutation } from "@/lib/useDecideMutation";
import { formatMoney } from "@/lib/money";
import { fmtDateTime } from "@/lib/dates";
import DeskPage from "@/components/DeskPage";
import { Camera } from "lucide-react";
import { Chip, Loading, StatCard } from "@/components/common";
import { EXPENSE_STATUS_TONE } from "@/lib/tones";
import type { Expense, ExpenseStatus } from "@/types/api";

type Decision = "approve" | "reject" | "reimburse";

function sumCents(xs: Expense[]): number {
  return xs.reduce((acc, x) => acc + x.total_amount_cents, 0);
}

function totalLabel(xs: Expense[]): string {
  if (xs.length === 0) return "0.00 total";
  const cur = xs[0]?.currency ?? "EUR";
  return formatMoney(sumCents(xs), cur) + " total";
}

/**
 * Manager-side expense approvals desk.
 *
 * Reads the workspace-wide queue from `GET /api/v1/expenses` (cd-t6y2).
 * The server returns the cursor-paginated `{data, next_cursor, has_more}`
 * envelope from spec §12; `fetchAllExpenseClaims` walks every page and
 * returns the flattened `Expense[]` so the client-side filter still
 * sees the full set. Per-page driving will land alongside cd-mh4p's
 * pending-reimbursement panel rework.
 *
 * The payload no longer carries a `claimant`/`employee_id` field
 * (engagement → user resolution lives in cd-g6nf). Until that lands
 * the desk shows the bound `work_engagement_id` short-form rather
 * than a name + avatar — surfacing *something* identifiable is
 * better than a blank, and keeps the row expressive enough for a
 * manager to triage. The avatar slot returns once the roster
 * endpoint is wired.
 *
 * Likewise the LLM-autofill confidence chip (`ocr_confidence` on the
 * legacy mock shape) is hidden until cd-95zb surfaces a per-claim
 * extraction confidence on the server payload — guessing locally
 * would make the chip lie.
 */
export default function ExpensesApprovalsPage() {
  const expensesQ = useQuery({
    queryKey: qk.expenses("all"),
    queryFn: () => fetchAllExpenseClaims(),
  });

  const decide = useDecideMutation<Expense[], Decision>({
    queryKey: qk.expenses("all"),
    endpoint: (id, decision) => "/api/v1/expenses/" + id + "/" + decision,
    applyOptimistic: (prev, id, decision) => {
      const nextState: ExpenseStatus =
        decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "reimbursed";
      return prev.map((x) => (x.id === id ? { ...x, state: nextState } : x));
    },
  });

  const sub = "Review submitted claims. LLM autofill flags low-confidence fields; approving snaps the exchange rate and attaches to the current open pay period.";
  const overflow = [{ label: "Export CSV", onSelect: () => undefined }];

  if (expensesQ.isPending) {
    return <DeskPage title="Expense approvals" sub={sub} overflow={overflow}><Loading /></DeskPage>;
  }
  if (!expensesQ.data) {
    return <DeskPage title="Expense approvals" sub={sub} overflow={overflow}>Failed to load.</DeskPage>;
  }

  const all = expensesQ.data;
  const pending = all.filter((x) => x.state === "submitted");
  const approved = all.filter((x) => x.state === "approved");
  const rejected = all.filter((x) => x.state === "rejected");
  const reimbursed = all.filter((x) => x.state === "reimbursed");

  return (
    <DeskPage title="Expense approvals" sub={sub} overflow={overflow}>
      <section className="grid grid--stats">
        <StatCard
          label="Needs decision"
          value={pending.length}
          sub={totalLabel(pending)}
          warn={pending.length > 0}
        />
        <StatCard
          label="Approved (this period)"
          value={approved.length}
          sub={totalLabel(approved) + " · pay out on payslip"}
        />
        <StatCard
          label="Reimbursed"
          value={reimbursed.length}
          sub="paid out via March payslip"
        />
        <StatCard label="Rejected (90d)" value={rejected.length} sub="—" />
      </section>

      <div className="panel">
        <header className="panel__head">
          <h2>Pending · {pending.length}</h2>
          <span className="muted">Primary queue — work top to bottom.</span>
        </header>

        <ul className="approval-list approval-list--wide">
          {pending.length === 0 && (
            <li className="empty-state">Queue empty. All submitted claims have been decided.</li>
          )}
          {pending.map((x) => {
            const cls = "approval" + (x.total_amount_cents >= 10000 ? " approval--medium" : "");
            const category = x.category || "other";
            // `submitted_at` is non-null for any row in the
            // `submitted` filter above, but TS can't narrow off a
            // discriminated state literal — guard inline so the chip
            // shows a sensible fallback if the server ever returns a
            // misaligned row (e.g. a draft slipped past the filter).
            const submittedLabel =
              x.submitted_at !== null ? fmtDateTime(x.submitted_at) : "draft";
            return (
              <li key={x.id} className={cls}>
                <div className="approval__head">
                  <strong>{x.vendor}</strong>
                  <Chip tone="ghost" size="sm">{x.work_engagement_id}</Chip>
                  <span className="approval__time">submitted {submittedLabel}</span>
                </div>

                <div className="expense-approval__grid">
                  <div className="expense-approval__amount">
                    <span className="expense-approval__value">{formatMoney(x.total_amount_cents, x.currency)}</span>
                    <span className="expense-approval__currency mono">{x.currency}</span>
                  </div>
                  <div className="expense-approval__body">
                    <p className="expense-approval__note">{x.note_md}</p>
                    <div className="expense-approval__meta">
                      <span>Category: <strong>{category}</strong></span>
                      <span>· Attaches to <strong>April 2026</strong> pay period</span>
                    </div>
                  </div>
                  <div className="expense-approval__receipt">
                    <div className="receipt-thumb" aria-hidden="true">
                      <Camera size={20} strokeWidth={1.6} />
                    </div>
                    <span className="muted mono">
                      {x.attachments.length === 0
                        ? "no receipt"
                        : x.attachments.length === 1
                          ? "receipt · 1 page"
                          : `${x.attachments.length} receipts`}
                    </span>
                  </div>
                </div>

                <div className="approval__actions">
                  <button
                    className="btn btn--moss"
                    type="button"
                    onClick={() => decide.mutate({ id: x.id, decision: "approve" })}
                  >
                    Approve
                  </button>
                  <button
                    className="btn btn--ghost"
                    type="button"
                    onClick={() => decide.mutate({ id: x.id, decision: "reject" })}
                  >
                    Reject with reason
                  </button>
                  <button className="btn btn--ghost" type="button">Edit fields</button>
                </div>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Recent decisions</h2>
          <span className="muted">History — not actionable.</span>
        </header>
        <table className="table">
          <thead>
            <tr><th>Worker</th><th>Vendor</th><th>Amount</th><th>Submitted</th><th>State</th></tr>
          </thead>
          <tbody>
            {[...approved, ...reimbursed, ...rejected].length === 0 && (
              <tr><td colSpan={5} className="empty-state empty-state--quiet">No decisions yet.</td></tr>
            )}
            {[...approved, ...reimbursed, ...rejected].map((x) => {
              // Already filtered to approved | reimbursed | rejected
              // above, but the cast narrows the literal so the tone
              // map look-up stays type-safe without a non-null
              // fallback branch.
              const state = x.state as Exclude<ExpenseStatus, "draft" | "submitted">;
              const submittedLabel =
                x.submitted_at !== null ? fmtDateTime(x.submitted_at) : "—";
              return (
                <tr key={x.id}>
                  <td className="mono">{x.work_engagement_id}</td>
                  <td>{x.vendor}<div className="table__sub">{x.note_md}</div></td>
                  <td className="mono">{formatMoney(x.total_amount_cents, x.currency)}</td>
                  <td className="mono">{submittedLabel}</td>
                  <td><Chip tone={EXPENSE_STATUS_TONE[state]} size="sm">{x.state}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
