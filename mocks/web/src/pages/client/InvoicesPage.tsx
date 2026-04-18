import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { Me, Property, VendorInvoice } from "@/types/api";

// §22 — vendor invoices billed to one of the user's binding orgs
// (the orgs they hold a `client` grant for in the active workspace).
// Read-only view; clients can't mark anything paid (the workspace
// pushes funds and owns the paid bookkeeping flag).
export default function ClientInvoicesPage() {
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const orgIds = useMemo(() => meQ.data?.client_binding_org_ids ?? [], [meQ.data]);
  const invoicesQ = useQuery({
    queryKey: qk.vendorInvoices(orgIds.join(",")),
    queryFn: async () => {
      const groups = await Promise.all(
        orgIds.map((oid) => fetchJson<VendorInvoice[]>("/api/v1/vendor_invoices?client_org_id=" + oid)),
      );
      return groups.flat();
    },
    enabled: orgIds.length > 0,
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  if (meQ.isPending) return <DeskPage title="Invoices"><Loading /></DeskPage>;

  const invoices = invoicesQ.data ?? [];
  const propsById = new Map((propsQ.data ?? []).map((p) => [p.id, p]));

  return (
    <DeskPage
      title="Invoices"
      sub="Vendor invoices billed to your organization (§22)."
    >
      {invoices.length === 0 ? (
        <div className="panel">
          <p className="muted">No invoices billed to you yet.</p>
        </div>
      ) : (
        <div className="panel">
          <table className="table">
            <thead>
              <tr><th>Invoice</th><th>Property</th><th>Total</th><th>Status</th><th>Billed</th><th>Due</th></tr>
            </thead>
            <tbody>
              {invoices.map((v) => (
                <tr key={v.id}>
                  <td>{v.id}</td>
                  <td>{v.property_id ? propsById.get(v.property_id)?.name ?? v.property_id : "—"}</td>
                  <td className="table__mono">{formatMoney(v.total_cents, v.currency)}</td>
                  <td>
                    <Chip
                      size="sm"
                      tone={v.status === "paid" ? "moss" : v.status === "approved" ? "sky" : "ghost"}
                    >
                      {v.status}
                    </Chip>
                  </td>
                  <td className="table__mono">{v.billed_at}</td>
                  <td className="table__mono muted">{v.due_on ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeskPage>
  );
}
