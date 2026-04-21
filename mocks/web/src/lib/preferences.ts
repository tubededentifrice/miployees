// Cookie-backed preferences. Reads are synchronous from document.cookie
// so they can hydrate initial state without waiting for /api/v1/me;
// writes go to FastAPI (which re-sets the cookie) and we update our
// local mirror optimistically so layout doesn't flash.

import type { Role, Theme } from "@/types/api";
import { withBase } from "./api";

const ROLE_COOKIE = "crewday_role";
const THEME_COOKIE = "crewday_theme";
const AGENT_COLLAPSED_COOKIE = "crewday_agent_collapsed";
const NAV_COLLAPSED_COOKIE = "crewday_nav_collapsed";
const WORKSPACE_COOKIE = "crewday_workspace";

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
  if (r === "manager" || r === "client") return r;
  return "employee";
}

export function readWorkspaceCookie(): string | null {
  return readCookie(WORKSPACE_COOKIE);
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

// Tri-state for the LEFT side nav, same shape as the agent rail:
// "1" / "0" / missing. The cookie is set when the user toggles the
// collapse button; missing means we use a viewport default.
export function readNavCollapsedCookie(): boolean | null {
  const v = readCookie(NAV_COLLAPSED_COOKIE);
  if (v === "1") return true;
  if (v === "0") return false;
  return null;
}

// Small screens are already covered by the off-canvas drawer
// (<=720px). Between 720px and NAV_DEFAULT_OPEN_AT we start collapsed
// so laptop widths breathe; wider screens start open.
const NAV_DEFAULT_OPEN_AT = 1200;
function defaultNavCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  return window.innerWidth < NAV_DEFAULT_OPEN_AT;
}

export function initialNavCollapsed(): boolean {
  const pref = readNavCollapsedCookie();
  return pref !== null ? pref : defaultNavCollapsed();
}

// Fire-and-forget writers. The server is authoritative; we optimistic
// -mirror so the next paint reflects the choice.
export function persistRole(role: Role): void {
  fetch(withBase("/switch/" + role), { method: "GET", credentials: "same-origin", keepalive: true })
    .catch(() => { /* preferences are best-effort */ });
}

export function persistTheme(theme: Theme): void {
  fetch(withBase("/theme/set/" + theme), { method: "POST", credentials: "same-origin", keepalive: true })
    .catch(() => { /* best-effort */ });
}

export function persistWorkspace(workspaceId: string): void {
  fetch(withBase("/workspaces/switch/" + workspaceId), { method: "POST", credentials: "same-origin", keepalive: true })
    .catch(() => { /* best-effort */ });
}

export function persistAgentCollapsed(state: "open" | "collapsed"): void {
  const url = withBase("/agent/sidebar/" + state);
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

export function persistNavCollapsed(state: "open" | "collapsed"): void {
  const url = withBase("/nav/sidebar/" + state);
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
