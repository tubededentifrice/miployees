import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
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

interface StaysPayload {
  stays: Stay[];
  closures: PropertyClosure[];
}

// §02 — short label for membership_role on the property card. Keep the
// vocabulary small so the chip doesn't crowd the row.
const MEMBERSHIP_LABEL: Record<string, string> = {
  owner_workspace: "Owner",
  managed_workspace: "Managed",
  observer_workspace: "Observer",
};

export default function PropertiesPage() {
  const { workspaceId } = useWorkspace();
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const staysQ = useQuery({
    queryKey: qk.stays(),
    queryFn: () => fetchJson<StaysPayload>("/api/v1/stays"),
  });
  const wsQ = useQuery({
    queryKey: qk.workspaces(),
    queryFn: () => fetchJson<Workspace[]>("/api/v1/workspaces"),
  });
  const orgsQ = useQuery({
    queryKey: qk.organizations(workspaceId ?? "active"),
    queryFn: () => fetchJson<Organization[]>("/api/v1/organizations"),
  });
  const pwQ = useQuery({
    queryKey: qk.propertyWorkspaces(),
    queryFn: () => fetchJson<PropertyWorkspace[]>("/api/v1/property_workspaces"),
  });

  if (propsQ.isPending || staysQ.isPending || wsQ.isPending || orgsQ.isPending || pwQ.isPending) {
    return (
      <DeskPage title="Properties" actions={<button className="btn btn--moss">+ Add property</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!propsQ.data || !staysQ.data || !wsQ.data || !orgsQ.data || !pwQ.data) {
    return (
      <DeskPage title="Properties" actions={<button className="btn btn--moss">+ Add property</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const properties = propsQ.data;
  const stays = staysQ.data.stays;
  const closures = staysQ.data.closures;
  const wsById = new Map(wsQ.data.map((w) => [w.id, w]));
  const orgById = new Map(orgsQ.data.map((o) => [o.id, o]));
  const activeWsId = workspaceId ?? meQ.data?.current_workspace_id ?? null;
  const memberships = pwQ.data;

  return (
    <DeskPage
      title="Properties"
      actions={<button className="btn btn--moss">+ Add property</button>}
    >
      <section className="grid grid--cards">
        {properties.map((p) => {
          const propStays = stays.filter((s) => s.property_id === p.id);
          const propClosures = closures.filter((c) => c.property_id === p.id);
          const propMembers = memberships.filter((m) => m.property_id === p.id);
          const ourMembership = activeWsId
            ? propMembers.find((m) => m.workspace_id === activeWsId)
            : undefined;
          const externalMembers = propMembers.filter((m) => m.workspace_id !== activeWsId);
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
                    {propClosures.length > 0 && (
                      <>
                        <span>·</span>
                        <span className="muted">
                          {propClosures.length} closure{propClosures.length > 1 ? "s" : ""}
                        </span>
                      </>
                    )}
                  </div>
                  <div className="prop-card__chips">
                    {ourMembership && (
                      <Chip
                        size="sm"
                        tone={ourMembership.membership_role === "owner_workspace" ? "moss" : "sky"}
                      >
                        {MEMBERSHIP_LABEL[ourMembership.membership_role]}
                      </Chip>
                    )}
                    {externalMembers.map((m) => {
                      const ws = wsById.get(m.workspace_id);
                      if (!ws) return null;
                      return (
                        <Chip key={m.workspace_id} size="sm" tone="ghost">
                          {MEMBERSHIP_LABEL[m.membership_role]}: {ws.name}
                        </Chip>
                      );
                    })}
                    {clientOrg && (
                      <Chip size="sm" tone="sand">Client: {clientOrg.name}</Chip>
                    )}
                  </div>
                </div>
              </Link>
              <div className="prop-card__footer">
                <Link to={"/property/" + p.id} className="link">Overview</Link>
                <Link to={"/property/" + p.id + "/closures"} className="link link--muted">
                  Closures →
                </Link>
              </div>
            </article>
          );
        })}
      </section>
    </DeskPage>
  );
}
