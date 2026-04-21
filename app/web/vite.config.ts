import { defineConfig, type PluginOption } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";

// URL-based cache busting in dev: Vite adds ?t=<timestamp> to HMR
// updates, but the initial request for index.html references raw
// paths like `/src/main.tsx`. We stamp every local <script src> and
// <link href> with `?v=<nonce>` so the browser's HTTP cache treats
// each dev-server boot as a fresh set of URLs — no reliance on
// Cache-Control / ETag revalidation. External absolute URLs
// (https://fonts…) are left alone. Production builds already get
// content-hashed filenames from Rollup.
function cacheBustHtml(): PluginOption {
  const nonce = Date.now().toString(36);
  // Vite's dev middleware only substitutes `__HMR_CONFIG_NAME__` &
  // friends in `/@vite/client` when the URL has no extra query
  // string; appending `?v=` here would silently break HMR. Same for
  // the other `/@…` pseudo-paths Vite injects.
  const skip = /^\/@(vite|react-refresh|id|fs|vite-plugin-pwa)\b/;
  const stamp = (path: string) => (skip.test(path) ? path : `${path}?v=${nonce}`);
  return {
    name: "crewday:cache-bust-html",
    transformIndexHtml(html) {
      return html
        .replace(
          /(<script\b[^>]*\ssrc=")(\/[^"?#]+)(")/g,
          (_m, pre, path, post) => `${pre}${stamp(path)}${post}`,
        )
        .replace(
          /(<link\b[^>]*\shref=")(\/[^"?#]+)(")/g,
          (_m, pre, path, post) => `${pre}${stamp(path)}${post}`,
        );
    },
  };
}

// Vite dev server proxies the FastAPI backend so the SPA can hit
// /api/v1, /events, and the cookie-setting endpoints /switch,
// /theme/toggle, /agent/sidebar, /nav/sidebar without CORS headaches.
const BACKEND =
  process.env.VITE_BACKEND_URL ?? "http://host.docker.internal:8100";

// Mocks SPA — the sibling Vite container serving ``mocks/web/`` at
// ``/mocks/`` so operators can compare the production app against the
// disposable mocks page-for-page on the same origin. Production
// (dev.crew.day) routes this via Traefik with a higher-priority
// router; the loopback host port (127.0.0.1:8100) reuses the same
// path by forwarding ``/mocks/*`` over the compose network to the
// mocks Vite dev server. Unset in non-docker dev → the proxy entry
// drops out and ``/mocks/`` just 404s locally, which is fine.
const MOCKS_BACKEND = process.env.VITE_MOCKS_BACKEND_URL ?? null;

// Route prefixes that must pass through to FastAPI in dev; everything
// else is handled by Vite (and in prod, by the SPA catch-all). The
// `/admin/api` prefix covers /admin/api/v1/* deployment-admin routes
// (§12 "Admin surface"); /admin itself (without /api) is a SPA route
// and stays local.
//
// ``/w`` covers every workspace-scoped API path — per spec §12 "Base
// URL", workspace REST lives under ``/w/<slug>/api/v1/...`` and SSE
// under ``/w/<slug>/events``. ``src/lib/api.ts`` rewrites bare
// ``/api/v1/...`` calls into ``/w/<slug>/api/v1/...`` in the browser;
// without this proxy entry the rewritten path falls through to Vite's
// SPA catch-all and returns ``index.html`` instead of JSON.
// NOTE: `/w` is NOT a SPA route in dev — the SPA mounts workspace
// pages under other prefixes (e.g. ``/today``, ``/admin``). If a
// future SPA route needs the ``/w/`` prefix, narrow this to
// ``/w/:slug/api`` + ``/w/:slug/events`` and co-exist.
const API_PATHS = [
  "/api",
  "/admin/api",
  "/w",
  "/events",
  "/switch",
  "/theme",
  "/agent",
  "/nav/sidebar",
  "/healthz",
  "/readyz",
  "/version",
  "/metrics",
];

export default defineConfig({
  plugins: [
    cacheBustHtml(),
    react(),
    // The PWA plugin only emits a service worker for the production
    // build. Keeping `devOptions.enabled` off (the default) ensures
    // dev never installs a SW that could cache stale bundles and mask
    // HMR updates. `main.tsx` also unregisters any SW left over from
    // an earlier baked-dist build.
    VitePWA({
      registerType: "autoUpdate",
      strategies: "generateSW",
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [
          /^\/api/,
          /^\/admin\/api/,
          /^\/w\/[^/]+\/api/,
          /^\/w\/[^/]+\/events/,
          /^\/events/,
        ],
        runtimeCaching: [
          {
            urlPattern: /\/api\/v1\/tasks.*$/,
            handler: "StaleWhileRevalidate",
            options: { cacheName: "tasks-cache" },
          },
        ],
      },
      manifest: {
        name: "crew.day",
        short_name: "crew.day",
        theme_color: "#3F6E3B",
        background_color: "#FAF7F2",
        display: "standalone",
        start_url: "/",
      },
    }),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  // The dev server binds inside the Docker `web-dev` container on
  // 0.0.0.0 and is reached two ways:
  //   - locally: 127.0.0.1:8100 → container :5173 (port forward)
  //   - publicly: https://dev.crew.day → Traefik → web-dev:5173
  // Letting Vite pick HMR host/port from the page origin makes both
  // work without per-URL config: ws:// for 127.0.0.1:8100, wss://
  // for the public host (Traefik upgrades the websocket).
  // `allowedHosts` lets the public hostname through Vite's host-check.
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    allowedHosts: ["dev.crew.day", "localhost", "127.0.0.1"],
    proxy: {
      ...Object.fromEntries(
        API_PATHS.map((p) => [p, { target: BACKEND, changeOrigin: true, ws: true }]),
      ),
      // ``/mocks/*`` falls through to the sibling ``mocks-web-dev``
      // Vite container when ``VITE_MOCKS_BACKEND_URL`` is set (the
      // compose file wires this). Production routes the same prefix
      // via a higher-priority Traefik router, so the loopback (127.0.0.1:8100)
      // and the public host (dev.crew.day) behave the same. Mocks Vite
      // is configured with ``--base /mocks/`` so it emits asset URLs
      // carrying that prefix — no StripPrefix needed here either.
      ...(MOCKS_BACKEND
        ? {
            "/mocks": {
              target: MOCKS_BACKEND,
              changeOrigin: true,
              ws: true,
            },
          }
        : {}),
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // Function form — Rollup 4 (bundled inside Vite 8) rejects
        // the object-literal shape at the TypeScript level.
        // ``mocks/web/vite.config.ts`` uses the same shape.
        manualChunks(id: string): string | undefined {
          if (
            id.includes("node_modules/react-router-dom/") ||
            id.includes("node_modules/react-dom/") ||
            id.includes("node_modules/react/")
          ) {
            return "vendor";
          }
          if (id.includes("node_modules/@tanstack/react-query/")) {
            return "query";
          }
          return undefined;
        },
      },
    },
  },
});
