import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { makeQueryClient } from "@/lib/queryClient";
import { RoleProvider } from "@/context/RoleContext";
import { ThemeProvider } from "@/context/ThemeContext";
import { SseProvider } from "@/context/SseContext";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { NavHistoryProvider } from "@/context/NavHistoryContext";
import { AuthProvider } from "@/auth";
import { I18nProvider } from "@/i18n";
import { startOfflineQueueReplay } from "@/lib/offlineQueue";

import "@/styles/tokens.css";
import "@/styles/reset.css";
import "@/styles/globals.css";

// Dev containers serve via Vite without a service worker, but users
// who visited earlier container builds (baked prod dist + VitePWA)
// may still have a SW from that origin cached in their browser.
// Proactively unregister it on boot and clear its caches so live
// changes from the Vite dev server aren't masked by stale bundles.
if (import.meta.env.DEV && "serviceWorker" in navigator) {
  void navigator.serviceWorker.getRegistrations().then((regs) => {
    for (const r of regs) void r.unregister();
  });
}
if (import.meta.env.DEV && "caches" in window) {
  void window.caches
    .keys()
    .then((keys) => Promise.all(keys.map((k) => window.caches.delete(k))));
}

if (typeof window !== "undefined") {
  startOfflineQueueReplay();
}

const queryClient = makeQueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <BrowserRouter>
          <AuthProvider>
            <NavHistoryProvider>
              <ThemeProvider>
                <RoleProvider>
                  <WorkspaceProvider>
                    <SseProvider>
                      <App />
                    </SseProvider>
                  </WorkspaceProvider>
                </RoleProvider>
              </ThemeProvider>
            </NavHistoryProvider>
          </AuthProvider>
        </BrowserRouter>
      </I18nProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
