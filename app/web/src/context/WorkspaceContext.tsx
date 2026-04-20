// PLACEHOLDER — real impl lands in cd-knp1. DO NOT USE FOR PRODUCTION DECISIONS.
//
// Stand-in for `context/WorkspaceContext`. The real impl tracks the
// `crewday_workspace` cookie and invalidates every scoped query on
// switch — see `mocks/web/src/context/WorkspaceContext.tsx`.
import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

interface WorkspaceCtx {
  workspaceId: string | null;
  setWorkspaceId: (wsid: string) => void;
}

const Ctx = createContext<WorkspaceCtx | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const value = useMemo(
    () => ({ workspaceId, setWorkspaceId }),
    [workspaceId, setWorkspaceId],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return v;
}
