import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading, StatCard } from "@/components/common";
import type { Employee, PaySlip, PendingReimbursement } from "@/types/api";

interface PayPayload {
  current: PaySlip[];
  previous: PaySlip[];
}

const STATUS_TONE: Record<PaySlip["status"], "sand" | "sky" | "moss" | "rust"> = {
  draft: "sand",
  issued: "sky",
  paid: "moss",
  voided: "rust",
};

function sumGross(xs: PaySlip[]): number {
  return xs.reduce((acc, p) => acc + p.gross_cents, 0);
}
function sumNet(xs: PaySlip[]): number {
  return xs.reduce((acc, p) => acc + p.net_cents, 0);
}

export default function PayPage() {
  const payQ = useQuery({
    queryKey: qk.payslips(),
    queryFn: () => fetchJson<PayPayload>("/api/v1/payslips"),
  });
  const employeesQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  // §09 "Amount owed to the employee" — workspace-wide aggregate of
  // approved-but-not-yet-reimbursed claims. Grouped by owed_currency
  // (the destination's currency, not the claim's). ``by_user`` drives
  // the per-employee breakdown table.
  const pendingQ = useQuery({
    queryKey: qk.expensesPendingReimbursement("all"),
    queryFn: () =>
      fetchJson<PendingReimbursement>("/api/v1/expenses/pending_reimbursement"),
  });

  const sub = "Periods, payslips, pay rules. Gross only — taxes and social contributions are out of scope.";
  const actions = <button className="btn btn--moss">Close period</button>;
  const overflow = [{ label: "Export CSV", onSelect: () => undefined }];

  if (payQ.isPending || employeesQ.isPending) {
    return <DeskPage title="Pay" sub={sub} actions={actions} overflow={overflow}><Loading /></DeskPage>;
  }
  if (!payQ.data || !employeesQ.data) {
    return <DeskPage title="Pay" sub={sub} actions={actions} overflow={overflow}>Failed to load.</DeskPage>;
  }

  const empById = new Map(employeesQ.data.map((e) => [e.id, e]));
  const { current, previous } = payQ.data;
  const defaultCurrency = current[0]?.currency ?? "EUR";
  const pending = pendingQ.data;
  const pendingByUser = pending?.by_user ?? [];
  const pendingTotals = pending?.totals_by_currency ?? [];
  const pendingHeadline =
    pendingTotals.length === 0
      ? formatMoney(0, defaultCurrency)
      : pendingTotals
          .map((t) => formatMoney(t.amount_cents, t.currency))
          .join(" + ");

  return (
    <DeskPage title="Pay" sub={sub} actions={actions} overflow={overflow}>
      <section className="grid grid--stats">
        <StatCard label="Current period" value="April 2026" sub="open · closes 30 Apr" />
        <StatCard label="Drafts" value={current.length} sub="payslips pending issue" />
        <StatCard
          label="April gross (est.)"
          value={formatMoney(sumGross(current), defaultCurrency)}
          sub="before reimbursements"
        />
        <StatCard
          label="Last period"
          value={formatMoney(sumNet(previous), previous[0]?.currency ?? defaultCurrency)}
          sub="March · all paid"
        />
        <StatCard
          label="Pending reimbursements"
          value={pendingHeadline}
          sub={
            pendingByUser.length === 0
              ? "nothing owed right now"
              : `${pendingByUser.length} employee${pendingByUser.length === 1 ? "" : "s"} · destination currency`
          }
        />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>April 2026 — drafts</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Employee</th><th>Hours</th><th>Overtime</th><th>Gross</th>
              <th>Reimbursements</th><th>Net</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {current.map((p) => {
              const emp = empById.get(p.employee_id);
              return (
                <tr key={p.id}>
                  <td>
                    {emp && <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name}</>}
                  </td>
                  <td className="mono">{p.hours} h</td>
                  <td className="mono">{p.overtime} h</td>
                  <td className="mono">{formatMoney(p.gross_cents, p.currency)}</td>
                  <td className="mono">{formatMoney(p.reimbursements_cents, p.currency)}</td>
                  <td className="mono"><strong>{formatMoney(p.net_cents, p.currency)}</strong></td>
                  <td><Chip tone={STATUS_TONE[p.status]} size="sm">{p.status}</Chip></td>
                  <td><button className="btn btn--sm btn--ghost">Preview PDF</button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <div className="panel__head-stack">
            <h2>Pending reimbursements</h2>
            <p className="panel__sub muted">
              Approved expense claims waiting to roll into a payslip.
              Each row shows what the employee is owed in the currency
              of the account the reimbursement will land in.
            </p>
          </div>
        </header>
        {pendingQ.isPending ? (
          <Loading />
        ) : pendingByUser.length === 0 ? (
          <p className="muted">Nothing owed right now — all approved claims are already on a payslip.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Employee</th>
                <th>Claims</th>
                <th>Owed</th>
              </tr>
            </thead>
            <tbody>
              {pendingByUser.map((row) => {
                const emp = empById.get(row.employee_id);
                const claimCount = pending?.claims.filter(
                  (c) => (c.user_id || c.employee_id) === row.user_id,
                ).length ?? 0;
                return (
                  <tr key={row.user_id}>
                    <td>
                      {emp ? (
                        <>
                          <Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name}
                        </>
                      ) : (
                        row.user_id
                      )}
                    </td>
                    <td className="mono">{claimCount}</td>
                    <td className="mono">
                      {row.totals_by_currency.map((t, i) => (
                        <span key={t.currency}>
                          {i > 0 && " + "}
                          <strong>{formatMoney(t.amount_cents, t.currency)}</strong>
                        </span>
                      ))}
                    </td>
                  </tr>
                );
              })}
              <tr className="table__foot">
                <td><strong>Total</strong></td>
                <td className="mono">
                  <strong>{pending?.claims.length ?? 0}</strong>
                </td>
                <td className="mono">
                  <strong>
                    {pendingTotals.map((t, i) => (
                      <span key={t.currency}>
                        {i > 0 && " + "}
                        {formatMoney(t.amount_cents, t.currency)}
                      </span>
                    ))}
                  </strong>
                </td>
              </tr>
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <header className="panel__head"><h2>March 2026 — paid</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>Employee</th><th>Gross</th><th>Reimb.</th><th>Net</th><th>Paid</th>
            </tr>
          </thead>
          <tbody>
            {previous.map((p) => {
              const emp = empById.get(p.employee_id);
              return (
                <tr key={p.id}>
                  <td>{emp?.name}</td>
                  <td className="mono">{formatMoney(p.gross_cents, p.currency)}</td>
                  <td className="mono">{formatMoney(p.reimbursements_cents, p.currency)}</td>
                  <td className="mono"><strong>{formatMoney(p.net_cents, p.currency)}</strong></td>
                  <td><Chip tone="moss" size="sm">paid</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel panel--danger">
        <header className="panel__head"><h2>Always-gated actions</h2></header>
        <p className="muted">These payroll actions always require a manager passkey — the agent approval flow cannot bypass them.</p>
        <ul className="danger-list">
          <li><code className="inline-code">payout_destination.create</code> · <code className="inline-code">payout_destination.update</code></li>
          <li><code className="inline-code">work_engagement.set_default_pay_destination</code></li>
          <li>
            <code className="inline-code">POST /payslips/:id/payout_manifest</code>{" "}
            <Chip tone="rust" size="sm">session-only</Chip>
          </li>
        </ul>
      </div>
    </DeskPage>
  );
}
