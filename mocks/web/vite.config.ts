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
  return {
    name: "crewday:cache-bust-html",
    transformIndexHtml(html) {
      return html
        .replace(
          /(<script\b[^>]*\ssrc=")(\/[^"?#]+)(")/g,
          `$1$2?v=${nonce}$3`,
        )
        .replace(
          /(<link\b[^>]*\shref=")(\/[^"?#]+)(")/g,
          `$1$2?v=${nonce}$3`,
        );
    },
  };
}

// Vite dev server proxies the FastAPI backend so the SPA can hit
// /api/v1, /events, and the cookie-setting endpoints /switch,
// /theme/toggle, /agent/sidebar without CORS headaches.
const BACKEND =
  process.env.VITE_BACKEND_URL ?? "http://host.docker.internal:8100";

// Route prefixes that must pass through to FastAPI in dev; everything
// else is handled by Vite (and in prod, by the SPA catch-all).
const API_PATHS = [
  "/api",
  "/events",
  "/switch",
  "/theme",
  "/agent",
  "/healthz",
  "/readyz",
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
        navigateFallbackDenylist: [/^\/api/, /^\/events/],
        runtimeCaching: [
          {
            urlPattern: /\/api\/v1\/tasks.*$/,
            handler: "StaleWhileRevalidate",
            options: { cacheName: "tasks-cache" },
          },
        ],
      },
      manifest: {
        name: "crewday",
        short_name: "crewday",
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
  //   - publicly: https://dev.crewday.app → Traefik → web-dev:5173
  // Letting Vite pick HMR host/port from the page origin makes both
  // work without per-URL config: ws:// for 127.0.0.1:8100, wss://
  // for the public host (Traefik upgrades the websocket).
  // `allowedHosts` lets the public hostname through Vite's host-check.
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    allowedHosts: ["dev.crewday.app", "localhost", "127.0.0.1"],
    proxy: Object.fromEntries(
      API_PATHS.map((p) => [p, { target: BACKEND, changeOrigin: true, ws: true }]),
    ),
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
});
