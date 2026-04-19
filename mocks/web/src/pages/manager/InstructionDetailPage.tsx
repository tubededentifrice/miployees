import { Fragment, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { Instruction, Property } from "@/types/api";

const SCOPE_TONE: Record<Instruction["scope"], "sky" | "moss" | "sand"> = {
  global: "sky",
  property: "moss",
  area: "sand",
};

function fmtSaved(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

// Mock body is plain text with newlines; render with <br> between lines.
// Real Markdown rendering will land when the spec calls for it.
function renderBody(body: string): ReactNode {
  const lines = body.split("\n");
  return lines.map((line, idx) => (
    <Fragment key={idx}>
      {line}
      {idx < lines.length - 1 && <br />}
    </Fragment>
  ));
}

export default function InstructionDetailPage() {
  const { iid } = useParams<{ iid: string }>();
  const instrQ = useQuery({
    queryKey: qk.instruction(iid ?? ""),
    queryFn: () => fetchJson<Instruction>("/api/v1/instructions/" + iid),
    enabled: Boolean(iid),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  if (!iid) return <DeskPage title="Instruction">Missing instruction id.</DeskPage>;
  if (instrQ.isPending || propsQ.isPending) {
    return <DeskPage title="Instruction"><Loading /></DeskPage>;
  }
  if (!instrQ.data || !propsQ.data) {
    return <DeskPage title="Instruction">Failed to load.</DeskPage>;
  }

  const i = instrQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const propName = i.property_id ? propsById.get(i.property_id)?.name ?? "" : "";
  const scopeLabel =
    i.scope === "global" ? "House-wide" :
    i.scope === "property" ? propName :
    propName + (i.area ? " · " + i.area : "");

  const sub = (
    <>
      <Link to="/instructions" className="link">← All instructions</Link>{" "}·{" "}
      <Chip tone={SCOPE_TONE[i.scope]} size="sm">{scopeLabel}</Chip>
    </>
  );
  const actions = <button className="btn btn--moss">Edit</button>;
  const overflow = [
    { label: "View revisions", onSelect: () => undefined },
  ];

  return (
    <DeskPage title={i.title} sub={sub} actions={actions} overflow={overflow}>
      <article className="panel panel--article">
        <div className="kb-body">
          {renderBody(i.body_md)}
        </div>
        <footer className="kb-footer">
          <div>
            {i.tags.map((t) => (
              <Chip key={t} tone="ghost" size="sm">#{t}</Chip>
            ))}
          </div>
          <div className="mono muted">Revision {i.version} · saved {fmtSaved(i.updated_at)}</div>
        </footer>
      </article>

      <section className="panel">
        <header className="panel__head"><h2>Where this applies</h2></header>
        <ul className="task-list task-list--desk">
          <li className="task-row">
            <span className="task-row__time mono">via scope</span>
            <span className="task-row__title"><strong>All tasks matching the scope above</strong></span>
            <Chip tone="ghost" size="sm">automatic</Chip>
          </li>
          <li className="task-row">
            <span className="task-row__time mono">linked to template</span>
            <span className="task-row__title"><strong>Linen change — master bedroom</strong></span>
            <Chip tone="moss" size="sm">template link</Chip>
          </li>
        </ul>
      </section>
    </DeskPage>
  );
}
