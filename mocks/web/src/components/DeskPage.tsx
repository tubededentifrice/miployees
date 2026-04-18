import { type ReactNode } from "react";

interface Props {
  title: string;
  sub?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}

// Shared manager-page chrome: topbar (title + optional sub + actions)
// that scrolls with the page, then content stacked at 22px gaps.
// Every ManagerLayout page renders through this so topbar spacing
// stays uniform.
export default function DeskPage({ title, sub, actions, children }: Props) {
  return (
    <>
      <header className="desk__topbar">
        <div>
          <h1 className="page-title">{title}</h1>
          {sub ? <p className="page-sub">{sub}</p> : null}
        </div>
        {actions ? <div className="desk__topbar-actions">{actions}</div> : null}
      </header>
      <div className="desk__content">{children}</div>
    </>
  );
}
