import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDate } from "@/lib/dates";
import { Chip, Loading } from "@/components/common";
import AgentApprovalModePanel from "@/components/AgentApprovalModePanel";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import AppearancePanel from "@/components/AppearancePanel";
import AvatarEditor from "@/components/AvatarEditor";
import ChatChannelsMeCard from "@/components/ChatChannelsMeCard";
import PersonalTokensPanel from "@/components/PersonalTokensPanel";
import type { Me } from "@/types/api";

const LANG_LABEL: Record<string, string> = {
  fr: "Français",
  en: "English",
  es: "Español",
  pt: "Português",
};

export default function MePage() {
  const [editorOpen, setEditorOpen] = useState(false);

  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
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
  const langLabel = LANG_LABEL[employee.language] ?? employee.language;

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
        <header className="panel__head"><h2>Bookings</h2></header>
        <Link to="/bookings" className="stack-row">
          <div>
            <strong>My bookings</strong>
            <div className="stack-row__sub">
              Booked time is paid time. Tap a row to amend or decline →
            </div>
          </div>
          <Chip tone="ghost" size="sm">Open</Chip>
        </Link>
      </section>

      <section className="panel">
        <header className="panel__head"><h2>Schedule</h2></header>
        <Link to="/schedule" className="stack-row">
          <div>
            <strong>My schedule</strong>
            <div className="stack-row__sub">
              Weekly pattern, leave and one-off day adjustments →
            </div>
          </div>
          <Chip tone="ghost" size="sm">Open</Chip>
        </Link>
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
    </section>
  );
}
