import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type {
  BookingBilling,
  ClientRate,
  ClientUserRate,
  Me,
  Organization,
  Property,
  User,
  VendorInvoice,
} from "@/types/api";

interface OrganizationDetailPayload {
  organization: Organization;
  properties_billed: Property[];
  client_rates: ClientRate[];
  client_user_rates: ClientUserRate[];
  recent_booking_billings: BookingBilling[];
  vendor_invoices_billed_to: VendorInvoice[];
  vendor_invoices_billed_from: VendorInvoice[];
  portal_user: User | null;
}

// `WorkRole` is not currently exported from api.ts — read the legacy
// `Role` shape (id + name) used everywhere else for the rate table.
interface WorkRoleLite {
  id: string;
  name: string;
}

// §22 — Organizations directory. Lists every organization in the active
// workspace (clients we bill, suppliers that bill us, or both) and lets
// the manager drill into one to see its rate card, recent booking
// billings, and the vendor invoices flowing through it.
export default function OrganizationsPage() {
  const { workspaceId } = useWorkspace();
  const [activeOid, setActiveOid] = useState<string | null>(null);
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const orgsQ = useQuery({
    queryKey: qk.organizations(workspaceId ?? "active"),
    queryFn: () => fetchJson<Organization[]>("/api/v1/organizations"),
  });
  const rolesQ = useQuery({
    queryKey: ["work_roles", workspaceId ?? "active"] as const,
    queryFn: () => fetchJson<WorkRoleLite[]>("/api/v1/work_roles"),
  });
  const usersQ = useQuery({
    queryKey: qk.users(),
    queryFn: () => fetchJson<User[]>("/api/v1/users"),
  });

  const orgs = orgsQ.data ?? [];
  const visibleOrgs = useMemo(() => orgs, [orgs]);
  const selectedOid = activeOid ?? visibleOrgs[0]?.id ?? null;

  const detailQ = useQuery({
    queryKey: qk.organization(selectedOid ?? ""),
    queryFn: () => fetchJson<OrganizationDetailPayload>("/api/v1/organizations/" + selectedOid),
    enabled: selectedOid !== null,
  });

  void meQ;

  if (orgsQ.isPending) return <DeskPage title="Organizations"><Loading /></DeskPage>;

  if (visibleOrgs.length === 0) {
    return (
      <DeskPage
        title="Organizations"
        actions={<button className="btn btn--moss">+ New organization</button>}
      >
        <div className="panel">
          <p className="muted">
            No organizations in this workspace. Create one when an owner enters
            "agency mode" — link a property to a client, or register a supplier
            to route agency-supplied engagements.
          </p>
        </div>
      </DeskPage>
    );
  }

  return (
    <DeskPage
      title="Organizations"
      sub="Clients we bill, suppliers that bill us, and the contracts in between."
      actions={<button className="btn btn--moss">+ New organization</button>}
    >
      <section className="grid grid--split">
        <div className="panel">
          <header className="panel__head"><h2>Counterparties</h2></header>
          <ul className="org-list">
            {visibleOrgs.map((o) => (
              <li
                key={o.id}
                className={"org-list__row" + (o.id === selectedOid ? " org-list__row--active" : "")}
                onClick={() => setActiveOid(o.id)}
              >
                <div>
                  <strong>{o.name}</strong>
                  {o.legal_name && o.legal_name !== o.name && (
                    <div className="muted">{o.legal_name}</div>
                  )}
                </div>
                <div className="org-list__chips">
                  {o.is_client && <Chip tone="moss" size="sm">Client</Chip>}
                  {o.is_supplier && <Chip tone="sky" size="sm">Supplier</Chip>}
                  <Chip tone="ghost" size="sm">{o.default_currency}</Chip>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <OrganizationDetail
          loading={detailQ.isPending}
          detail={detailQ.data ?? null}
          rolesById={new Map((rolesQ.data ?? []).map((r) => [r.id, r]))}
          usersById={new Map((usersQ.data ?? []).map((u) => [u.id, u]))}
        />
      </section>
    </DeskPage>
  );
}

function OrganizationDetail({
  loading,
  detail,
  rolesById,
  usersById,
}: {
  loading: boolean;
  detail: OrganizationDetailPayload | null;
  rolesById: Map<string, WorkRoleLite>;
  usersById: Map<string, User>;
}) {
  if (loading || !detail) {
    return <div className="panel"><Loading /></div>;
  }
  const o = detail.organization;
  return (
    <div className="panel">
      <header className="panel__head">
        <h2>{o.name}</h2>
        <div className="sharing-client__chips">
          {o.is_client && <Chip tone="moss" size="sm">Client</Chip>}
          {o.is_supplier && <Chip tone="sky" size="sm">Supplier</Chip>}
        </div>
      </header>
      {o.notes && <p className="muted">{o.notes}</p>}

      {o.tax_id && (
        <p className="org-meta">
          <span className="muted">Tax ID:</span> <code className="inline-code">{o.tax_id}</code>
        </p>
      )}

      <section className="org-section">
        <h3>Contacts</h3>
        {o.contacts.length === 0 ? (
          <p className="muted">No contacts on file.</p>
        ) : (
          <ul className="org-contacts">
            {o.contacts.map((c, i) => (
              <li key={i}>
                <strong>{c.name}</strong>
                <span className="muted"> · {c.role}</span>
                <div className="muted mono">{c.email} · {c.phone_e164}</div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {o.is_client && (
        <>
          <section className="org-section">
            <h3>Properties billed</h3>
            {detail.properties_billed.length === 0 ? (
              <p className="muted">No properties currently billed to this client.</p>
            ) : (
              <ul className="org-prop-list">
                {detail.properties_billed.map((p) => (
                  <li key={p.id}>
                    <strong>{p.name}</strong>
                    <span className="muted"> — {p.city}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="org-section">
            <h3>Rate card</h3>
            {detail.client_rates.length === 0 && detail.client_user_rates.length === 0 ? (
              <p className="muted">No rates on file. Shifts will surface in the "unpriced" CSV bucket.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr><th>Subject</th><th>Hourly</th><th>From</th><th>To</th></tr>
                </thead>
                <tbody>
                  {detail.client_rates.map((r) => (
                    <tr key={r.id}>
                      <td>Role · <strong>{rolesById.get(r.work_role_id)?.name ?? r.work_role_id}</strong></td>
                      <td className="table__mono">{formatMoney(r.hourly_cents, r.currency)}/h</td>
                      <td className="table__mono">{r.effective_from}</td>
                      <td className="table__mono muted">{r.effective_to ?? "ongoing"}</td>
                    </tr>
                  ))}
                  {detail.client_user_rates.map((r) => (
                    <tr key={r.id}>
                      <td>User · <strong>{usersById.get(r.user_id)?.display_name ?? r.user_id}</strong></td>
                      <td className="table__mono">{formatMoney(r.hourly_cents, r.currency)}/h</td>
                      <td className="table__mono">{r.effective_from}</td>
                      <td className="table__mono muted">{r.effective_to ?? "ongoing"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="org-section">
            <h3>Recent billings</h3>
            {detail.recent_booking_billings.length === 0 ? (
              <p className="muted">No booking billings yet.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr><th>Worker</th><th>Minutes</th><th>Hourly</th><th>Subtotal</th><th>Source</th></tr>
                </thead>
                <tbody>
                  {detail.recent_booking_billings.map((b) => (
                    <tr key={b.id}>
                      <td>{usersById.get(b.user_id)?.display_name ?? b.user_id}</td>
                      <td className="table__mono">{b.billable_minutes}</td>
                      <td className="table__mono">{formatMoney(b.hourly_cents, b.currency)}</td>
                      <td className="table__mono">{formatMoney(b.subtotal_cents, b.currency)}</td>
                      <td>
                        <Chip
                          size="sm"
                          tone={
                            b.is_cancellation_fee
                              ? "rust"
                              : b.rate_source === "unpriced"
                                ? "rust"
                                : "ghost"
                          }
                        >
                          {b.is_cancellation_fee ? "cancel fee" : b.rate_source}
                        </Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      <section className="org-section">
        <h3>Vendor invoices</h3>
        {detail.vendor_invoices_billed_to.length === 0 && detail.vendor_invoices_billed_from.length === 0 ? (
          <p className="muted">No invoices yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr><th>Invoice</th><th>Direction</th><th>Total</th><th>Status</th><th>Billed</th></tr>
            </thead>
            <tbody>
              {detail.vendor_invoices_billed_to.map((v) => (
                <tr key={v.id}>
                  <td>{v.id}</td>
                  <td><Chip size="sm" tone="rust">we owe</Chip></td>
                  <td className="table__mono">{formatMoney(v.total_cents, v.currency)}</td>
                  <td><Chip size="sm" tone={v.status === "paid" ? "moss" : v.status === "approved" ? "sky" : "ghost"}>{v.status}</Chip></td>
                  <td className="table__mono">{v.billed_at}</td>
                </tr>
              ))}
              {detail.vendor_invoices_billed_from
                .filter((v) => !detail.vendor_invoices_billed_to.includes(v))
                .map((v) => (
                  <tr key={v.id}>
                    <td>{v.id}</td>
                    <td><Chip size="sm" tone="moss">they owe</Chip></td>
                    <td className="table__mono">{formatMoney(v.total_cents, v.currency)}</td>
                    <td><Chip size="sm" tone={v.status === "paid" ? "moss" : "ghost"}>{v.status}</Chip></td>
                    <td className="table__mono">{v.billed_at}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </section>

      {detail.portal_user && (
        <p className="org-portal muted">
          Portal user: <strong>{detail.portal_user.display_name}</strong> ({detail.portal_user.email})
        </p>
      )}
    </div>
  );
}
