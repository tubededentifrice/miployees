import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Camera, Timer } from "lucide-react";
import { Chip, Loading } from "@/components/common";
import type { TaskPriority, TaskTemplate } from "@/types/api";

// §08 — decimal qty formatter, trailing zeros trimmed.
function fmtQty(n: number): string {
  const s = n.toFixed(3);
  return s.replace(/\.?0+$/, "");
}

const PRIORITY_TONE: Record<TaskPriority, "ghost" | "sand" | "rust"> = {
  low: "ghost",
  normal: "ghost",
  high: "sand",
  urgent: "rust",
};

export default function TemplatesPage() {
  const tplQ = useQuery({
    queryKey: qk.taskTemplates(),
    queryFn: () => fetchJson<TaskTemplate[]>("/api/v1/task_templates"),
  });

  if (tplQ.isPending) {
    return (
      <DeskPage title="Task templates" actions={<button className="btn btn--moss">+ New template</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!tplQ.data) {
    return (
      <DeskPage title="Task templates" actions={<button className="btn btn--moss">+ New template</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const templates = tplQ.data;

  return (
    <DeskPage
      title="Task templates"
      sub="Reusable definitions. Schedules materialize tasks from these. Edit once, update everywhere."
      actions={<button className="btn btn--moss">+ New template</button>}
    >
      <section className="grid grid--cards">
        {templates.map((tpl) => (
          <article key={tpl.id} className="tpl-card">
            <header className="tpl-card__head">
              <h3 className="tpl-card__title">{tpl.name}</h3>
              <div className="tpl-card__chips">
                <Chip tone="ghost" size="sm">{tpl.role}</Chip>
                <Chip tone={PRIORITY_TONE[tpl.priority]} size="sm">{tpl.priority}</Chip>
                {tpl.photo_evidence !== "disabled" && (
                  <Chip tone="sky" size="sm"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> {tpl.photo_evidence}</Chip>
                )}
              </div>
            </header>
            <p className="tpl-card__desc">{tpl.description}</p>
            <div className="tpl-card__meta">
              <span className="tpl-card__duration">
                <Timer size={14} strokeWidth={1.75} aria-hidden="true" /> {tpl.duration_minutes} min
              </span>
              <span>· scope {tpl.property_scope}</span>
            </div>
            {tpl.checklist.length > 0 && (
              <ul className="tpl-card__checklist">
                {tpl.checklist.map((c, idx) => (
                  <li key={idx}>
                    <span className="checklist__box" aria-hidden="true" />
                    <span>{c.label}</span>
                    {c.guest_visible && (
                      <Chip tone="moss" size="sm">guest-visible</Chip>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {tpl.inventory_effects.length > 0 && (
              <div className="tpl-card__effects">
                {tpl.inventory_effects.some((e) => e.kind === "consume") && (
                  <div className="tpl-effect tpl-effect--consume">
                    <span className="tpl-effect__label">Uses</span>
                    <span>
                      {tpl.inventory_effects
                        .filter((e) => e.kind === "consume")
                        .map((e) => `${fmtQty(e.qty)} ${e.item_ref}`)
                        .join(" · ")}
                    </span>
                  </div>
                )}
                {tpl.inventory_effects.some((e) => e.kind === "produce") && (
                  <div className="tpl-effect tpl-effect--produce">
                    <span className="tpl-effect__label">Produces</span>
                    <span>
                      {tpl.inventory_effects
                        .filter((e) => e.kind === "produce")
                        .map((e) => `${fmtQty(e.qty)} ${e.item_ref}`)
                        .join(" · ")}
                    </span>
                  </div>
                )}
              </div>
            )}
          </article>
        ))}
      </section>
    </DeskPage>
  );
}
