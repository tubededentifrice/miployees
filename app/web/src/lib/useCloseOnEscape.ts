import { useEffect } from "react";

// Universal Escape-to-close for scrim-backed drawers across the app
// (§14 Web). Native <dialog> already handles Escape via the browser;
// this hook exists for the custom aside + scrim drawers that don't.
//
// Stacks naturally: the most-recently-mounted drawer registers its
// listener last, and `window.addEventListener` fires in registration
// order. We stop propagation after closing so a nested drawer doesn't
// also close its parent on the same keystroke.
//
// `active` lets the caller gate the listener (e.g. only when the
// drawer is actually rendered), but defaults to true so the common
// case "mount = active" stays a one-liner.
export function useCloseOnEscape(
  onClose: () => void,
  active: boolean = true,
): void {
  useEffect(() => {
    if (!active) return;
    function handler(ev: KeyboardEvent): void {
      if (ev.key !== "Escape" && ev.key !== "Esc") return;
      // If the event originated inside a native <dialog>, let the
      // browser close that instead of intercepting here.
      const target = ev.target as HTMLElement | null;
      if (target?.closest?.("dialog[open]")) return;
      ev.stopPropagation();
      onClose();
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose, active]);
}
