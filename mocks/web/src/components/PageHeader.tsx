import { type ReactNode, useCallback, useEffect, useRef } from "react";
import { Link, useLocation } from "react-router-dom";
import { ChevronLeft, Menu, MoreHorizontal } from "lucide-react";
import { useShellNav } from "@/context/ShellNavContext";
import { resolveParent, type ParentDescriptor } from "@/lib/routeParents";

export interface PageHeaderOverflowItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  destructive?: boolean;
}

interface Props {
  title: ReactNode;
  sub?: ReactNode;
  /** Single primary trailing action (button / link). Anything beyond one
   *  action goes in `overflow` per §14 "Page header". */
  actions?: ReactNode;
  overflow?: PageHeaderOverflowItem[];
  /** Explicit parent for the back button. Pass `false` to suppress the
   *  route-derived default on a sub-page that would otherwise pick one up. */
  back?: ParentDescriptor | false;
}

// §14 "Page header". Three slots: leading (back OR hamburger), heading
// (title + optional sub), trailing (one primary action + overflow).
// Sticky on phone with safe-area inset; large Fraunces title on
// desktop. Back parent auto-derived from `routeParents.ts`.
export default function PageHeader({ title, sub, actions, overflow, back }: Props) {
  const { pathname } = useLocation();
  const shellNav = useShellNav();

  const resolved: ParentDescriptor | null =
    back === false
      ? null
      : back && typeof back === "object"
        ? back
        : resolveParent(pathname);

  // Back wins the leading slot over the hamburger: once the user is on
  // a sub-page, "take me back" is more useful than "open the drawer".
  const showHamburger = !resolved && Boolean(shellNav?.hasDrawer);

  const hasOverflow = Boolean(overflow && overflow.length > 0);

  return (
    <header className="page-topbar">
      <div className="page-topbar__leading">
        {resolved && (
          <Link
            to={resolved.to}
            className="page-topbar__icon-btn"
            aria-label={`Back to ${resolved.label}`}
          >
            <ChevronLeft size={22} strokeWidth={2} aria-hidden="true" />
          </Link>
        )}
        {showHamburger && shellNav && (
          <button
            type="button"
            className="page-topbar__icon-btn page-topbar__menu-btn"
            onClick={shellNav.toggle}
            aria-label={shellNav.isOpen ? "Close menu" : "Open menu"}
            aria-expanded={shellNav.isOpen}
          >
            <Menu size={22} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="page-topbar__heading">
        <h1 className="page-title">{title}</h1>
        {sub ? <p className="page-sub">{sub}</p> : null}
      </div>
      <div className="page-topbar__trailing">
        {actions}
        {hasOverflow ? <OverflowMenu items={overflow!} /> : null}
      </div>
    </header>
  );
}

function OverflowMenu({ items }: { items: PageHeaderOverflowItem[] }) {
  const ref = useRef<HTMLDialogElement>(null);

  const open = useCallback(() => ref.current?.showModal(), []);
  const close = useCallback(() => ref.current?.close(), []);

  // Close on outside-click within the dialog's scrim.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onClick = (e: MouseEvent) => {
      if (e.target === el) el.close();
    };
    el.addEventListener("click", onClick);
    return () => el.removeEventListener("click", onClick);
  }, []);

  return (
    <>
      <button
        type="button"
        className="page-topbar__icon-btn"
        onClick={open}
        aria-label="More actions"
        aria-haspopup="menu"
      >
        <MoreHorizontal size={22} strokeWidth={2} aria-hidden="true" />
      </button>
      <dialog ref={ref} className="overflow-menu" aria-label="More actions">
        <ul className="overflow-menu__list" role="menu">
          {items.map((it, idx) => (
            <li key={idx} role="none">
              <button
                type="button"
                role="menuitem"
                className={
                  "overflow-menu__item" +
                  (it.destructive ? " overflow-menu__item--destructive" : "")
                }
                onClick={() => {
                  close();
                  it.onSelect();
                }}
              >
                {it.icon ? <span className="overflow-menu__icon" aria-hidden="true">{it.icon}</span> : null}
                <span className="overflow-menu__label">{it.label}</span>
              </button>
            </li>
          ))}
        </ul>
      </dialog>
    </>
  );
}
