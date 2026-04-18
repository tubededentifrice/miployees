// Cookie-backed preferences. Reads are synchronous from document.cookie
// so they can hydrate initial state without waiting for /api/v1/me;
// writes go to FastAPI (which re-sets the cookie) and we update our
// local mirror optimistically so layout doesn't flash.

import type { Role, Theme } from "@/types/api";

const ROLE_COOKIE = "crewday_role";
const THEME_COOKIE = "crewday_theme";
const AGENT_COLLAPSED_COOKIE = "crewday_agent_collapsed";

function readCookie(name: string): string | null {
  const target = name + "=";
  for (const chunk of document.cookie.split(";")) {
    const c = chunk.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

export function readRoleCookie(): Role {
  const r = readCookie(ROLE_COOKIE);
  return r === "manager" ? "manager" : "employee";
}

export function readThemeCookie(): Theme {
  const t = readCookie(THEME_COOKIE);
  if (t === "dark" || t === "light" || t === "system") return t;
  return "system";
}

// Tri-state: explicit "collapsed" / "open" / no-preference. The server
// writes "1" or "0" depending on the user's last toggle; missing means
// the user has never expressed a preference and we fall back to a
// viewport-driven default (see `initialAgentCollapsed`).
export function readAgentCollapsedCookie(): boolean | null {
  const v = readCookie(AGENT_COLLAPSED_COOKIE);
  if (v === "1") return true;
  if (v === "0") return false;
  return null;
}

// Viewport-driven default for users who haven't toggled the rail yet.
// At wide desktops (≥ AGENT_DEFAULT_OPEN_AT) the rail starts open; on
// laptop / tablet widths it starts collapsed so the main column has
// room. Phone (≤720) is handled by the off-canvas drawer and ignores
// this default entirely.
const AGENT_DEFAULT_OPEN_AT = 1600;
function defaultAgentCollapsed(): boolean {
  if (typeof window === "undefined") return true;
  return window.innerWidth < AGENT_DEFAULT_OPEN_AT;
}

export function initialAgentCollapsed(): boolean {
  const pref = readAgentCollapsedCookie();
  return pref !== null ? pref : defaultAgentCollapsed();
}

// Fire-and-forget writers. The server is authoritative; we optimistic
// -mirror so the next paint reflects the choice.
export function persistRole(role: Role): void {
  fetch("/switch/" + role, { method: "GET", credentials: "same-origin", keepalive: true })
    .catch(() => { /* preferences are best-effort */ });
}

export function persistTheme(theme: Theme): void {
  fetch("/theme/set/" + theme, { method: "POST", credentials: "same-origin", keepalive: true })
    .catch(() => { /* best-effort */ });
}

export function persistAgentCollapsed(state: "open" | "collapsed"): void {
  const url = "/agent/sidebar/" + state;
  let delivered = false;
  if (navigator.sendBeacon) {
    try {
      delivered = navigator.sendBeacon(url, new Blob([], { type: "text/plain" }));
    } catch {
      /* fall through */
    }
  }
  if (!delivered) {
    fetch(url, { method: "POST", credentials: "same-origin", keepalive: true })
      .catch(() => { /* best-effort */ });
  }
}
