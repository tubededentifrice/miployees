import { useCallback, useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Archive,
  BedDouble,
  Boxes,
  Building2,
  CalendarCheck,
  CalendarClock,
  CalendarDays,
  ClipboardCheck,
  Euro,
  FileText,
  Files,
  Home,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  Palmtree,
  ScrollText,
  Settings,
  Shield,
  ShieldCheck,
  Sunrise,
  UserCircle,
  Users,
  Wallet,
  Webhook,
  Wrench,
} from "lucide-react";
import AgentSidebar from "@/components/AgentSidebar";
import BottomTabs from "@/components/BottomTabs";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { ShellNavProvider } from "@/context/ShellNavContext";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import {
  initialAgentCollapsed,
  initialNavCollapsed,
  persistNavCollapsed,
} from "@/lib/preferences";
import type { Me } from "@/types/api";

// ManagerLayout mounts AgentSidebar as a SIBLING of <Outlet />.
// React Router remounts only the outlet subtree on navigation, so the
// sidebar's chat log scroll position, composer draft, and cached log
// survive route changes. Do NOT wrap the outlet in the sidebar's
// parent (that would couple them), and do NOT put a `key` prop on the
// layout route (that would force a full remount).
//
// At phone widths the same shared <BottomTabs /> the worker shell uses
// hosts the worker-facing routes (Today/Schedule/Chat/Expenses/Me);
// the hamburger drawer holds the rest. MY WORK items are tagged
// `phoneHidden` so they don't duplicate the bottom bar.

const ICON_SIZE = 16;
const ICON_STROKE = 1.75;
const NAV_ICON = (Icon: typeof LayoutDashboard) => (
  <Icon size={ICON_SIZE} strokeWidth={ICON_STROKE} />
);

const BASE_NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "MY WORK", phoneHidden: true },
  { type: "link", to: "/today", matchPrefix: ["/today", "/task/"], label: "My Day", phoneHidden: true, icon: NAV_ICON(Sunrise) },
  { type: "link", to: "/schedule", label: "My Schedule", phoneHidden: true, icon: NAV_ICON(CalendarClock) },
  { type: "link", to: "/my/expenses", matchPrefix: "/my/expenses", label: "My Expenses", phoneHidden: true, icon: NAV_ICON(Euro) },
  { type: "link", to: "/me", matchPrefix: ["/me", "/history"], label: "Me", phoneHidden: true, icon: NAV_ICON(UserCircle) },
  { type: "section", label: "OPERATE" },
  { type: "link", to: "/dashboard", label: "Dashboard", icon: NAV_ICON(LayoutDashboard) },
  { type: "link", to: "/properties", matchPrefix: "/propert", label: "Properties", icon: NAV_ICON(Home) },
  { type: "link", to: "/stays", label: "Stays", icon: NAV_ICON(BedDouble) },
  { type: "link", to: "/employees", matchPrefix: "/employee", label: "Employees", icon: NAV_ICON(Users) },
  { type: "link", to: "/templates", label: "Templates", icon: NAV_ICON(FileText) },
  { type: "link", to: "/schedules", label: "Schedules", icon: NAV_ICON(CalendarCheck) },
  { type: "link", to: "/scheduler", label: "Scheduler", icon: NAV_ICON(CalendarDays) },
  { type: "link", to: "/instructions", matchPrefix: "/instructions", label: "Instructions", icon: NAV_ICON(ListChecks) },
  { type: "link", to: "/inventory", label: "Inventory", icon: NAV_ICON(Boxes) },
  { type: "section", label: "ASSETS" },
  { type: "link", to: "/assets", matchPrefix: ["/assets", "/asset/"], label: "Assets", icon: NAV_ICON(Wrench) },
  { type: "link", to: "/asset_types", label: "Catalog", icon: NAV_ICON(Archive) },
  { type: "link", to: "/documents", label: "Documents", icon: NAV_ICON(Files) },
  { type: "section", label: "DECIDE" },
  { type: "link", to: "/approvals", label: "Approvals", icon: NAV_ICON(ClipboardCheck) },
  { type: "link", to: "/leaves", label: "Leaves", icon: NAV_ICON(Palmtree) },
  { type: "link", to: "/expenses", label: "Expenses", icon: NAV_ICON(Euro) },
  { type: "link", to: "/pay", label: "Pay", icon: NAV_ICON(Wallet) },
  { type: "section", label: "ADMIN" },
  { type: "link", to: "/organizations", matchPrefix: "/organization", label: "Organizations", icon: NAV_ICON(Building2) },
  { type: "link", to: "/permissions", label: "Permissions", icon: NAV_ICON(ShieldCheck) },
  { type: "link", to: "/audit", label: "Audit log", icon: NAV_ICON(ScrollText) },
  { type: "link", to: "/webhooks", label: "Webhooks", icon: NAV_ICON(Webhook) },
  { type: "link", to: "/tokens", label: "API tokens", icon: NAV_ICON(KeyRound) },
  { type: "link", to: "/settings", label: "Settings", icon: NAV_ICON(Settings) },
];

// §14 "Administration link" — rendered only when the caller holds any
// active (scope_kind='deployment') role_grants row. LLM provider +
// capability config lives on /admin/llm (§11), not on the workspace.
const ADMINISTRATION_LINK: SideNavItem = {
  type: "link",
  to: "/admin",
  matchPrefix: "/admin",
  label: "Administration",
  icon: NAV_ICON(Shield),
};

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
  const [navCollapsed, setNavCollapsed] = useState<boolean>(() => initialNavCollapsed());
  const toggleNavCollapsed = useCallback(() => {
    setNavCollapsed((c) => {
      const next = !c;
      persistNavCollapsed(next ? "collapsed" : "open");
      return next;
    });
  }, []);
  const navItems: SideNavItem[] = data?.is_deployment_admin
    ? [...BASE_NAV_ITEMS, ADMINISTRATION_LINK]
    : BASE_NAV_ITEMS;
  const hasDrawer = hasDrawerItems(navItems);
  const toggleNav = useCallback(() => setNavOpen((v) => !v), []);

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
    <ShellNavProvider hasDrawer={hasDrawer} isOpen={navOpen} toggle={toggleNav}>
      <div
        className="desk"
        data-agent-collapsed={collapsed ? "true" : "false"}
        data-nav-collapsed={navCollapsed ? "true" : "false"}
        data-nav-open={navOpen ? "true" : "false"}
      >
        {navOpen && (
          <div
            className="desk__scrim"
            onClick={() => setNavOpen(false)}
            role="presentation"
            aria-hidden="true"
          />
        )}

        <SideNav
          items={navItems}
          collapsed={navCollapsed}
          onToggleCollapsed={toggleNavCollapsed}
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
    </ShellNavProvider>
  );
}
