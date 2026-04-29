import { useQuery } from "@tanstack/react-query";
import { Outlet } from "react-router-dom";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useAuth } from "./useAuth";
import type { ResolvedPermission, ScopeKind } from "@/types/auth";

interface RequirePermissionProps {
  actionKey: string;
  scopeKind?: ScopeKind;
  scopeId?: string;
  children?: React.ReactNode;
}

export function RequirePermission({
  actionKey,
  scopeKind = "workspace",
  scopeId,
  children,
}: RequirePermissionProps) {
  const { user } = useAuth();
  const { workspaceId } = useWorkspace();
  const resolvedScopeId = scopeId ?? (scopeKind === "workspace" ? workspaceId : null);
  const userId = user?.user_id ?? null;

  const q = useQuery({
    queryKey:
      userId && resolvedScopeId
        ? qk.permissionResolved(userId, actionKey, scopeKind, resolvedScopeId)
        : ["permission", "unresolved", actionKey, scopeKind],
    enabled: Boolean(userId && resolvedScopeId),
    queryFn: () => {
      const params = new URLSearchParams({
        action_key: actionKey,
        scope_kind: scopeKind,
        scope_id: resolvedScopeId ?? "",
      });
      return fetchJson<ResolvedPermission>(`/api/v1/permissions/resolved/self?${params}`);
    },
    retry: false,
  });

  if (!userId || !resolvedScopeId || q.isPending) {
    return (
      <div className="auth-hold" role="status" aria-live="polite" aria-busy="true">
        <span className="auth-hold__label">Checking permissions...</span>
      </div>
    );
  }

  if (q.isError || q.data.effect !== "allow") {
    const detail =
      q.error instanceof ApiError && q.error.status >= 500
        ? "Crewday could not verify access for this page."
        : "You do not have permission to open this page.";
    return <ForbiddenPanel detail={detail} />;
  }

  return <>{children ?? <Outlet />}</>;
}

export function ForbiddenPanel({ detail }: { detail?: string }) {
  return (
    <div className="auth-gate" role="alert" aria-labelledby="permission-denied-title">
      <div className="auth-gate__panel">
        <h1 id="permission-denied-title" className="auth-gate__title">Access denied</h1>
        <p className="auth-gate__sub">{detail ?? "You do not have permission to open this page."}</p>
      </div>
    </div>
  );
}

export default RequirePermission;
