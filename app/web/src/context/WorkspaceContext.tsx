import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { persistWorkspace, readWorkspaceCookie } from "@/lib/preferences";

// §02 — active workspace context. Server is authoritative via the
// `crewday_workspace` cookie; this hook mirrors it so the UI can
// react synchronously to a switch without waiting for /me to
// re-fetch. Switching invalidates every workspace-scoped query so
// the next paint reflects the new tenant.

interface WorkspaceCtx {
  workspaceId: string | null;
  setWorkspaceId: (wsid: string) => void;
}

const Ctx = createContext<WorkspaceCtx | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaceId, setWorkspaceIdState] = useState<string | null>(() => readWorkspaceCookie());
  const queryClient = useQueryClient();

  const setWorkspaceId = useCallback((wsid: string) => {
    setWorkspaceIdState(wsid);
    persistWorkspace(wsid);
    // Drop every cached entry — every query is potentially scoped to
    // the previous workspace. /me will re-fetch with the new context.
    queryClient.invalidateQueries();
  }, [queryClient]);

  const value = useMemo(() => ({ workspaceId, setWorkspaceId }), [workspaceId, setWorkspaceId]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return v;
}
