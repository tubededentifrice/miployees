import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Loading } from "@/components/common";
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

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dmon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function dmonyr(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

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

  return (
    <>
      <section className="phone__section">
        <div className="profile-card">
          <div className="avatar avatar--xl">{employee.avatar_initials}</div>
          <div>
            <h2 className="profile-card__name">{employee.name}</h2>
            <div className="profile-card__roles">
              {employee.roles.map((r) => (
                <span key={r} className="chip chip--ghost chip--sm">{r}</span>
              ))}
            </div>
            <div className="profile-card__meta">
              Started {dmonyr(employee.started_on)} · {employee.phone}
            </div>
          </div>
        </div>
      </section>

      <section className="phone__section">
        <h2 className="section-title">Shift</h2>
        <Link to="/shifts" className="stack-row">
          <div>
            <strong>
              {employee.clocked_in_at
                ? "On shift since " + hhmm(employee.clocked_in_at)
                : "Not clocked in"}
            </strong>
            <div className="stack-row__sub">View history →</div>
          </div>
          <span
            className={
              "chip chip--sm chip--" + (employee.clocked_in_at ? "moss" : "ghost")
            }
          >
            {employee.clocked_in_at ? "Active" : "Off"}
          </span>
        </Link>
      </section>

      <section className="phone__section">
        <h2 className="section-title">History</h2>
        <Link to="/history" className="stack-row">
          <div>
            <strong>Past tasks, chats, expenses, leaves</strong>
            <div className="stack-row__sub">Browse what's been wrapped up →</div>
          </div>
          <span className="chip chip--ghost chip--sm">View</span>
        </Link>
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
                    {dmon(lv.starts_on)} → {dmon(lv.ends_on)}
                  </strong>
                  <div className="stack-row__sub">
                    {cap(lv.category)} · {lv.note}
                  </div>
                </div>
                <span
                  className={
                    "chip chip--sm chip--" + (lv.approved_at ? "moss" : "sand")
                  }
                >
                  {lv.approved_at ? "Approved" : "Pending"}
                </span>
              </li>
            ))
          )}
        </ul>
        <button className="btn btn--ghost" type="button">+ Request leave</button>
      </section>

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

      <AgentApprovalModePanel variant="phone" />

      <ChatChannelsMeCard me={me.data} />

      <section className="phone__section">
        <AgentPreferencesPanel
          scope="user"
          title="My agent preferences"
          subtitle="Private to you. Written in plain language; sent to your chat agent on every turn."
        />
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
                ? "Tap Clock in at the top of this page"
                : "Clock-in disabled for your account"}
            </div>
          </div>
          <span className={"chip chip--sm chip--" + clockChip}>{employee.clock_mode}</span>
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
    </>
  );
}
