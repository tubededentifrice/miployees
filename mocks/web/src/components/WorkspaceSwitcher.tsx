import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Check } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
import type { AvailableWorkspace, Me } from "@/types/api";

// §02 — workspace switcher rendered under the brand row in SideNav.
// Lists every workspace the current user has a grant on (from /me's
// `available_workspaces`); selecting one writes the cookie and
// invalidates every query so the next render is in the new tenant.
//
// Hidden when the user has only one workspace — keeps the chrome
// quiet for the single-tenant default case.

const ROLE_LABEL: Record<string, string> = {
  manager: "Manager",
  worker: "Worker",
  client: "Client",
  guest: "Guest",
};

export default function WorkspaceSwitcher() {
  const { workspaceId, setWorkspaceId } = useWorkspace();
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!meQ.data) return null;
  const available = meQ.data.available_workspaces ?? [];
  if (available.length === 0) return null;

  const activeId = workspaceId ?? meQ.data.current_workspace_id;
  const active = available.find((a) => a.workspace.id === activeId) ?? available[0];
  if (!active) return null;
  // Render the chip even with a single workspace so the active tenant
  // is always visible. The trigger is inert in that case (no menu, no
  // hover affordance) — the user just sees "Bernard workspace ·
  // Manager" as page context.
  const interactive = available.length > 1;

  const pick = (next: AvailableWorkspace) => {
    setOpen(false);
    if (next.workspace.id !== activeId) setWorkspaceId(next.workspace.id);
  };

  return (
    <div className="ws-switcher" ref={ref}>
      <button
        type="button"
        className={"ws-switcher__trigger" + (interactive ? "" : " ws-switcher__trigger--inert")}
        aria-haspopup={interactive ? "listbox" : undefined}
        aria-expanded={interactive ? open : undefined}
        aria-disabled={interactive ? undefined : true}
        onClick={() => { if (interactive) setOpen((v) => !v); }}
      >
        <span className="ws-switcher__name">{active.workspace.name}</span>
        {active.grant_role && (
          <span className="ws-switcher__role">{ROLE_LABEL[active.grant_role] ?? active.grant_role}</span>
        )}
        {interactive && <ChevronDown size={14} aria-hidden="true" className="ws-switcher__chev" />}
      </button>
      {interactive && open && (
        <ul className="ws-switcher__menu" role="listbox" aria-label="Switch workspace">
          {available.map((w) => {
            const selected = w.workspace.id === activeId;
            return (
              <li key={w.workspace.id} role="option" aria-selected={selected}>
                <button
                  type="button"
                  className={"ws-switcher__opt" + (selected ? " ws-switcher__opt--active" : "")}
                  onClick={() => pick(w)}
                >
                  <span className="ws-switcher__opt-name">{w.workspace.name}</span>
                  {w.grant_role && (
                    <span className="ws-switcher__opt-role">{ROLE_LABEL[w.grant_role] ?? w.grant_role}</span>
                  )}
                  {selected && <Check size={14} aria-hidden="true" className="ws-switcher__opt-check" />}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
