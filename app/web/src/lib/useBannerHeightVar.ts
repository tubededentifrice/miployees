// PLACEHOLDER — real impl lands with the PreviewShell + AgentSidebar
// follow-ups (cd-k69n). DO NOT USE FOR PRODUCTION DECISIONS.
//
// Sets the `--banner-h` custom property so `.phone--chat` can size
// against `100dvh - banner`. Real impl mirrors
// `mocks/web/src/lib/useBannerHeightVar.ts`.
import { useEffect } from "react";

export function useBannerHeightVar(): void {
  useEffect(() => {
    const sync = (): void => {
      const banner = document.querySelector(".preview-banner");
      if (!banner) return;
      const h = banner.getBoundingClientRect().height;
      document.documentElement.style.setProperty("--banner-h", h + "px");
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
}
