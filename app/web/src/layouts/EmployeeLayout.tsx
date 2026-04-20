import { useCallback, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, Euro, ListTodo, UserCircle } from "lucide-react";
import AgentSidebar from "@/components/AgentSidebar";
import BottomTabs from "@/components/BottomTabs";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import {
  initialAgentCollapsed,
  initialNavCollapsed,
  persistNavCollapsed,
} from "@/lib/preferences";
import type { Booking, Me } from "@/types/api";

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1).replace(/_/g, " ");
}

// Phone-frame layout. Body (<Outlet />) + a fixed bottom tab bar that
// includes the Chat button (mobile entry to the agent). The v0 clock-in
// dock is gone — under §09's booking model, the booking IS the time
// record (no clock-in / clock-out tap). The dock now renders the
// "next booking" hint and a one-tap shortcut to /schedule; the
// drawer on that day opens straight to the booking row (§14).
//
// At tablet / desktop widths (>=720px) the phone becomes a three-column
// grid: shared <SideNav /> on the left (Chat is removed from its items
// — the agent lives on the right), the page <Outlet /> in the middle,
// and the shared <AgentSidebar /> on the right.

const ICON_SIZE = 16;
const ICON_STROKE = 1.75;
const NAV_ICON = (Icon: typeof ListTodo) => (
  <Icon size={ICON_SIZE} strokeWidth={ICON_STROKE} />
);

const NAV_ITEMS: SideNavItem[] = [
  { type: "link", to: "/today", matchPrefix: ["/today", "/task/"], label: "Today", phoneHidden: true, icon: NAV_ICON(ListTodo) },
  { type: "link", to: "/schedule", label: "Schedule", phoneHidden: true, icon: NAV_ICON(CalendarDays) },
  { type: "link", to: "/my/expenses", label: "Expenses", phoneHidden: true, icon: NAV_ICON(Euro) },
  { type: "link", to: "/me", matchPrefix: ["/me", "/history"], label: "Me", phoneHidden: true, icon: NAV_ICON(UserCircle) },
];

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p.charAt(0).toUpperCase()).join("") || "·";
}

function fmtBookingHint(b: Booking | undefined): string {
  if (!b) return "No bookings today";
  const start = new Date(b.scheduled_start);
  const end = new Date(b.scheduled_end);
  const t = (d: Date) =>
    `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `Next booking · ${t(start)}–${t(end)}`;
}

export default function EmployeeLayout() {
  const { pathname } = useLocation();
  const isChat = pathname === "/chat";
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const bookingsQ = useQuery({
    queryKey: qk.bookings(),
    queryFn: () => fetchJson<Booking[]>("/api/v1/bookings"),
  });
  const collapsed = initialAgentCollapsed();
  const [navCollapsed, setNavCollapsed] = useState<boolean>(() => initialNavCollapsed());
  const toggleNavCollapsed = useCallback(() => {
    setNavCollapsed((c) => {
      const next = !c;
      persistNavCollapsed(next ? "collapsed" : "open");
      return next;
    });
  }, []);

  const myEmpId = data?.employee.id;
  const now = Date.now();
  const myNext = bookingsQ.data
    ?.filter((b) => b.employee_id === myEmpId && b.status === "scheduled")
    .filter((b) => new Date(b.scheduled_end).getTime() >= now)
    .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start))[0];

  const footerName = data?.employee.name ?? "…";
  const footerRole = data?.employee.roles[0] ? roleLabel(data.employee.roles[0]) : "Employee";
  const footerInitials = data?.employee.avatar_initials
    ?? (data ? initialsOf(data.employee.name) : "·");

  const bookingHint = (
    <NavLink to="/schedule" className="booking-hint">
      {fmtBookingHint(myNext)}
    </NavLink>
  );

  return (
    <main
      className={"phone" + (isChat ? " phone--chat" : "")}
      data-agent-collapsed={collapsed ? "true" : "false"}
      data-nav-collapsed={navCollapsed ? "true" : "false"}
    >
      <SideNav
        items={NAV_ITEMS}
        collapsed={navCollapsed}
        onToggleCollapsed={toggleNavCollapsed}
        action={bookingHint}
        footer={{
          initials: footerInitials,
          avatarUrl: data?.employee.avatar_url ?? null,
          name: footerName,
          role: footerRole,
        }}
      />

      <div className="phone__body">
        <Outlet />
      </div>

      {!isChat && <div className="phone__dock">{bookingHint}</div>}

      <BottomTabs />

      {/* Sibling of <Outlet />. Do not nest. The CSS hides this rail
          below the desktop breakpoint; on phones, the bottom Chat tab
          navigates to /chat full-screen instead. */}
      <AgentSidebar role="employee" />
    </main>
  );
}
