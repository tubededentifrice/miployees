import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { InventoryItem, Property } from "@/types/api";

export default function InventoryPage() {
  const invQ = useQuery({
    queryKey: qk.inventory(),
    queryFn: () => fetchJson<InventoryItem[]>("/api/v1/inventory"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const sub = "Per-property stock. Items at or below par trigger a procurement task.";
  const actions = <button className="btn btn--moss">+ New item</button>;
  const overflow = [{ label: "Export CSV", onSelect: () => undefined }];

  if (invQ.isPending || propsQ.isPending) {
    return <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}><Loading /></DeskPage>;
  }
  if (!invQ.data || !propsQ.data) {
    return <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>Failed to load.</DeskPage>;
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  // Group inventory by property, preserving first-seen order.
  const order: string[] = [];
  const byProp = new Map<string, InventoryItem[]>();
  for (const item of invQ.data) {
    if (!byProp.has(item.property_id)) {
      byProp.set(item.property_id, []);
      order.push(item.property_id);
    }
    byProp.get(item.property_id)!.push(item);
  }

  return (
    <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
      {order.map((pid) => {
        const p = propsById.get(pid);
        const items = byProp.get(pid) ?? [];
        return (
          <div key={pid} className="panel">
            <header className="panel__head">
              <h2>
                {p && <Chip tone={p.color} size="sm">{p.name}</Chip>}{" "}
                Inventory
              </h2>
              <span className="muted mono">{items.length} items</span>
            </header>
            <table className="table">
              <thead>
                <tr>
                  <th>Item</th><th>SKU</th><th>Area</th><th>On hand</th><th>Par</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const empty = item.on_hand === 0;
                  const low = item.on_hand < item.par;
                  const rowCls = empty ? "row--critical" : low ? "row--warn" : "";
                  return (
                    <tr key={item.id} className={rowCls}>
                      <td><strong>{item.name}</strong></td>
                      <td className="mono muted">{item.sku}</td>
                      <td>{item.area}</td>
                      <td className="mono"><strong>{item.on_hand}</strong> {item.unit}</td>
                      <td className="mono muted">{item.par}</td>
                      <td>
                        {empty ? (
                          <Chip tone="rust" size="sm">out of stock</Chip>
                        ) : low ? (
                          <Chip tone="sand" size="sm">below par</Chip>
                        ) : (
                          <Chip tone="moss" size="sm">ok</Chip>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}
    </DeskPage>
  );
}
