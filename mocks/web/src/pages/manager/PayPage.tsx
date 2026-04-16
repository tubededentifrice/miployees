import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading, StatCard } from "@/components/common";
import type { Employee, PaySlip } from "@/types/api";

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

  const sub = "Periods, payslips, pay rules. Gross only — taxes and social contributions are out of scope (§00, N2).";
  const actions = (
    <>
      <button className="btn btn--ghost">Export CSV</button>
      <button className="btn btn--moss">Close period</button>
    </>
  );

  if (payQ.isPending || employeesQ.isPending) {
    return <DeskPage title="Pay" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!payQ.data || !employeesQ.data) {
    return <DeskPage title="Pay" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const empById = new Map(employeesQ.data.map((e) => [e.id, e]));
  const { current, previous } = payQ.data;
  const defaultCurrency = current[0]?.currency ?? "EUR";

  return (
    <DeskPage title="Pay" sub={sub} actions={actions}>
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
                    {emp && <><Avatar initials={emp.avatar_initials} size="xs" /> {emp.name}</>}
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
        <p className="muted">These payroll actions always require a manager passkey — the agent approval flow cannot bypass them (§11).</p>
        <ul className="danger-list">
          <li><code className="inline-code">payout_destination.create</code> · <code className="inline-code">payout_destination.update</code></li>
          <li><code className="inline-code">employee.set_default_pay_destination</code></li>
          <li>
            <code className="inline-code">POST /payslips/:id/payout_manifest</code>{" "}
            <Chip tone="rust" size="sm">session-only</Chip>
          </li>
        </ul>
      </div>
    </DeskPage>
  );
}
