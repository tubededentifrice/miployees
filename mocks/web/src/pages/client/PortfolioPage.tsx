import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  Me,
  Organization,
  Property,
  PropertyClosure,
  PropertyWorkspace,
  Stay,
  Workspace,
} from "@/types/api";

interface StaysPayload { stays: Stay[]; closures: PropertyClosure[]; }

// §22 — client portfolio. Lists every property whose `client_org_id`
// is one of the binding orgs the user holds a `client` grant for in
// the active workspace. Read-only view; the operational manager UI
// continues to live on the agency side.
export default function ClientPortfolioPage() {
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const orgsQ = useQuery({
    queryKey: qk.organizations("active"),
    queryFn: () => fetchJson<Organization[]>("/api/v1/organizations"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const wsQ = useQuery({
    queryKey: qk.workspaces(),
    queryFn: () => fetchJson<Workspace[]>("/api/v1/workspaces"),
  });
  const pwQ = useQuery({
    queryKey: qk.propertyWorkspaces(),
    queryFn: () => fetchJson<PropertyWorkspace[]>("/api/v1/property_workspaces"),
  });
  const staysQ = useQuery({
    queryKey: qk.stays(),
    queryFn: () => fetchJson<StaysPayload>("/api/v1/stays"),
  });

  if (meQ.isPending || orgsQ.isPending || propsQ.isPending || wsQ.isPending || pwQ.isPending || staysQ.isPending) {
    return <DeskPage title="My properties"><Loading /></DeskPage>;
  }
  if (!meQ.data || !orgsQ.data || !propsQ.data || !wsQ.data || !pwQ.data || !staysQ.data) {
    return <DeskPage title="My properties">Failed to load.</DeskPage>;
  }

  const me = meQ.data;
  const orgIds = new Set(me.client_binding_org_ids ?? []);
  const wsById = new Map(wsQ.data.map((w) => [w.id, w]));
  const orgById = new Map(orgsQ.data.map((o) => [o.id, o]));
  const myProps = propsQ.data.filter((p) => p.client_org_id && orgIds.has(p.client_org_id));
  const stays = staysQ.data.stays;

  return (
    <DeskPage
      title="My properties"
      sub="Properties billed to you in the active workspace. Switch workspaces to see other portfolios."
    >
      {myProps.length === 0 ? (
        <div className="panel">
          <p className="muted">
            No properties billed to your organization in the current workspace.
            If you also work with another agency, switch workspaces from the sidebar.
          </p>
        </div>
      ) : (
        <section className="grid grid--cards">
          {myProps.map((p) => {
            const propStays = stays.filter((s) => s.property_id === p.id);
            const propMembers = pwQ.data.filter((m) => m.property_id === p.id);
            const owner = propMembers.find((m) => m.membership_role === "owner_workspace");
            const managed = propMembers.filter((m) => m.membership_role === "managed_workspace");
            const clientOrg = p.client_org_id ? orgById.get(p.client_org_id) : undefined;
            return (
              <article key={p.id} className="prop-card">
                <Link className="prop-card__link" to={"/property/" + p.id}>
                  <div className={"prop-card__swatch prop-card__swatch--" + p.color}>
                    <span className="prop-card__kind">{p.kind.toUpperCase()}</span>
                  </div>
                  <div className="prop-card__body">
                    <h3 className="prop-card__name">{p.name}</h3>
                    <div className="prop-card__city">{p.city} · {p.timezone}</div>
                    <div className="prop-card__stats">
                      <span>{propStays.length} stays</span>
                      <span>·</span>
                      <span>{p.areas.length} areas</span>
                    </div>
                    <div className="prop-card__chips">
                      {clientOrg && <Chip size="sm" tone="sand">Billed to {clientOrg.name}</Chip>}
                      {owner && (
                        <Chip size="sm" tone="moss">Owner: {wsById.get(owner.workspace_id)?.name ?? owner.workspace_id}</Chip>
                      )}
                      {managed.map((m) => (
                        <Chip key={m.workspace_id} size="sm" tone="sky">
                          Managed by {wsById.get(m.workspace_id)?.name ?? m.workspace_id}
                        </Chip>
                      ))}
                    </div>
                  </div>
                </Link>
              </article>
            );
          })}
        </section>
      )}
    </DeskPage>
  );
}
