import { Outlet, useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AgentSidebar from "@/components/AgentSidebar";
import BottomTabs from "@/components/BottomTabs";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { initialAgentCollapsed } from "@/lib/preferences";
import type { Me } from "@/types/api";

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1).replace(/_/g, " ");
}

// Phone-frame layout. Body (<Outlet />) + a bottom dock that hosts the
// clock-in toggle, plus a fixed bottom tab bar that includes the Chat
// button (mobile entry to the agent). On the dedicated /chat route the
// dock is suppressed so the composer can claim the bottom band.
//
// At tablet / desktop widths (>=720px) the phone becomes a three-column
// grid: shared <SideNav /> on the left (Chat is removed from its items
// — the agent lives on the right), the page <Outlet /> in the middle,
// and the shared <AgentSidebar /> on the right (mounted as a SIBLING
// of <Outlet /> so the chat log/draft survive route changes). The
// clock-in button rides in the sidebar's `action` slot at this width;
// the phone-mode dock + bottom tab bar stay in the DOM and are hidden
// by CSS.

// /chat lives on the right-rail AgentSidebar at every non-phone width
// (the rail is always present and user-toggleable above 720px) and on
// the bottom tab bar at phone widths. No side-nav entry needed.
//
// `phoneHidden` removes items from the off-canvas hamburger drawer at
// phone widths because they're already in the bottom tab bar; they
// still render in the desktop side nav. With everything phone-hidden
// the employee shell renders no hamburger / mobile top bar at all.
const NAV_ITEMS: SideNavItem[] = [
  { type: "link", to: "/today", label: "Today", phoneHidden: true },
  { type: "link", to: "/week", label: "Week", phoneHidden: true },
  { type: "link", to: "/my/expenses", label: "Expenses", phoneHidden: true },
  { type: "link", to: "/me", matchPrefix: "/me", label: "Me", phoneHidden: true },
];

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p.charAt(0).toUpperCase()).join("") || "·";
}

export default function EmployeeLayout() {
  const { pathname } = useLocation();
  const isChat = pathname === "/chat";
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const qc = useQueryClient();
  const collapsed = initialAgentCollapsed();

  const toggleShift = useMutation({
    mutationFn: () => fetchJson<Me>("/api/v1/shifts/toggle", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.me() });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  const clockedIn = data?.employee.clocked_in_at;
  const clockedAt = clockedIn
    ? new Date(clockedIn).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;

  const footerName = data?.employee.name ?? "…";
  const footerRole = data?.employee.roles[0] ? roleLabel(data.employee.roles[0]) : "Employee";
  const footerInitials = data?.employee.avatar_initials
    ?? (data ? initialsOf(data.employee.name) : "·");

  const clockButton = (
    <button
      type="button"
      className={"clock-toggle " + (clockedIn ? "clock-toggle--on" : "clock-toggle--off")}
      onClick={() => toggleShift.mutate()}
      disabled={toggleShift.isPending}
    >
      {clockedIn ? `● On shift · ${clockedAt}` : "Clock in"}
    </button>
  );

  return (
    <main
      className={"phone" + (isChat ? " phone--chat" : "")}
      data-agent-collapsed={collapsed ? "true" : "false"}
    >
      <SideNav
        items={NAV_ITEMS}
        action={clockButton}
        footer={{
          initials: footerInitials,
          name: footerName,
          role: footerRole,
        }}
      />

      <div className="phone__body">
        <Outlet />
      </div>

      {!isChat && <div className="phone__dock">{clockButton}</div>}

      <BottomTabs />

      {/* Sibling of <Outlet />. Do not nest. The CSS hides this rail
          below the desktop breakpoint; on phones, the bottom Chat tab
          navigates to /chat full-screen instead. */}
      <AgentSidebar role="employee" />
    </main>
  );
}
