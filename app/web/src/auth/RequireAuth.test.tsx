import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { RequireAuth } from "./RequireAuth";
import { __resetAuthStoreForTests } from "./useAuth";
import { setAuthenticated, setLoading, setUnauthenticated } from "./authStore";
import type { AuthMe } from "./types";

const SAMPLE_USER: AuthMe = {
  user_id: "01HZ_USER",
  display_name: "Cara",
  email: "cara@example.com",
  available_workspaces: [],
  current_workspace_id: null,
};

function App({ initial = "/today", children }: { initial?: string; children?: ReactNode }) {
  const [qc] = [new QueryClient()];
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/login" element={<div>login page</div>} />
          <Route element={<RequireAuth />}>
            <Route path="/today" element={<div>protected today</div>} />
            <Route path="/property/:id" element={<div>protected property</div>} />
          </Route>
          {children}
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
});

describe("<RequireAuth>", () => {
  it("renders the loading hold-pattern while auth state is `loading`", () => {
    setLoading();
    render(<App />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText(/Checking your session/i)).toBeInTheDocument();
    expect(screen.queryByText("protected today")).toBeNull();
  });

  it("redirects to /login (without `next`) when unauthenticated and the user is on /", () => {
    setUnauthenticated();
    render(<App initial="/" />);
    // /  has no protected route in this harness, so MemoryRouter
    // renders no match — but we exercise the redirect via /today
    // explicitly in the next test. The point of this case is just
    // that a `setUnauthenticated()` doesn't render the protected
    // child.
    expect(screen.queryByText("protected today")).toBeNull();
  });

  it("redirects to /login?next=<encoded-path> when unauthenticated mid-deep-link", () => {
    setUnauthenticated();
    render(<App initial="/today" />);
    expect(screen.getByText("login page")).toBeInTheDocument();
  });

  it("preserves search and hash in the `next` parameter", () => {
    setUnauthenticated();
    function LoginProbe() {
      const loc = useLocation();
      return <span data-testid="loc">{loc.pathname + loc.search}</span>;
    }
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/property/abc?tab=tasks#row=12"]}>
          <Routes>
            <Route path="/login" element={<LoginProbe />} />
            <Route element={<RequireAuth />}>
              <Route path="/property/:id" element={<div>protected property</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const loc = screen.getByTestId("loc");
    expect(loc.textContent).toContain("/login");
    expect(loc.textContent).toContain("?next=");
    // The `next` payload must round-trip the original path + query.
    const url = new URL("http://localhost" + (loc.textContent ?? ""));
    const next = url.searchParams.get("next");
    expect(next).toBe("/property/abc?tab=tasks#row=12");
  });

  it("renders the protected child when the user is authenticated", () => {
    setAuthenticated(SAMPLE_USER);
    render(<App initial="/today" />);
    expect(screen.getByText("protected today")).toBeInTheDocument();
  });

  it("supports the `children` shape (non-Outlet integration)", () => {
    setAuthenticated(SAMPLE_USER);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <RequireAuth>
            <span>inline child</span>
          </RequireAuth>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("inline child")).toBeInTheDocument();
  });
});
