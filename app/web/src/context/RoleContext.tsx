// PLACEHOLDER — real impl lands in cd-knp1. DO NOT USE FOR PRODUCTION DECISIONS.
//
// Matches the surface expected by `layouts/PreviewShell`, `App`, and
// future consumers: a `RoleProvider` that supplies `{ role, setRole }`
// through context. The real implementation will back `role` with the
// `crewday_role` cookie and `/switch/:role` mutation — see
// `mocks/web/src/context/RoleContext.tsx`.
import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { Role } from "@/types/api";

interface RoleCtx {
  role: Role;
  setRole: (r: Role) => void;
}

const Ctx = createContext<RoleCtx | null>(null);

export function RoleProvider({ children }: { children: ReactNode }) {
  const [role, setRole] = useState<Role>("manager");
  const value = useMemo(() => ({ role, setRole }), [role, setRole]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRole(): RoleCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRole must be used inside <RoleProvider>");
  return v;
}
