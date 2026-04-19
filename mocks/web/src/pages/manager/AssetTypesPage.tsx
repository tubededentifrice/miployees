import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { AssetIcon } from "@/components/AssetIcon";
import type { AssetType } from "@/types/api";

export default function AssetTypesPage() {
  const typesQ = useQuery({
    queryKey: qk.assetTypes(),
    queryFn: () => fetchJson<AssetType[]>("/api/v1/asset_types"),
  });

  const sub = "Pre-seeded types with default maintenance actions.";

  if (typesQ.isPending) {
    return <DeskPage title="Asset type catalog" sub={sub}><Loading /></DeskPage>;
  }
  if (!typesQ.data) {
    return <DeskPage title="Asset type catalog" sub={sub}>Failed to load.</DeskPage>;
  }

  return (
    <DeskPage title="Asset type catalog" sub={sub}>
      <section className="grid grid--cards">
        {typesQ.data.map((at) => (
          <article key={at.id} className="tpl-card">
            <header className="tpl-card__head">
              <h3 className="tpl-card__title">
                <AssetIcon name={at.icon_name} size={18} />
                {at.name}
              </h3>
              <div className="tpl-card__chips">
                <Chip tone="ghost" size="sm">{at.category}</Chip>
              </div>
            </header>
            {at.default_lifespan_years != null && (
              <div className="tpl-card__meta">
                Expected lifespan: {at.default_lifespan_years} years
              </div>
            )}
            {at.default_actions.length > 0 && (
              <ul className="tpl-card__checklist">
                {at.default_actions.map((da) => (
                  <li key={da.key}>
                    <span>{da.label}</span>
                    {da.interval_days != null && (
                      <span className="muted"> every {da.interval_days}d</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </article>
        ))}
      </section>
    </DeskPage>
  );
}
