import { useState } from "react";
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
  EntitySettingsPayload,
  Expense,
  ExpenseStatus,
  Leave,
  PaySlip,
  Property,
  SettingDefinition,
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

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust" | "sand"> = {
  scheduled: "ghost",
  pending: "ghost",
  in_progress: "sky",
  completed: "moss",
  skipped: "rust",
  cancelled: "rust",
  overdue: "sand",
};

const EXPENSE_TONE: Record<ExpenseStatus, "sand" | "moss" | "rust" | "sky" | "ghost"> = {
  draft: "ghost",
  submitted: "sand",
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

function formatValue(value: unknown): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "--";
  return String(value);
}

function SettingsOverridePanel({
  overrides,
  resolved,
  catalog,
}: {
  overrides: Record<string, unknown>;
  resolved: Record<string, { value: unknown; source: string }>;
  catalog: SettingDefinition[];
}) {
  const employeeScoped = catalog.filter((d) => d.override_scope.includes("E"));

  return (
    <div className="panel">
      <header className="panel__head"><h2>Settings overrides</h2></header>
      <p className="muted">
        Employee-scoped settings. Overridden values take precedence over property and workspace defaults.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Setting</th>
            <th>Effective value</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {employeeScoped.map((def) => {
            const hasOverride = def.key in overrides;
            const res = resolved[def.key];
            return (
              <tr key={def.key}>
                <td title={def.description}>
                  <code className="inline-code">{def.key}</code>
                  <span className="muted setting-label">{def.label}</span>
                </td>
                <td>
                  {hasOverride ? (
                    <strong>{formatValue(res?.value)}</strong>
                  ) : (
                    <span className="muted">{formatValue(res?.value)}</span>
                  )}
                </td>
                <td>
                  {hasOverride ? (
                    <Chip tone="moss" size="sm">overridden</Chip>
                  ) : (
                    <span className="muted">inherited ({res?.source ?? "catalog"})</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

type Tab = "overview" | "settings";

export default function EmployeeDetailPage() {
  const { eid = "" } = useParams<{ eid: string }>();
  const [activeTab, setActiveTab] = useState<Tab>("overview");

  const detailQ = useQuery({
    queryKey: qk.employee(eid),
    queryFn: () => fetchJson<EmployeeDetail>("/api/v1/employees/" + eid),
    enabled: eid !== "",
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const settingsQ = useQuery({
    queryKey: qk.employeeSettings(eid),
    queryFn: () => fetchJson<EntitySettingsPayload>("/api/v1/employees/" + eid + "/settings"),
    enabled: eid !== "" && activeTab === "settings",
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
    enabled: activeTab === "settings",
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
      actions={<button className="btn btn--ghost">Edit roles</button>}
      overflow={[{ label: "Message", onSelect: () => undefined }]}
    >
      <nav className="tabs tabs--h">
        <a
          className={"tab-link" + (activeTab === "overview" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("overview")}
        >
          Overview
        </a>
        <a className="tab-link">Shifts</a>
        <a className="tab-link">Payslips</a>
        <a className="tab-link">Leaves</a>
        <a className="tab-link">Policies</a>
        <a
          className={"tab-link" + (activeTab === "settings" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("settings")}
        >
          Settings
        </a>
        <a className="tab-link">Passkeys</a>
      </nav>

      {activeTab === "overview" && (
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
      )}

      {activeTab === "settings" && (
        <>
          {(settingsQ.isPending || catalogQ.isPending) ? (
            <Loading />
          ) : settingsQ.data && catalogQ.data ? (
            <SettingsOverridePanel
              overrides={settingsQ.data.overrides}
              resolved={settingsQ.data.resolved}
              catalog={catalogQ.data}
            />
          ) : (
            <p>Failed to load settings.</p>
          )}
        </>
      )}
    </DeskPage>
  );
}
