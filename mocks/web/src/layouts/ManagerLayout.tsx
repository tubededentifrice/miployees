import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Menu } from "lucide-react";
import AgentSidebar from "@/components/AgentSidebar";
import BottomTabs from "@/components/BottomTabs";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { initialAgentCollapsed } from "@/lib/preferences";
import type { Me } from "@/types/api";

// ManagerLayout mounts AgentSidebar as a SIBLING of <Outlet />.
// React Router remounts only the outlet subtree on navigation, so the
// sidebar's chat log scroll position, composer draft, and cached log
// survive route changes. Do NOT wrap the outlet in the sidebar's
// parent (that would couple them), and do NOT put a `key` prop on the
// layout route (that would force a full remount).
//
// At phone widths the same shared <BottomTabs /> the worker shell uses
// hosts the worker-facing routes (Today/Week/Chat/Expenses/Me); the
// hamburger drawer holds the rest. MY WORK items are tagged
// `phoneHidden` so they don't duplicate the bottom bar.

const NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "MY WORK", phoneHidden: true },
  { type: "link", to: "/today", label: "My Day", phoneHidden: true },
  { type: "link", to: "/week", label: "My Week", phoneHidden: true },
  { type: "link", to: "/my/expenses", matchPrefix: "/my/expenses", label: "My Expenses", phoneHidden: true },
  { type: "link", to: "/me", matchPrefix: "/me", label: "Me", phoneHidden: true },
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
  { type: "link", to: "/assets", matchPrefix: ["/assets", "/asset/"], label: "Assets" },
  { type: "link", to: "/asset_types", label: "Catalog" },
  { type: "link", to: "/documents", label: "Documents" },
  { type: "section", label: "DECIDE" },
  { type: "link", to: "/approvals", label: "Approvals" },
  { type: "link", to: "/leaves", label: "Leaves" },
  { type: "link", to: "/expenses", label: "Expenses" },
  { type: "link", to: "/pay", label: "Pay" },
  { type: "section", label: "ADMIN" },
  { type: "link", to: "/organizations", matchPrefix: "/organization", label: "Organizations" },
  { type: "link", to: "/permissions", label: "Permissions" },
  { type: "link", to: "/audit", label: "Audit log" },
  { type: "link", to: "/webhooks", label: "Webhooks" },
  { type: "link", to: "/tokens", label: "API tokens" },
  { type: "link", to: "/llm", label: "LLM & agents" },
  { type: "link", to: "/settings", label: "Settings" },
];

// Drawer-bar visibility: only render the hamburger + mobile top bar
// when there's at least one non-`phoneHidden` link to put inside the
// drawer. Today's RBAC is implicit (workers have no manager-only
// items), so the worker shell never shows it; once permissions filter
// NAV_ITEMS this rule lets workers gain a hamburger when they earn
// access to anything beyond MY WORK.
function hasDrawerItems(items: SideNavItem[]): boolean {
  return items.some((it) => it.type === "link" && !it.phoneHidden);
}

export default function ManagerLayout() {
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const collapsed = initialAgentCollapsed();
  const { pathname } = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const showMobileBar = hasDrawerItems(NAV_ITEMS);

  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  return (
    <div
      className="desk"
      data-agent-collapsed={collapsed ? "true" : "false"}
      data-nav-open={navOpen ? "true" : "false"}
      data-mobile-bar={showMobileBar ? "true" : "false"}
    >
      {showMobileBar && (
        <header className="desk__mobile-bar" aria-label="Mobile controls">
          <button
            type="button"
            className="desk__icon-btn"
            onClick={() => setNavOpen((v) => !v)}
            aria-label={navOpen ? "Close menu" : "Open menu"}
            aria-expanded={navOpen}
          >
            <Menu size={20} strokeWidth={2} aria-hidden="true" />
          </button>
          <div className="desk__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crewday</span>
          </div>
        </header>
      )}

      {navOpen && (
        <div
          className="desk__scrim"
          onClick={() => setNavOpen(false)}
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
      <AgentSidebar role="manager" />

      <BottomTabs />
    </div>
  );
}
