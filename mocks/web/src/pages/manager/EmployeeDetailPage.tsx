import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate, fmtDateTime } from "@/lib/dates";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  Employee,
  Expense,
  ExpenseStatus,
  Leave,
  PaySlip,
  Property,
  Task,
  TaskStatus,
} from "@/types/api";

interface EmployeeDetail {
  subject: Employee;
  subject_tasks: Task[];
  subject_expenses: Expense[];
  subject_leaves: Leave[];
  subject_payslips: PaySlip[];
}

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  skipped: "rust",
};

const EXPENSE_TONE: Record<ExpenseStatus, "sand" | "moss" | "rust" | "sky"> = {
  pending: "sand",
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

export default function EmployeeDetailPage() {
  const { eid = "" } = useParams<{ eid: string }>();
  const detailQ = useQuery({
    queryKey: qk.employee(eid),
    queryFn: () => fetchJson<EmployeeDetail>("/api/v1/employees/" + eid),
    enabled: eid !== "",
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  if (detailQ.isPending || propsQ.isPending) {
    return <DeskPage title="Employee"><Loading /></DeskPage>;
  }
  if (!detailQ.data || !propsQ.data) {
    return <DeskPage title="Employee">Failed to load.</DeskPage>;
  }

  const { subject, subject_tasks, subject_expenses } = detailQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  return (
    <DeskPage
      title={subject.name}
      sub={subject.roles.join(" · ") + " · " + subject.phone}
      actions={
        <>
          <button className="btn btn--ghost">Message</button>
          <button className="btn btn--ghost">Edit roles</button>
        </>
      }
    >
      <nav className="tabs tabs--h">
        <a className="tab-link tab-link--active">Overview</a>
        <a className="tab-link">Shifts</a>
        <a className="tab-link">Payslips</a>
        <a className="tab-link">Leaves</a>
        <a className="tab-link">Capabilities</a>
        <a className="tab-link">Passkeys</a>
      </nav>

      <section className="grid grid--split">
        <div className="panel">
          <header className="panel__head"><h2>Tasks</h2></header>
          <ul className="task-list task-list--desk">
            {subject_tasks.map((t) => {
              const prop = propsById.get(t.property_id);
              return (
                <li key={t.id} className="task-row">
                  <span className="task-row__time table__mono">
                    {fmtDateTime(t.scheduled_start)}
                  </span>
                  <span className="task-row__title">
                    <strong>{t.title}</strong>
                    <span className="task-row__area">{t.area}</span>
                  </span>
                  {prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}
                  <Chip tone={STATUS_TONE[t.status]} size="sm">{t.status}</Chip>
                </li>
              );
            })}
          </ul>
        </div>

        <div className="panel">
          <header className="panel__head"><h2>Recent expenses</h2></header>
          <ul className="expense-list">
            {subject_expenses.map((x) => (
              <li key={x.id} className="expense-row">
                <div className="expense-row__main">
                  <strong>{x.merchant}</strong>
                  <span className="expense-row__note">{x.note}</span>
                  <span className="expense-row__time">{fmtDate(x.submitted_at)}</span>
                </div>
                <div className="expense-row__side">
                  <span className="expense-row__amount">{formatMoney(x.amount_cents, x.currency)}</span>
                  <Chip tone={EXPENSE_TONE[x.status]} size="sm">{x.status}</Chip>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </DeskPage>
  );
}
