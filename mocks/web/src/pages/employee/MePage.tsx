import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDate, fmtTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import { Chip, Loading } from "@/components/common";
import AgentApprovalModePanel from "@/components/AgentApprovalModePanel";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import AppearancePanel from "@/components/AppearancePanel";
import AvatarEditor from "@/components/AvatarEditor";
import ChatChannelsMeCard from "@/components/ChatChannelsMeCard";
import PersonalTokensPanel from "@/components/PersonalTokensPanel";
import { LeaveDialog, OverrideDialog } from "@/components/ScheduleDialogs";
import type { AvailabilityOverride, Leave, Me } from "@/types/api";

interface LeavesPayload {
  leaves: Leave[];
}

interface OverridesPayload {
  overrides: AvailabilityOverride[];
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
  const [editorOpen, setEditorOpen] = useState(false);
  // `leaveIso` / `overrideIso` null = dialog closed; an ISO string
  // opens the corresponding shared dialog with that date pre-filled
  // (today when opened from the panel actions). The same writers
  // /schedule uses — same approval semantics, same invalidation set.
  const [leaveIso, setLeaveIso] = useState<string | null>(null);
  const [overrideIso, setOverrideIso] = useState<string | null>(null);

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

  const overridesQ = useQuery({
    queryKey: qk.meOverrides(),
    queryFn: () => fetchJson<OverridesPayload>("/api/v1/me/availability_overrides"),
  });

  if (me.isPending) {
    return (
      <section className="me-page"><Loading /></section>
    );
  }
  if (me.isError || !me.data) {
    return (
      <section className="me-page"><p className="muted">Failed to load.</p></section>
    );
  }

  const { employee } = me.data;
  const leaves = leavesQ.data?.leaves ?? [];
  const overrides = overridesQ.data?.overrides ?? [];
  const langLabel = LANG_LABEL[employee.language] ?? employee.language;
  const clockChip = CLOCK_CHIP[employee.clock_mode] ?? "ghost";
  const todayIso = new Date().toISOString().slice(0, 10);
  const weeklyPatternByDay: Record<string, number> = {
    mon: 0, tue: 1, wed: 2, thu: 3, fri: 4, sat: 5, sun: 6,
  };

  return (
    <section className="me-page">
      <section className="panel">
        <div className="profile-card">
          <button
            type="button"
            className="avatar-trigger"
            onClick={() => setEditorOpen(true)}
            aria-label="Change profile photo"
          >
            <span className="avatar avatar--xl">
              {employee.avatar_url
                ? <img className="avatar__img" src={employee.avatar_url} alt={employee.name} />
                : employee.avatar_initials}
            </span>
            <span className="avatar-trigger__edit" aria-hidden="true">
              <Pencil size={12} strokeWidth={2.5} />
            </span>
          </button>
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

      <section className="panel">
        <header className="panel__head"><h2>Email</h2></header>
        <div className="stack-row">
          <div>
            <strong>{employee.email}</strong>
            <div className="stack-row__sub">
              Used for magic links (invites, lost-device recovery) and digests. Changing it sends
              a confirmation link to the new address and a 72-hour revert link to this one.
            </div>
          </div>
          <button type="button" className="btn btn--ghost btn--sm">Change</button>
        </div>
      </section>

      <section className="panel">
        <header className="panel__head"><h2>Shift</h2></header>
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

      <section className="panel">
        <header className="panel__head"><h2>Clock mode</h2></header>
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

      <section className="panel">
        <header className="panel__head">
          <h2>Weekly availability</h2>
          <Link to="/schedule" className="btn btn--ghost btn--sm">
            Open schedule
          </Link>
        </header>
        <p className="muted">
          Your standing weekly pattern — managers own the pattern itself.
          For a one-off change (a specific day off, a different shift on
          Friday), use <strong>Adjust a day</strong> below or on the{" "}
          <Link to="/schedule">Schedule</Link> page. Extra hours are
          auto-approved; reducing hours needs manager approval (§06).
        </p>
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

      <section className="panel">
        <header className="panel__head">
          <h2>My leave</h2>
          <button
            className="btn btn--moss btn--sm"
            type="button"
            onClick={() => setLeaveIso(todayIso)}
          >
            + Request leave
          </button>
        </header>
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
                    {cap(lv.category)}{lv.note ? ` · ${lv.note}` : ""}
                  </div>
                </div>
                <Chip tone={lv.approved_at ? "moss" : "sand"} size="sm">
                  {lv.approved_at ? "Approved" : "Pending"}
                </Chip>
              </li>
            ))
          )}
        </ul>
      </section>

      <section className="panel">
        <header className="panel__head">
          <h2>My availability overrides</h2>
          <button
            className="btn btn--moss btn--sm"
            type="button"
            onClick={() => setOverrideIso(todayIso)}
          >
            + Adjust a day
          </button>
        </header>
        <p className="muted">
          One-off changes to your working hours for a specific date. Also
          editable per-day from <Link to="/schedule">Schedule</Link>.
        </p>
        <ul className="task-list">
          {overrides.length === 0 ? (
            <li className="empty-state empty-state--quiet">No overrides on file.</li>
          ) : (
            overrides.map((ao) => (
              <li key={ao.id} className="stack-row">
                <div>
                  <strong>{fmtDate(ao.date)}</strong>
                  <div className="stack-row__sub">
                    {ao.available
                      ? ao.starts_local && ao.ends_local
                        ? `${ao.starts_local}–${ao.ends_local}`
                        : "Working (pattern hours)"
                      : "Off"}
                    {ao.reason ? ` · ${ao.reason}` : ""}
                  </div>
                </div>
                <Chip tone={ao.approved_at ? "moss" : "sand"} size="sm">
                  {ao.approved_at ? "Approved" : "Pending"}
                </Chip>
              </li>
            ))
          )}
        </ul>
      </section>

      <AppearancePanel />

      <AgentApprovalModePanel />

      <AgentPreferencesPanel
        scope="user"
        title="My agent preferences"
        subtitle="Private to you. Written in plain language; sent to your chat agent on every turn."
      />

      <ChatChannelsMeCard me={me.data} />

      <section className="panel">
        <header className="panel__head"><h2>Language</h2></header>
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

      <PersonalTokensPanel />

      <section className="panel">
        <header className="panel__head">
          <div className="panel__head-stack">
            <h2>Passkeys</h2>
            <p className="panel__sub">
              Devices you've registered to sign in. Remove any you no longer trust —
              re-enrolling on a new device revokes the rest automatically.
            </p>
          </div>
          <button className="btn btn--moss btn--sm" type="button">+ Register another device</button>
        </header>
        <ul className="entry-cards">
          <li className="entry-card">
            <div className="entry-card__head">
              <span className="entry-card__name">iPhone 14 · Face ID</span>
              <Chip tone="moss" size="sm">active</Chip>
              <div className="entry-card__action">
                <button type="button" className="btn btn--sm btn--ghost">Remove</button>
              </div>
            </div>
            <div className="entry-card__meta">
              <span>
                <span className="entry-card__meta-label">Added</span>
                12 Mar 2025
              </span>
              <span>
                <span className="entry-card__meta-label">Last used</span>
                today
              </span>
            </div>
          </li>
        </ul>
      </section>

      <section className="panel">
        <header className="panel__head"><h2>History</h2></header>
        <Link to="/history" className="stack-row">
          <div>
            <strong>Past tasks, chats, expenses, leaves</strong>
            <div className="stack-row__sub">Browse what's been wrapped up →</div>
          </div>
          <Chip tone="ghost" size="sm">View</Chip>
        </Link>
      </section>

      <AvatarEditor
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        currentUrl={employee.avatar_url}
        userName={employee.name}
      />

      <LeaveDialog
        iso={leaveIso}
        employeeId={empId || null}
        onClose={() => setLeaveIso(null)}
      />
      <OverrideDialog
        iso={overrideIso}
        employeeId={empId || null}
        pattern={
          overrideIso
            ? (() => {
                // Translate the MePage weekly_availability dict into the
                // SelfWeeklyAvailabilitySlot shape the dialog expects.
                const wd = (new Date(overrideIso).getDay() + 6) % 7;
                const key = Object.entries(weeklyPatternByDay)
                  .find(([, idx]) => idx === wd)?.[0];
                const slot = key ? employee.weekly_availability[key] : null;
                return slot
                  ? { weekday: wd, starts_local: slot[0], ends_local: slot[1] }
                  : { weekday: wd, starts_local: null, ends_local: null };
              })()
            : null
        }
        onClose={() => setOverrideIso(null)}
      />
    </section>
  );
}
