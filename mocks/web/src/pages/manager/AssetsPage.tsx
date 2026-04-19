import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, FilterChipGroup, Loading } from "@/components/common";
import { AssetIcon } from "@/components/AssetIcon";
import { ASSET_CONDITION_TONE, ASSET_STATUS_TONE } from "@/lib/tones";
import type { Asset, AssetType, Property } from "@/types/api";

export default function AssetsPage() {
  const [activeCategory, setActiveCategory] = useState<string>("");
  const [activeProperty, setActiveProperty] = useState<string>("");

  const assetsQ = useQuery({
    queryKey: qk.assets(),
    queryFn: () => fetchJson<Asset[]>("/api/v1/assets"),
  });
  const typesQ = useQuery({
    queryKey: qk.assetTypes(),
    queryFn: () => fetchJson<AssetType[]>("/api/v1/asset_types"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const sub = "Tracked equipment and appliances across all properties.";
  const actions = <button className="btn btn--moss">+ New asset</button>;

  if (assetsQ.isPending || typesQ.isPending || propsQ.isPending) {
    return <DeskPage title="Assets" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!assetsQ.data || !typesQ.data || !propsQ.data) {
    return <DeskPage title="Assets" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const typesById = new Map(typesQ.data.map((t) => [t.id, t]));
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  const categories = Array.from(new Set(typesQ.data.map((t) => t.category)));

  const filtered = assetsQ.data.filter((a) => {
    if (activeProperty && a.property_id !== activeProperty) return false;
    if (activeCategory) {
      const at = a.asset_type_id ? typesById.get(a.asset_type_id) : null;
      if (!at || at.category !== activeCategory) return false;
    }
    return true;
  });

  return (
    <DeskPage title="Assets" sub={sub} actions={actions}>
      <section className="panel">
        <FilterChipGroup
          value={activeCategory}
          onChange={setActiveCategory}
          options={categories.map((cat) => ({ value: cat, label: cat }))}
        />
        <FilterChipGroup
          value={activeProperty}
          onChange={setActiveProperty}
          allLabel="All properties"
          options={propsQ.data.map((p) => ({ value: p.id, label: p.name, tone: p.color }))}
        />

        <table className="table">
          <thead>
            <tr>
              <th>Asset</th>
              <th>Type</th>
              <th>Property</th>
              <th>Area</th>
              <th>Condition</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((a) => {
              const at = a.asset_type_id ? typesById.get(a.asset_type_id) : null;
              const prop = propsById.get(a.property_id);
              const makeLine = [a.make, a.model].filter(Boolean).join(" ");
              return (
                <tr key={a.id}>
                  <td>
                    <Link to={"/asset/" + a.id} className="link asset-name-link">
                      {at && <AssetIcon name={at.icon_name} />}
                      <strong>{a.name}</strong>
                    </Link>
                    {makeLine && <span className="table__sub">{makeLine}</span>}
                  </td>
                  <td>{at?.name ?? <span className="muted">--</span>}</td>
                  <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                  <td>{a.area ?? <span className="muted">--</span>}</td>
                  <td><Chip tone={ASSET_CONDITION_TONE[a.condition]} size="sm">{a.condition.replace("_", " ")}</Chip></td>
                  <td><Chip tone={ASSET_STATUS_TONE[a.status]} size="sm">{a.status.replace("_", " ")}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </DeskPage>
  );
}
