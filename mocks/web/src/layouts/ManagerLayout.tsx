import { useCallback, useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import AgentSidebar from "@/components/AgentSidebar";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { readAgentCollapsedCookie } from "@/lib/preferences";
import type { Me } from "@/types/api";

// ManagerLayout mounts AgentSidebar as a SIBLING of <Outlet />.
// React Router remounts only the outlet subtree on navigation, so the
// sidebar's chat log scroll position, composer draft, and cached log
// survive route changes. Do NOT wrap the outlet in the sidebar's
// parent (that would couple them), and do NOT put a `key` prop on the
// layout route (that would force a full remount).
//
// On mobile the top bar carries the hamburger nav drawer; the agent
// drawer is opened from a bottom dock (a single Chat button) so the
// mobile entry point matches the employee shell's bottom-bar Chat tab.
// Opening either drawer sets a local boolean that toggles the relevant
// `data-*` attribute / prop; navigating away closes both.

const NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "MY WORK" },
  { type: "link", to: "/today", label: "My Day" },
  { type: "link", to: "/week", label: "My Week" },
  { type: "link", to: "/my/expenses", matchPrefix: "/my/expenses", label: "My Expenses" },
  { type: "link", to: "/me", matchPrefix: "/me", label: "Me" },
  { type: "section", label: "OPERATE" },
  { type: "link", to: "/dashboard", label: "Dashboard" },
  { type: "link", to: "/properties", matchPrefix: "/propert", label: "Properties" },
  { type: "link", to: "/stays", label: "Stays" },
  { type: "link", to: "/employees", matchPrefix: "/employee", label: "Employees" },
  { type: "link", to: "/templates", label: "Templates" },
  { type: "link", to: "/schedules", label: "Schedules" },
  { type: "link", to: "/instructions", matchPrefix: "/instructions", label: "Instructions" },
  { type: "link", to: "/inventory", label: "Inventory" },
  { type: "section", label: "ASSETS" },
  { type: "link", to: "/assets", matchPrefix: "/asset", label: "Assets" },
  { type: "link", to: "/asset_types", label: "Catalog" },
  { type: "link", to: "/documents", label: "Documents" },
  { type: "section", label: "DECIDE" },
  { type: "link", to: "/approvals", label: "Approvals" },
  { type: "link", to: "/leaves", label: "Leaves" },
  { type: "link", to: "/expenses", label: "Expenses" },
  { type: "link", to: "/pay", label: "Pay" },
  { type: "section", label: "ADMIN" },
  { type: "link", to: "/permissions", label: "Permissions" },
  { type: "link", to: "/audit", label: "Audit log" },
  { type: "link", to: "/webhooks", label: "Webhooks" },
  { type: "link", to: "/llm", label: "LLM & agents" },
  { type: "link", to: "/settings", label: "Settings" },
];

export default function ManagerLayout() {
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const collapsed = readAgentCollapsedCookie();
  const { pathname } = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);

  useEffect(() => {
    setNavOpen(false);
    setAgentOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen && !agentOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setNavOpen(false);
        setAgentOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen, agentOpen]);

  const closeDrawers = useCallback(() => {
    setNavOpen(false);
    setAgentOpen(false);
  }, []);

  return (
    <div
      className="desk"
      data-agent-collapsed={collapsed ? "true" : "false"}
      data-nav-open={navOpen ? "true" : "false"}
      data-agent-mobile-open={agentOpen ? "true" : "false"}
    >
      <header className="desk__mobile-bar" aria-label="Mobile controls">
        <button
          type="button"
          className="desk__icon-btn"
          onClick={() => setNavOpen((v) => !v)}
          aria-label={navOpen ? "Close menu" : "Open menu"}
          aria-expanded={navOpen}
        >
          <svg viewBox="0 0 24 24" width={20} height={20} fill="none" stroke="currentColor"
               strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <line x1={3} y1={6} x2={21} y2={6} />
            <line x1={3} y1={12} x2={21} y2={12} />
            <line x1={3} y1={18} x2={21} y2={18} />
          </svg>
        </button>
        <div className="desk__brand">
          <span className="desk__logo" aria-hidden="true">◈</span>
          <span className="desk__wordmark">crewday</span>
        </div>
      </header>

      {(navOpen || agentOpen) && (
        <div
          className="desk__scrim"
          onClick={closeDrawers}
          role="presentation"
          aria-hidden="true"
        />
      )}

      <SideNav
        items={NAV_ITEMS}
        footer={{
          initials: "EB",
          name: data?.manager_name ?? "Élodie Bernard",
          role: "Manager",
        }}
      />

      <section className="desk__main">
        <Outlet />
      </section>

      {/* Sibling of <Outlet />. Do not nest. */}
      <AgentSidebar role="manager" mobileOpen={agentOpen} onMobileClose={() => setAgentOpen(false)} />

      <nav className="desk__bottom-dock" aria-label="Mobile actions">
        <button
          type="button"
          className={"tab" + (agentOpen ? " tab--active" : "")}
          onClick={() => setAgentOpen((v) => !v)}
          aria-label={agentOpen ? "Close agent" : "Open agent"}
          aria-expanded={agentOpen}
        >
          <span className="tab__glyph" aria-hidden="true">✦</span>
          <span>Chat</span>
        </button>
      </nav>
    </div>
  );
}
