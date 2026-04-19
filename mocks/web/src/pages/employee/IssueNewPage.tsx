import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Camera } from "lucide-react";
import { Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import { cap } from "@/lib/strings";
import type { Issue, Property } from "@/types/api";

type Category = "damage" | "broken" | "supplies" | "safety" | "other";
type Severity = "low" | "normal" | "high" | "urgent";

const CATEGORIES: Category[] = ["damage", "broken", "supplies", "safety", "other"];
const SEVERITIES: [Severity, string][] = [
  ["low", "Low"],
  ["normal", "Normal"],
  ["high", "High — unsafe or guest-facing"],
  ["urgent", "Urgent — needs action today"],
];

interface NewIssueBody {
  title: string;
  severity: Severity;
  category: Category;
  property_id: string;
  area: string;
  body: string;
}

export default function IssueNewPage() {
  const nav = useNavigate();
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const [title, setTitle] = useState("");
  const [propertyId, setPropertyId] = useState("");
  const [area, setArea] = useState("");
  const [category, setCategory] = useState<Category>("broken");
  const [severity, setSeverity] = useState<Severity>("normal");
  const [body, setBody] = useState("");

  const create = useMutation({
    mutationFn: (payload: NewIssueBody) =>
      fetchJson<Issue>("/api/v1/issues", { method: "POST", body: payload }),
    onSuccess: () => nav("/me"),
  });

  const header = (
    <PageHeader
      title="Report an issue"
      sub="Tell the manager something is broken, missing, or unsafe. The more specific the better."
    />
  );

  if (propsQ.isPending) return <>{header}<section className="phone__section"><Loading /></section></>;
  if (propsQ.isError || !propsQ.data) {
    return <>{header}<section className="phone__section"><p className="muted">Failed to load.</p></section></>;
  }

  const properties = propsQ.data;
  const activePropertyId = propertyId || properties[0]?.id || "";

  return (
    <>
      {header}
      <section className="phone__section">
        <p className="muted">
          You can also report this in <Link to="/chat">Chat</Link> — it's usually faster.
        </p>

      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate({
            title,
            severity,
            category,
            property_id: activePropertyId,
            area,
            body,
          });
        }}
      >
        <label className="field">
          <span>Short title</span>
          <input
            name="title"
            placeholder="e.g. Bathroom tap dripping"
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>

        <label className="field">
          <span>Property</span>
          <select
            name="property_id"
            required
            value={activePropertyId}
            onChange={(e) => setPropertyId(e.target.value)}
          >
            {properties.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </label>

        <label className="field">
          <span>Area</span>
          <input
            name="area"
            placeholder="e.g. Master bathroom"
            value={area}
            onChange={(e) => setArea(e.target.value)}
          />
        </label>

        <label className="field">
          <span>Category</span>
          <div className="chip-group">
            {CATEGORIES.map((c) => (
              <label key={c} className="chip-radio">
                <input
                  type="radio"
                  name="category"
                  value={c}
                  checked={category === c}
                  onChange={() => setCategory(c)}
                />
                <span>{cap(c)}</span>
              </label>
            ))}
          </div>
        </label>

        <label className="field">
          <span>Severity</span>
          <div className="chip-group">
            {SEVERITIES.map(([s, label]) => (
              <label key={s} className="chip-radio">
                <input
                  type="radio"
                  name="severity"
                  value={s}
                  checked={severity === s}
                  onChange={() => setSeverity(s)}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </label>

        <label className="field">
          <span>What happened?</span>
          <AutoGrowTextarea
            name="body"
            placeholder="What you saw, what you tried, anything the manager should know."
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </label>

        <div className="form__row">
          <button type="button" className="btn btn--ghost">
            <Camera size={16} strokeWidth={1.8} aria-hidden="true" /> Attach photo
          </button>
          <button type="submit" className="btn btn--moss">Send to manager</button>
        </div>
      </form>
      </section>
    </>
  );
}
