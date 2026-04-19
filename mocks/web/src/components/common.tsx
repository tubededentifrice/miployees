import type { InputHTMLAttributes, ReactNode } from "react";
import { useEffect, useRef } from "react";

// Small presentational helpers. Each one is ~10 lines — trivial at
// the markup level but standardises class spelling so future refactors
// don't have to grep 35 files for `.chip--moss`.

export function Chip({
  tone = "ghost",
  size,
  children,
  title,
}: {
  tone?: "moss" | "rust" | "sand" | "sky" | "ghost" | "active";
  size?: "sm" | "lg";
  children: ReactNode;
  title?: string;
}) {
  const cls = ["chip", "chip--" + tone, size ? "chip--" + size : ""].filter(Boolean).join(" ");
  return <span className={cls} title={title}>{children}</span>;
}

export function Dot({ tone }: { tone: "moss" | "rust" | "sand" }) {
  return <span className={"dot dot--" + tone} aria-hidden="true" />;
}

type CheckboxProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> & {
  label?: ReactNode;
  hint?: ReactNode;
  size?: "sm" | "lg";
  tone?: "moss" | "rust" | "sky";
  block?: boolean;
  indeterminate?: boolean;
  className?: string;
};

// Field-guide checkbox. Matches the .checkbox BEM block in globals.css.
// The native input carries all semantics; the .checkbox__box + __tick
// are the visual layer. Indeterminate flips a CSS class because there
// is no `:indeterminate` pseudo that composes cleanly with our tick.
export function Checkbox({
  label,
  hint,
  size,
  tone,
  block,
  indeterminate,
  className = "",
  ...input
}: CheckboxProps) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = !!indeterminate;
  }, [indeterminate]);

  const rootCls = [
    "checkbox",
    size ? "checkbox--" + size : "",
    tone ? "checkbox--" + tone : "",
    block ? "checkbox--block" : "",
    !label && !hint ? "checkbox--standalone" : "",
    className,
  ].filter(Boolean).join(" ");

  const inputCls = [
    "checkbox__input",
    indeterminate ? "is-indeterminate" : "",
  ].filter(Boolean).join(" ");

  return (
    <label className={rootCls}>
      <input ref={ref} type="checkbox" className={inputCls} {...input} />
      <span className="checkbox__box" aria-hidden="true">
        <svg className="checkbox__tick" viewBox="0 0 18 18">
          <path d="M3.8 9.6 L7.4 13 L14.2 5.4" />
        </svg>
        <span className="checkbox__dash" aria-hidden="true" />
      </span>
      {(label || hint) && (
        <span className="checkbox__label">
          {label}
          {hint ? <span className="checkbox__label--hint">{hint}</span> : null}
        </span>
      )}
    </label>
  );
}

export type ChipTone = "moss" | "rust" | "sand" | "sky" | "ghost";

export interface FilterChipOption<T extends string> {
  value: T;
  label: ReactNode;
  tone?: ChipTone;
}

// Interactive filter row. An empty string is the "all" sentinel; keep
// it in state so callers can read `active === ""` without a second
// field. Markup stays identical to what AssetsPage/DocumentsPage emit
// by hand so `.desk-filters .chip--active` CSS keeps matching.
export function FilterChipGroup<T extends string>({
  value,
  onChange,
  allLabel = "All",
  options,
}: {
  value: T | "";
  onChange: (next: T | "") => void;
  allLabel?: ReactNode;
  options: FilterChipOption<T>[];
}) {
  return (
    <div className="desk-filters">
      <span
        className={"chip chip--ghost chip--sm" + (value === "" ? " chip--active" : "")}
        onClick={() => onChange("")}
      >
        {allLabel}
      </span>
      {options.map((opt) => {
        const tone = opt.tone ?? "ghost";
        const active = value === opt.value ? " chip--active" : "";
        return (
          <span
            key={opt.value}
            className={"chip chip--" + tone + " chip--sm" + active}
            onClick={() => onChange(opt.value)}
          >
            {opt.label}
          </span>
        );
      })}
    </div>
  );
}

export function Avatar({
  initials,
  url,
  size = "md",
  alt,
}: {
  initials: string;
  url?: string | null;
  size?: "xs" | "sm" | "md" | "xl";
  alt?: string;
}) {
  return (
    <span className={"avatar avatar--" + size}>
      {url
        ? <img className="avatar__img" src={url} alt={alt ?? initials} />
        : initials}
    </span>
  );
}

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={"panel " + className}>
      {(title || right) && (
        <header className="panel__head">
          {title ? <h2>{title}</h2> : <span />}
          {right}
        </header>
      )}
      {children}
    </div>
  );
}

export function StatCard({
  label,
  value,
  sub,
  warn,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  warn?: boolean;
}) {
  return (
    <div className={"stat-card" + (warn ? " stat-card--warn" : "")}>
      <div className="stat-card__label">{label}</div>
      <div className="stat-card__value">{value}</div>
      {sub ? <div className="stat-card__sub">{sub}</div> : null}
    </div>
  );
}

export function ProgressBar({ value, slim }: { value: number; slim?: boolean }) {
  return (
    <span className={"progress-bar" + (slim ? " progress-bar--slim" : "")}>
      <span style={{ width: Math.max(0, Math.min(100, value)) + "%" }} />
    </span>
  );
}

export function EmptyState({
  glyph,
  children,
  variant,
}: {
  glyph?: ReactNode;
  children: ReactNode;
  variant?: "celebrate" | "quiet";
}) {
  const cls = ["empty-state", variant ? "empty-state--" + variant : ""].filter(Boolean).join(" ");
  return (
    <div className={cls}>
      {glyph ? <span className="empty-state__glyph" aria-hidden="true">{glyph}</span> : null}
      {typeof children === "string" ? <p>{children}</p> : children}
    </div>
  );
}

export function Loading() {
  return <div className="empty-state empty-state--quiet">Loading…</div>;
}
