import { type ReactNode } from "react";
import PageHeader, { type PageHeaderOverflowItem } from "./PageHeader";

interface Props {
  title: string;
  sub?: ReactNode;
  /** At most ONE primary trailing action per §14 "Page header". */
  actions?: ReactNode;
  /** Secondary actions rendered in the `⋯` overflow menu. */
  overflow?: PageHeaderOverflowItem[];
  children: ReactNode;
}

// Shared manager-page chrome: PageHeader (same component used by the
// worker shell) + content stacked at 22px gaps. Keeping it thin here
// means every page respects the one-primary-action rule without
// reimplementing the bar.
export default function DeskPage({ title, sub, actions, overflow, children }: Props) {
  return (
    <>
      <PageHeader title={title} sub={sub} actions={actions} overflow={overflow} />
      <div className="desk__content">{children}</div>
    </>
  );
}
