// PLACEHOLDER — real impl lands in cd-k69n. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// Right-rail crewday agent surface. Real impl mirrors
// `mocks/web/src/components/AgentSidebar.tsx`.
import type { Role } from "@/types/api";

interface AgentSidebarProps {
  role: Role;
}

export default function AgentSidebar(_props: AgentSidebarProps) {
  return <aside className="agent-sidebar" aria-label="Agent sidebar" />;
}
