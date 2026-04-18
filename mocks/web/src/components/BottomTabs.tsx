import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { CalendarDays, Euro, ListTodo, MessageSquareMore, User } from "lucide-react";

// Phone-only bottom navigation, shared by EmployeeLayout and
// ManagerLayout so both shells get the same row of 5 worker-style
// buttons (Today, Schedule, Chat, Expenses, Me). The bar is hidden at
// desktop widths via `.phone__tabs` in globals.css.
//
// MY WORK items in either side nav are intentionally hidden from the
// phone hamburger drawer because they duplicate these buttons.
//
// `glyph` accepts a Unicode character or a React node (e.g. a
// lucide-react icon). Unicode is sized via `.tab__glyph`'s
// `font-size`; React nodes are expected to size themselves.

interface TabDef {
  to: string;
  glyph: ReactNode;
  label: string;
  matchPrefix?: string;
}

const TABS: TabDef[] = [
  { to: "/today", glyph: <ListTodo size={18} strokeWidth={1.8} />, label: "Today" },
  { to: "/schedule", glyph: <CalendarDays size={18} strokeWidth={1.8} />, label: "Schedule" },
  { to: "/chat", glyph: <MessageSquareMore size={18} strokeWidth={1.8} />, label: "Chat" },
  { to: "/my/expenses", glyph: <Euro size={18} strokeWidth={1.8} />, label: "Expenses", matchPrefix: "/my/expenses" },
  { to: "/me", glyph: <User size={18} strokeWidth={1.8} />, label: "Me", matchPrefix: "/me" },
];

const ME_PATHS = new Set(["/me", "/shifts", "/history"]);

export default function BottomTabs() {
  const { pathname } = useLocation();
  return (
    <nav className="phone__tabs" aria-label="Bottom navigation">
      {TABS.map((t) => {
        const active =
          t.to === "/me"
            ? ME_PATHS.has(pathname)
            : t.matchPrefix
              ? pathname.startsWith(t.matchPrefix)
              : pathname === t.to;
        return (
          <NavLink
            key={t.to}
            to={t.to}
            className={"tab" + (active ? " tab--active" : "")}
          >
            <span className="tab__glyph" aria-hidden="true">{t.glyph}</span>
            <span>{t.label}</span>
          </NavLink>
        );
      })}
    </nav>
  );
}
