import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useDecideMutation } from "@/lib/useDecideMutation";
import { formatMoney } from "@/lib/money";
import { fmtDateTime } from "@/lib/dates";
import DeskPage from "@/components/DeskPage";
import { Camera } from "lucide-react";
import { Avatar, Chip, Loading, StatCard } from "@/components/common";
import { EXPENSE_STATUS_TONE } from "@/lib/tones";
import type { Employee, Expense, ExpenseStatus } from "@/types/api";

type Decision = "approve" | "reject" | "reimburse";

function sumCents(xs: Expense[]): number {
  return xs.reduce((acc, x) => acc + x.amount_cents, 0);
}

function totalLabel(xs: Expense[]): string {
  if (xs.length === 0) return "0.00 total";
  const cur = xs[0]?.currency ?? "EUR";
  return formatMoney(sumCents(xs), cur) + " total";
}

export default function ExpensesApprovalsPage() {
  const expensesQ = useQuery({
    queryKey: qk.expenses("all"),
    queryFn: () => fetchJson<Expense[]>("/api/v1/expenses"),
  });
  const employeesQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });

  const decide = useDecideMutation<Expense[], Decision>({
    queryKey: qk.expenses("all"),
    endpoint: (id, decision) => "/api/v1/expenses/" + id + "/" + decision,
    applyOptimistic: (prev, id, decision) => {
      const nextStatus: ExpenseStatus =
        decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "reimbursed";
      return prev.map((x) => (x.id === id ? { ...x, status: nextStatus } : x));
    },
  });

  const sub = "Review submitted claims. LLM autofill flags low-confidence fields; approving snaps the exchange rate and attaches to the current open pay period.";
  const overflow = [{ label: "Export CSV", onSelect: () => undefined }];

  if (expensesQ.isPending || employeesQ.isPending) {
    return <DeskPage title="Expense approvals" sub={sub} overflow={overflow}><Loading /></DeskPage>;
  }
  if (!expensesQ.data || !employeesQ.data) {
    return <DeskPage title="Expense approvals" sub={sub} overflow={overflow}>Failed to load.</DeskPage>;
  }

  const empById = new Map(employeesQ.data.map((e) => [e.id, e]));
  const all = expensesQ.data;
  const pending = all.filter((x) => x.status === "submitted");
  const approved = all.filter((x) => x.status === "approved");
  const rejected = all.filter((x) => x.status === "rejected");
  const reimbursed = all.filter((x) => x.status === "reimbursed");

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
            const emp = empById.get(x.employee_id);
            const lowConf = x.ocr_confidence !== null && x.ocr_confidence < 0.95;
            const cls = "approval" + (x.amount_cents >= 10000 ? " approval--medium" : "");
            const category = x.category ?? "other";
            return (
              <li key={x.id} className={cls}>
                <div className="approval__head">
                  {emp && <Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} />}
                  <strong>{emp?.name}</strong>
                  <Chip tone="ghost" size="sm">{x.merchant}</Chip>
                  {x.ocr_confidence !== null ? (
                    <Chip tone={lowConf ? "sand" : "sky"} size="sm">
                      LLM autofill · {Math.round(x.ocr_confidence * 100)}%
                    </Chip>
                  ) : (
                    <Chip tone="ghost" size="sm">manual entry</Chip>
                  )}
                  <span className="approval__time">submitted {fmtDateTime(x.submitted_at)}</span>
                </div>

                <div className="expense-approval__grid">
                  <div className="expense-approval__amount">
                    <span className="expense-approval__value">{formatMoney(x.amount_cents, x.currency)}</span>
                    <span className="expense-approval__currency mono">{x.currency}</span>
                  </div>
                  <div className="expense-approval__body">
                    <p className="expense-approval__note">{x.note}</p>
                    <div className="expense-approval__meta">
                      <span>Category: <strong>{category}</strong></span>
                      <span>· Attaches to <strong>April 2026</strong> pay period</span>
                      {lowConf && <Chip tone="sand" size="sm">review flagged fields</Chip>}
                    </div>
                  </div>
                  <div className="expense-approval__receipt">
                    <div className="receipt-thumb" aria-hidden="true">
                      <Camera size={20} strokeWidth={1.6} />
                    </div>
                    <span className="muted mono">receipt · 1 page</span>
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
            <tr><th>Employee</th><th>Merchant</th><th>Amount</th><th>Submitted</th><th>State</th></tr>
          </thead>
          <tbody>
            {[...approved, ...reimbursed, ...rejected].length === 0 && (
              <tr><td colSpan={5} className="empty-state empty-state--quiet">No decisions yet.</td></tr>
            )}
            {[...approved, ...reimbursed, ...rejected].map((x) => {
              const emp = empById.get(x.employee_id);
              const status = x.status as Exclude<ExpenseStatus, "draft" | "submitted">;
              return (
                <tr key={x.id}>
                  <td>
                    {emp && <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name}</>}
                  </td>
                  <td>{x.merchant}<div className="table__sub">{x.note}</div></td>
                  <td className="mono">{formatMoney(x.amount_cents, x.currency)}</td>
                  <td className="mono">{fmtDateTime(x.submitted_at)}</td>
                  <td><Chip tone={EXPENSE_STATUS_TONE[status]} size="sm">{x.status}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
