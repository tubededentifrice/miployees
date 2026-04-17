import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  ActionCatalogEntry,
  PermissionGroup,
  PermissionGroupMembersResponse,
  PermissionRule,
  ResolvedPermission,
  RoleGrant,
  User,
  Workspace,
} from "@/types/api";

type Tab = "groups" | "rules";

export default function PermissionsPage() {
  const [tab, setTab] = useState<Tab>("groups");

  const sub =
    "Who can do what. Groups collect users; rules attach to actions. " +
    "Root-only actions (marked) stay with owners regardless of rules.";

  return (
    <DeskPage
      title="Permissions"
      sub={sub}
      actions={
        <div className="permissions__tabs">
          <button
            className={`btn btn--ghost ${tab === "groups" ? "btn--active" : ""}`}
            onClick={() => setTab("groups")}
          >
            Groups
          </button>
          <button
            className={`btn btn--ghost ${tab === "rules" ? "btn--active" : ""}`}
            onClick={() => setTab("rules")}
          >
            Rules
          </button>
        </div>
      }
    >
      {tab === "groups" ? <GroupsTab /> : <RulesTab />}
    </DeskPage>
  );
}

function useWorkspaces() {
  return useQuery({
    queryKey: qk.workspaces(),
    queryFn: () => fetchJson<Workspace[]>("/api/v1/workspaces"),
  });
}

function useUsersIndex() {
  return useQuery({
    queryKey: qk.users(),
    queryFn: () => fetchJson<User[]>("/api/v1/users"),
    select: (rows) =>
      Object.fromEntries(rows.map((u) => [u.id, u])) as Record<string, User>,
  });
}

// ── Groups tab ────────────────────────────────────────────────────────

function GroupsTab() {
  const wss = useWorkspaces();
  const users = useUsersIndex();
  const [workspaceId, setWorkspaceId] = useState<string>("");
  const effectiveWs = workspaceId || wss.data?.[0]?.id || "";

  const groups = useQuery({
    queryKey: qk.permissionGroups("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<PermissionGroup[]>(
        `/api/v1/permission_groups?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const [selected, setSelected] = useState<string>("");
  const selectedId = selected || groups.data?.[0]?.id || "";

  const members = useQuery({
    queryKey: qk.permissionGroupMembers(selectedId),
    queryFn: () =>
      fetchJson<PermissionGroupMembersResponse>(
        `/api/v1/permission_groups/${encodeURIComponent(selectedId)}/members`,
      ),
    enabled: !!selectedId,
  });

  if (wss.isPending || groups.isPending) return <Loading />;
  if (!wss.data || !groups.data) return <div>Failed to load.</div>;

  const selectedGroup = groups.data.find((g) => g.id === selectedId);

  return (
    <div className="permissions__split">
      <section className="panel permissions__groups">
        <header className="panel__header">
          <label className="field">
            <span>Workspace</span>
            <select value={effectiveWs} onChange={(e) => setWorkspaceId(e.target.value)}>
              {wss.data.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        </header>
        <ul className="permissions__group-list">
          {groups.data.map((g) => (
            <li
              key={g.id}
              className={`permissions__group-row ${selectedId === g.id ? "permissions__group-row--active" : ""}`}
              onClick={() => setSelected(g.id)}
            >
              <div className="permissions__group-name">
                {g.name}
                {g.group_kind === "system" ? (
                  <Chip tone="moss" size="sm">system</Chip>
                ) : null}
                {g.is_derived ? <Chip tone="ghost" size="sm">derived</Chip> : null}
              </div>
              <div className="permissions__group-key mono muted">{g.key}</div>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel permissions__members">
        {selectedGroup ? (
          <>
            <header className="panel__header">
              <h3>{selectedGroup.name}</h3>
              <div className="muted">{selectedGroup.description_md || "—"}</div>
            </header>
            {members.isPending ? (
              <Loading />
            ) : members.data ? (
              <>
                {members.data.is_derived ? (
                  <p className="muted">
                    Auto-populated from role_grants on this scope. Add or
                    remove members by editing the underlying grant.
                  </p>
                ) : null}
                {selectedGroup.key === "owners" ? (
                  <p className="muted">
                    <strong>Governance anchor.</strong> Owners can always
                    perform root-only actions; the group must have ≥1
                    active member at all times. Adding or removing members
                    requires the{" "}
                    <code>groups.manage_owners_membership</code> action.
                  </p>
                ) : null}
                <table className="table">
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Email</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.data.members.map((m) => {
                      const u = users.data?.[m.user_id];
                      return (
                        <tr key={m.user_id}>
                          <td>{u?.display_name ?? m.user_id}</td>
                          <td className="mono muted">{u?.email ?? ""}</td>
                          <td>
                            {selectedGroup.is_derived ? (
                              <Chip tone="ghost" size="sm">derived</Chip>
                            ) : (
                              <button className="btn btn--ghost btn--sm">Remove</button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                    {members.data.members.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="muted">
                          No members.
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
                {!selectedGroup.is_derived ? (
                  <div className="panel__footer">
                    <button className="btn btn--moss btn--sm">+ Add member</button>
                  </div>
                ) : null}
              </>
            ) : (
              <div>Failed to load members.</div>
            )}
          </>
        ) : (
          <p className="muted">Pick a group.</p>
        )}
      </section>
    </div>
  );
}

// ── Rules tab ─────────────────────────────────────────────────────────

function RulesTab() {
  const wss = useWorkspaces();
  const users = useUsersIndex();
  const [workspaceId, setWorkspaceId] = useState<string>("");
  const effectiveWs = workspaceId || wss.data?.[0]?.id || "";

  const catalog = useQuery({
    queryKey: qk.actionCatalog(),
    queryFn: () => fetchJson<ActionCatalogEntry[]>("/api/v1/permissions/action_catalog"),
  });

  const rules = useQuery({
    queryKey: qk.permissionRules("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<PermissionRule[]>(
        `/api/v1/permission_rules?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const groups = useQuery({
    queryKey: qk.permissionGroups("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<PermissionGroup[]>(
        `/api/v1/permission_groups?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const groupsById = useMemo(() => {
    return Object.fromEntries((groups.data ?? []).map((g) => [g.id, g]));
  }, [groups.data]);

  if (wss.isPending || catalog.isPending || rules.isPending) return <Loading />;
  if (!wss.data || !catalog.data || !rules.data) return <div>Failed to load.</div>;

  const rulesByAction: Record<string, PermissionRule[]> = {};
  for (const r of rules.data) {
    const bucket = rulesByAction[r.action_key] ?? [];
    bucket.push(r);
    rulesByAction[r.action_key] = bucket;
  }

  return (
    <>
      <section className="panel">
        <header className="panel__header">
          <label className="field">
            <span>Workspace</span>
            <select value={effectiveWs} onChange={(e) => setWorkspaceId(e.target.value)}>
              {wss.data.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        </header>

        <WhoCanDoThis
          users={Object.values(users.data ?? {})}
          actions={catalog.data}
          scopeKind="workspace"
          scopeId={effectiveWs}
        />
      </section>

      <section className="panel">
        <table className="table table--roomy permissions__rules">
          <thead>
            <tr>
              <th>Action</th>
              <th>Default (if no rule matches)</th>
              <th>Active rules on this workspace</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {catalog.data.map((entry) => {
              const rs = rulesByAction[entry.key] ?? [];
              return (
                <tr key={entry.key}>
                  <td>
                    <div className="mono">{entry.key}</div>
                    <div className="table__sub">{entry.description}</div>
                    <div>
                      {entry.root_only ? (
                        <Chip tone="rust" size="sm">owners only</Chip>
                      ) : null}
                      {entry.root_protected_deny ? (
                        <Chip tone="sand" size="sm">owners immune to deny</Chip>
                      ) : null}
                      <Chip tone="ghost" size="sm">{entry.spec}</Chip>
                    </div>
                  </td>
                  <td>
                    {entry.default_allow.length === 0 ? (
                      <span className="muted">no default</span>
                    ) : (
                      entry.default_allow.map((k) => (
                        <Chip key={k} tone="moss" size="sm">{k}</Chip>
                      ))
                    )}
                  </td>
                  <td>
                    {rs.length === 0 ? (
                      <span className="muted">— default applies —</span>
                    ) : (
                      rs.map((r) => (
                        <RuleChip
                          key={r.id}
                          rule={r}
                          groupLabel={groupsById[r.subject_id]?.name}
                          userLabel={users.data?.[r.subject_id]?.display_name}
                        />
                      ))
                    )}
                  </td>
                  <td>
                    {entry.root_only ? (
                      <span className="muted">—</span>
                    ) : (
                      <button className="btn btn--ghost btn--sm">+ Rule</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}

function RuleChip({
  rule,
  groupLabel,
  userLabel,
}: {
  rule: PermissionRule;
  groupLabel?: string;
  userLabel?: string;
}) {
  const subject =
    rule.subject_kind === "group"
      ? groupLabel ?? rule.subject_id
      : userLabel ?? rule.subject_id;
  const tone = rule.effect === "allow" ? "moss" : "rust";
  return (
    <Chip tone={tone} size="sm">
      {rule.effect} · {rule.subject_kind}: {subject}
    </Chip>
  );
}

// Live "who can do this?" preview — calls the resolver.
function WhoCanDoThis({
  users,
  actions,
  scopeKind,
  scopeId,
}: {
  users: User[];
  actions: ActionCatalogEntry[];
  scopeKind: "workspace" | "property" | "organization";
  scopeId: string;
}) {
  const [userId, setUserId] = useState<string>(users[0]?.id ?? "");
  const [actionKey, setActionKey] = useState<string>(actions[0]?.key ?? "");

  const resolved = useQuery({
    queryKey: qk.permissionResolved(userId, actionKey, scopeKind, scopeId),
    queryFn: () =>
      fetchJson<ResolvedPermission>(
        `/api/v1/permissions/resolved?user_id=${encodeURIComponent(userId)}` +
          `&action_key=${encodeURIComponent(actionKey)}` +
          `&scope_kind=${scopeKind}&scope_id=${encodeURIComponent(scopeId)}`,
      ),
    enabled: !!userId && !!actionKey && !!scopeId,
  });

  return (
    <div className="permissions__resolver">
      <h4>Who can do this?</h4>
      <div className="permissions__resolver-fields">
        <label className="field">
          <span>User</span>
          <select value={userId} onChange={(e) => setUserId(e.target.value)}>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.display_name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Action</span>
          <select value={actionKey} onChange={(e) => setActionKey(e.target.value)}>
            {actions.map((a) => (
              <option key={a.key} value={a.key}>
                {a.key}
              </option>
            ))}
          </select>
        </label>
      </div>
      {resolved.isPending ? (
        <Loading />
      ) : resolved.data ? (
        <div className="permissions__resolver-result">
          <Chip tone={resolved.data.effect === "allow" ? "moss" : "rust"}>
            {resolved.data.effect}
          </Chip>{" "}
          <span className="mono muted">
            via <strong>{resolved.data.source_layer}</strong>
          </span>
          {resolved.data.matched_groups.length > 0 ? (
            <span className="muted">
              {" "}
              · matched{" "}
              {resolved.data.matched_groups.map((g) => (
                <Chip key={g} tone="ghost" size="sm">{g}</Chip>
              ))}
            </span>
          ) : null}
          {resolved.data.source_rule_id ? (
            <div className="mono muted">rule: {resolved.data.source_rule_id}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// Re-export `RoleGrant` so the type is referenced (keeps linter quiet
// when future extensions consult role_grants from this page).
export type { RoleGrant };
