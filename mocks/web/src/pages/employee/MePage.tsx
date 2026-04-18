import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDate, fmtTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import { Chip, Loading } from "@/components/common";
import AgentApprovalModePanel from "@/components/AgentApprovalModePanel";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import ChatChannelsMeCard from "@/components/ChatChannelsMeCard";
import type { Leave, Me } from "@/types/api";

interface LeavesPayload {
  leaves: Leave[];
}

const LANG_LABEL: Record<string, string> = {
  fr: "Français",
  en: "English",
  es: "Español",
  pt: "Português",
};

const CLOCK_CHIP: Record<string, "moss" | "ghost" | "rust"> = {
  auto: "moss",
  manual: "ghost",
  disabled: "rust",
};

const DAYS: [string, string][] = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];

export default function MePage() {
  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });

  const empId = me.data?.employee.id ?? "";

  const leavesQ = useQuery({
    queryKey: qk.employeeLeaves(empId),
    queryFn: () => fetchJson<LeavesPayload>("/api/v1/employees/" + empId + "/leaves"),
    enabled: Boolean(empId),
  });

  if (me.isPending) return <section className="phone__section"><Loading /></section>;
  if (me.isError || !me.data) {
    return <section className="phone__section"><p className="muted">Failed to load.</p></section>;
  }

  const { employee } = me.data;
  const leaves = leavesQ.data?.leaves ?? [];
  const langLabel = LANG_LABEL[employee.language] ?? employee.language;
  const clockChip = CLOCK_CHIP[employee.clock_mode] ?? "ghost";

  const firstName = employee.name.split(" ")[0];
  const todayStr = me.data.today
    ? new Date(me.data.today).toLocaleDateString("en-GB", {
        weekday: "long",
        day: "numeric",
        month: "short",
      })
    : "";

  return (
    <>
      {/* — Identity — */}
      <section className="phone__section phone__section--hero">
        <div className="me-greet">
          <span className="me-greet__hello">Hi, {firstName}</span>
          <span className="me-greet__date">{todayStr}</span>
        </div>
      </section>

      <section className="phone__section">
        <div className="profile-card">
          <div className="avatar avatar--xl">{employee.avatar_initials}</div>
          <div>
            <h2 className="profile-card__name">{employee.name}</h2>
            <div className="profile-card__roles">
              {employee.roles.map((r) => (
                <Chip key={r} tone="ghost" size="sm">{r}</Chip>
              ))}
            </div>
            <div className="profile-card__meta">
              Started{" "}
              {fmtDate(employee.started_on, "en-GB", {
                day: "2-digit",
                month: "short",
                year: "numeric",
              })}{" "}
              · {employee.phone}
            </div>
          </div>
        </div>
      </section>

      {/* — Work & schedule — */}
      <section className="phone__section">
        <h2 className="section-title">Shift</h2>
        <Link to="/shifts" className="stack-row">
          <div>
            <strong>
              {employee.clocked_in_at
                ? "On shift since " + fmtTime(employee.clocked_in_at)
                : "Not clocked in"}
            </strong>
            <div className="stack-row__sub">View history →</div>
          </div>
          <Chip tone={employee.clocked_in_at ? "moss" : "ghost"} size="sm">
            {employee.clocked_in_at ? "Active" : "Off"}
          </Chip>
        </Link>
      </section>

      <section className="phone__section">
        <h2 className="section-title">Clock mode</h2>
        <div className="stack-row">
          <div>
            <strong>Clock-in: {employee.clock_mode}</strong>
            <div className="stack-row__sub">
              {employee.clock_mode === "auto"
                ? "Idle after " + employee.auto_clock_idle_minutes + " min"
                : employee.clock_mode === "manual"
                ? "Tap Clock in on the bottom bar (or sidebar on desktop)"
                : "Clock-in disabled for your account"}
            </div>
          </div>
          <Chip tone={clockChip} size="sm">{employee.clock_mode}</Chip>
        </div>
      </section>

      <section className="phone__section">
        <h2 className="section-title">Weekly availability</h2>
        <p className="muted">Read-only. Ask the manager to change these.</p>
        <div className="avail-grid">
          {DAYS.map(([key, label]) => {
            const slot = employee.weekly_availability[key];
            if (slot) {
              return (
                <div key={key} className="avail-cell">
                  <span className="avail-cell__day">{label}</span>
                  <span className="avail-cell__hours">{slot[0]}</span>
                  <span className="avail-cell__hours">{slot[1]}</span>
                </div>
              );
            }
            return (
              <div key={key} className="avail-cell avail-cell--off">
                <span className="avail-cell__day">{label}</span>
                <span className="avail-cell__hours">Off</span>
              </div>
            );
          })}
        </div>
      </section>

      <section className="phone__section">
        <h2 className="section-title">My leave</h2>
        <ul className="task-list">
          {leaves.length === 0 ? (
            <li className="empty-state empty-state--quiet">No leave on file.</li>
          ) : (
            leaves.map((lv) => (
              <li key={lv.id} className="stack-row">
                <div>
                  <strong>
                    {fmtDate(lv.starts_on)} → {fmtDate(lv.ends_on)}
                  </strong>
                  <div className="stack-row__sub">
                    {cap(lv.category)} · {lv.note}
                  </div>
                </div>
                <Chip tone={lv.approved_at ? "moss" : "sand"} size="sm">
                  {lv.approved_at ? "Approved" : "Pending"}
                </Chip>
              </li>
            ))
          )}
        </ul>
        <button className="btn btn--ghost" type="button">+ Request leave</button>
      </section>

      {/* — Agent — */}
      <AgentApprovalModePanel variant="phone" />

      <AgentPreferencesPanel
        scope="user"
        variant="phone"
        title="My agent preferences"
        subtitle="Private to you. Written in plain language; sent to your chat agent on every turn."
      />

      <ChatChannelsMeCard me={me.data} />

      {/* — Settings — */}
      <section className="phone__section">
        <h2 className="section-title">Language</h2>
        <div className="stack-row">
          <div>
            <strong>{langLabel}</strong>
            <div className="stack-row__sub">
              Used for the agent, digests and reminders.
            </div>
          </div>
          <button type="button" className="btn btn--ghost btn--sm">Change</button>
        </div>
      </section>

      <section className="phone__section">
        <h2 className="section-title">Passkeys</h2>
        <ul className="task-list">
          <li className="stack-row">
            <div>
              <strong>iPhone 14 · Face ID</strong>
              <div className="stack-row__sub">Added 12 Mar 2025 · last used today</div>
            </div>
            <button className="btn btn--ghost btn--sm" type="button">Remove</button>
          </li>
        </ul>
        <button className="btn btn--moss" type="button">+ Register another device</button>
      </section>

      {/* — History (link out) — */}
      <section className="phone__section">
        <h2 className="section-title">History</h2>
        <Link to="/history" className="stack-row">
          <div>
            <strong>Past tasks, chats, expenses, leaves</strong>
            <div className="stack-row__sub">Browse what's been wrapped up →</div>
          </div>
          <Chip tone="ghost" size="sm">View</Chip>
        </Link>
      </section>
    </>
  );
}
