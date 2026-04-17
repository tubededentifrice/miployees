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
// The mobile bar (hamburger + agent toggle) appears below the CSS
// `720px` breakpoint. Opening either drawer sets a local boolean that
// toggles the relevant `data-*` attribute / prop; navigating away
// closes both.

const NAV_ITEMS: SideNavItem[] = [
  { type: "link", to: "/dashboard", label: "Dashboard" },
  { type: "section", label: "OPERATE" },
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
        <button
          type="button"
          className="desk__icon-btn desk__icon-btn--badge"
          onClick={() => setAgentOpen((v) => !v)}
          aria-label={agentOpen ? "Close agent" : "Open agent"}
          aria-expanded={agentOpen}
        >
          <svg viewBox="0 0 24 24" width={20} height={20} fill="none" stroke="currentColor"
               strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z" />
          </svg>
          <span className="desk__icon-btn__dot" aria-hidden="true" />
        </button>
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
      <AgentSidebar mobileOpen={agentOpen} onMobileClose={() => setAgentOpen(false)} />
    </div>
  );
}
