import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { BookingBilling, Me, Organization, User } from "@/types/api";

// §22 — billable-hours rollup. The CSV export ships in v1; this is
// the same data rendered for the client to read at a glance.
// Worker pay rates are deliberately NOT shown — clients see what the
// agency charges (`booking_billing.hourly_cents`), not the worker's
// `pay_rule` rate. See "Redactions" in §22.
export default function ClientBillableHoursPage() {
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const usersQ = useQuery({ queryKey: qk.users(), queryFn: () => fetchJson<User[]>("/api/v1/users") });
  const orgsQ = useQuery({
    queryKey: qk.organizations("active"),
    queryFn: () => fetchJson<Organization[]>("/api/v1/organizations"),
  });

  const orgIds = meQ.data?.client_binding_org_ids ?? [];
  const billingQs = useQuery({
    queryKey: qk.bookingBillings(orgIds.join(",")),
    queryFn: async () => {
      const groups = await Promise.all(
        orgIds.map((oid) => fetchJson<BookingBilling[]>("/api/v1/booking_billings?client_org_id=" + oid)),
      );
      return groups.flat();
    },
    enabled: orgIds.length > 0,
  });

  if (meQ.isPending || usersQ.isPending || orgsQ.isPending) {
    return <DeskPage title="Billable hours"><Loading /></DeskPage>;
  }

  const usersById = new Map((usersQ.data ?? []).map((u) => [u.id, u]));
  const orgById = new Map((orgsQ.data ?? []).map((o) => [o.id, o]));
  const rows = billingQs.data ?? [];
  const showWorker = true;  // §22 client.show_worker_names default true

  const totalsByCurrency = rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.currency] = (acc[r.currency] ?? 0) + r.subtotal_cents;
    return acc;
  }, {});

  return (
    <DeskPage
      title="Billable hours"
      sub="What the agency has charged for work on your properties."
    >
      <section className="grid grid--stats">
        {Object.entries(totalsByCurrency).map(([ccy, total]) => (
          <div key={ccy} className="stat-card">
            <div className="stat-card__label">Total · {ccy}</div>
            <div className="stat-card__value">{formatMoney(total, ccy)}</div>
            <div className="stat-card__sub">
              {rows.filter((r) => r.currency === ccy).reduce((m, r) => m + r.billable_minutes, 0)} min
            </div>
          </div>
        ))}
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Recent bookings</h2></header>
        {rows.length === 0 ? (
          <p className="muted">No bookings billed to you yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Client org</th>
                {showWorker && <th>Worker</th>}
                <th>Minutes</th>
                <th>Hourly</th>
                <th>Subtotal</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{orgById.get(r.client_org_id)?.name ?? r.client_org_id}</td>
                  {showWorker && <td>{usersById.get(r.user_id)?.display_name ?? r.user_id}</td>}
                  <td className="table__mono">{r.billable_minutes}</td>
                  <td className="table__mono">{formatMoney(r.hourly_cents, r.currency)}</td>
                  <td className="table__mono">{formatMoney(r.subtotal_cents, r.currency)}</td>
                  <td>
                    <Chip size="sm" tone={r.is_cancellation_fee ? "rust" : "ghost"}>
                      {r.is_cancellation_fee ? "cancel fee" : r.rate_source}
                    </Chip>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </DeskPage>
  );
}
