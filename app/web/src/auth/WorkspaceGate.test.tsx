import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { WorkspaceGate } from "./WorkspaceGate";
import { __resetAuthStoreForTests } from "./useAuth";
import { setAuthenticated } from "./authStore";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { AuthMe } from "./types";

function makeUser(workspaces: AuthMe["available_workspaces"], current: string | null = null): AuthMe {
  return {
    user_id: "01HZ_USER",
    display_name: "Dee",
    email: "dee@example.com",
    available_workspaces: workspaces,
    current_workspace_id: current,
  };
}

function App({ children }: { children: ReactNode }) {
  const qc = new QueryClient();
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          {children}
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<WorkspaceGate>", () => {
  it("renders the chooser when authenticated with multiple workspaces and no slug", () => {
    setAuthenticated(makeUser([
      {
        workspace: { id: "ws_a", name: "Acme", timezone: "UTC", default_currency: "USD", default_country: "US", default_locale: "en" },
        grant_role: "manager",
        binding_org_id: null,
        source: "workspace_grant",
      },
      {
        workspace: { id: "ws_b", name: "Beta Co", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
        grant_role: "worker",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ]));

    render(
      <App>
        <WorkspaceGate>
          <div>protected tree</div>
        </WorkspaceGate>
      </App>,
    );

    expect(screen.getByText(/Pick a workspace/i)).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Beta Co")).toBeInTheDocument();
    // Children stay hidden until a slug is picked.
    expect(screen.queryByText("protected tree")).toBeNull();
  });

  it("auto-adopts the only workspace and renders the protected tree", () => {
    setAuthenticated(makeUser([
      {
        workspace: { id: "ws_only", name: "Solo", timezone: "UTC", default_currency: "USD", default_country: "US", default_locale: "en" },
        grant_role: "manager",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ]));

    render(
      <App>
        <WorkspaceGate>
          <div>protected tree</div>
        </WorkspaceGate>
      </App>,
    );

    expect(screen.getByText("protected tree")).toBeInTheDocument();
    // The chooser must NOT render even momentarily for single-workspace
    // users — the auto-adopt fires synchronously in the same effect.
    expect(screen.queryByText(/Pick a workspace/i)).toBeNull();
  });

  it("adopts the server-supplied current_workspace_id without showing the chooser", () => {
    setAuthenticated(makeUser(
      [
        {
          workspace: { id: "ws_a", name: "Acme", timezone: "UTC", default_currency: "USD", default_country: "US", default_locale: "en" },
          grant_role: "manager",
          binding_org_id: null,
          source: "workspace_grant",
        },
        {
          workspace: { id: "ws_b", name: "Beta Co", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
          grant_role: "worker",
          binding_org_id: null,
          source: "workspace_grant",
        },
      ],
      "ws_b",
    ));

    render(
      <App>
        <WorkspaceGate>
          <div>protected tree</div>
        </WorkspaceGate>
      </App>,
    );

    // Server already picked ws_b — adopt it silently, no chooser.
    expect(screen.getByText("protected tree")).toBeInTheDocument();
    expect(screen.queryByText(/Pick a workspace/i)).toBeNull();
  });

  it("renders the no-workspaces empty state when the user has no grants", () => {
    setAuthenticated(makeUser([]));
    render(
      <App>
        <WorkspaceGate>
          <div>protected tree</div>
        </WorkspaceGate>
      </App>,
    );
    expect(screen.getByText(/No workspaces yet/i)).toBeInTheDocument();
    expect(screen.queryByText("protected tree")).toBeNull();
  });

  it("auto-focuses the first pickable button so keyboard users land inside the dialog", () => {
    setAuthenticated(makeUser([
      {
        workspace: { id: "ws_a", name: "Acme", timezone: "UTC", default_currency: "USD", default_country: "US", default_locale: "en" },
        grant_role: "manager",
        binding_org_id: null,
        source: "workspace_grant",
      },
      {
        workspace: { id: "ws_b", name: "Beta Co", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
        grant_role: "worker",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ]));

    render(
      <App>
        <WorkspaceGate>
          <div>protected tree</div>
        </WorkspaceGate>
      </App>,
    );

    // After mount, the first workspace pick is the active element.
    // `role="dialog"` + `aria-modal="true"` alone don't trap focus;
    // the auto-focus is what keeps keyboard users from landing on
    // chrome behind the backdrop.
    const firstPick = screen.getByText("Acme").closest("button");
    expect(firstPick).toBeTruthy();
    expect(document.activeElement).toBe(firstPick);
  });

  it("auto-focuses the sign-out action in the no-workspaces empty state", () => {
    setAuthenticated(makeUser([]));
    render(
      <App>
        <WorkspaceGate>
          <div>protected tree</div>
        </WorkspaceGate>
      </App>,
    );
    const signOut = screen.getByText("Sign out");
    expect(document.activeElement).toBe(signOut);
  });

  it("picking a workspace from the chooser commits the slug and reveals the protected tree", () => {
    setAuthenticated(makeUser([
      {
        workspace: { id: "ws_a", name: "Acme", timezone: "UTC", default_currency: "USD", default_country: "US", default_locale: "en" },
        grant_role: "manager",
        binding_org_id: null,
        source: "workspace_grant",
      },
      {
        workspace: { id: "ws_b", name: "Beta Co", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
        grant_role: "worker",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ]));

    render(
      <App>
        <Routes>
          <Route element={<WorkspaceGate />}>
            <Route path="/" element={<div>protected tree</div>} />
          </Route>
        </Routes>
      </App>,
    );

    expect(screen.queryByText("protected tree")).toBeNull();
    act(() => {
      screen.getByText("Beta Co").closest("button")!.click();
    });
    expect(screen.getByText("protected tree")).toBeInTheDocument();
  });
});
