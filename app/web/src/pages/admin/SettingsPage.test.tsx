import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, useLocation } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import SettingsPage from "./SettingsPage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [path, responses] of Object.entries(scripted)) {
    queues[path] = [...responses];
  }
  const paths = Object.keys(queues).sort((a, b) => b.length - a.length);
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const pathname = new URL(resolved, "http://crewday.test").pathname;
    const path = paths.find((candidate) => pathname === candidate);
    if (!path) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[path]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(next.body),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/settings"]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function me(overrides: Record<string, unknown> = {}): unknown {
  return {
    user_id: "u1",
    display_name: "Ada Admin",
    email: "ada@example.test",
    is_owner: true,
    capabilities: {
      "deployment.audit:read": true,
      "deployment.settings:write": true,
    },
    ...overrides,
  };
}

function signup(overrides: Record<string, unknown> = {}): unknown {
  return {
    signup_enabled: true,
    signup_throttle_overrides: { per_ip_hour: 5 },
    signup_disposable_domains_path: "app/abuse/data/disposable_domains.txt",
    ...overrides,
  };
}

function settings(overrides: Record<string, unknown> = {}): unknown {
  return {
    settings: [
      {
        key: "signup_throttle_overrides",
        value: { per_ip_hour: 5 },
        kind: "json",
        description: "Override the per-IP / per-email signup throttles.",
        root_only: false,
        updated_at: "2026-04-28T10:00:00Z",
        updated_by: "u1",
      },
      {
        key: "captcha_required",
        value: false,
        kind: "bool",
        description: "Require Turnstile CAPTCHA on the self-serve signup form.",
        root_only: false,
        updated_at: "2026-04-28T10:00:00Z",
        updated_by: "u1",
      },
      {
        key: "llm_default_budget_cents_30d",
        value: 2500,
        kind: "int",
        description: "Default rolling 30-day LLM spend cap per workspace, in cents.",
        root_only: false,
        updated_at: "",
        updated_by: "",
      },
      {
        key: "trusted_interfaces",
        value: ["tailscale0"],
        kind: "json",
        description: "Trusted network interfaces.",
        root_only: true,
        updated_at: "",
        updated_by: "",
      },
    ],
    ...overrides,
  };
}

function installPageFetch(overrides: {
  me?: unknown;
  signup?: unknown;
  settings?: unknown;
  extra?: Record<string, FakeResponse[]>;
} = {}) {
  return installFetch({
    "/admin/api/v1/me": [{ body: overrides.me ?? me() }],
    "/admin/api/v1/signup/settings": [{ body: overrides.signup ?? signup() }],
    "/admin/api/v1/settings": [{ body: overrides.settings ?? settings() }],
    ...(overrides.extra ?? {}),
  });
}

function rowFor(text: string): HTMLTableRowElement {
  const row = screen.getByText(text).closest("tr");
  if (!(row instanceof HTMLTableRowElement)) throw new Error(`No row for ${text}`);
  return row;
}

function jsonBody(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.doUnmock("@/auth");
  vi.doUnmock("@/layouts/PreviewShell");
  vi.doUnmock("@/layouts/PublicLayout");
  vi.doUnmock("@/layouts/EmployeeLayout");
  vi.doUnmock("@/layouts/ManagerLayout");
  vi.doUnmock("@/layouts/ClientLayout");
  vi.doUnmock("@/layouts/AdminLayout");
  vi.doUnmock("@/pages/admin/SettingsPage");
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin SettingsPage", () => {
  it("renders real settings envelopes, signup settings, and embedded capabilities", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Visitor signup")).toBeInTheDocument();
      expect(screen.getByDisplayValue("app/abuse/data/disposable_domains.txt")).toBeInTheDocument();
      expect(screen.getByLabelText("Throttle overrides")).toHaveValue('{\n  "per_ip_hour": 5\n}');
      expect(screen.queryByDisplayValue("[object Object]")).not.toBeInTheDocument();
      expect(screen.getByText("deployment.audit:read")).toBeInTheDocument();
      expect(screen.getByText("deployment.settings:write")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("saves and discards signup settings using the real API field names", async () => {
    const fetcher = installPageFetch({
      extra: {
        "/admin/api/v1/signup/settings": [
          { body: signup() },
          {
            body: signup({
              signup_enabled: false,
              signup_disposable_domains_path: "/srv/blocklist.txt",
              signup_throttle_overrides: { per_ip_hour: 9 },
            }),
          },
        ],
      },
    });
    try {
      render(<Harness />);
      await screen.findByText("Visitor signup");

      fireEvent.click(screen.getByLabelText("Anyone can create a workspace via /signup."));
      fireEvent.change(screen.getByDisplayValue("app/abuse/data/disposable_domains.txt"), {
        target: { value: "/tmp/discarded.txt" },
      });
      fireEvent.click(screen.getAllByRole("button", { name: "Discard" })[0]!);
      expect(screen.getByDisplayValue("app/abuse/data/disposable_domains.txt")).toBeInTheDocument();

      fireEvent.click(screen.getByLabelText("Anyone can create a workspace via /signup."));
      fireEvent.change(screen.getByDisplayValue("app/abuse/data/disposable_domains.txt"), {
        target: { value: "/srv/blocklist.txt" },
      });
      fireEvent.change(screen.getByLabelText("Throttle overrides"), {
        target: { value: '{ "per_ip_hour": 9 }' },
      });
      fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]!);

      await waitFor(() => {
        expect(fetcher.calls.some((call) => call.init.method === "PUT")).toBe(true);
      });
      const put = fetcher.calls.find((call) => call.url === "/admin/api/v1/signup/settings" && call.init.method === "PUT");
      expect(put).toBeDefined();
      expect(jsonBody(put!)).toEqual({
        signup_enabled: false,
        signup_disposable_domains_path: "/srv/blocklist.txt",
        signup_throttle_overrides: { per_ip_hour: 9 },
      });
    } finally {
      fetcher.restore();
    }
  });

  it("shows signup save errors without discarding the edited values", async () => {
    const fetcher = installPageFetch({
      extra: {
        "/admin/api/v1/signup/settings": [
          { body: signup() },
          { status: 500, body: { detail: "boom" } },
        ],
      },
    });
    try {
      render(<Harness />);
      await screen.findByText("Visitor signup");

      fireEvent.change(screen.getByDisplayValue("app/abuse/data/disposable_domains.txt"), {
        target: { value: "/srv/broken.txt" },
      });
      fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]!);

      expect(await screen.findByText("Could not save signup settings.")).toBeInTheDocument();
      expect(screen.getByDisplayValue("/srv/broken.txt")).toBeInTheDocument();
      const put = fetcher.calls.find((call) => call.url === "/admin/api/v1/signup/settings" && call.init.method === "PUT");
      expect(jsonBody(put!)).toEqual({
        signup_disposable_domains_path: "/srv/broken.txt",
      });
    } finally {
      fetcher.restore();
    }
  });

  it("optimistically updates deployment settings and rolls back on failure", async () => {
    const fetcher = installPageFetch({
      extra: {
        "/admin/api/v1/settings/captcha_required": [
          { status: 500, body: { detail: "boom" } },
        ],
        "/admin/api/v1/settings": [
          { body: settings() },
          { body: settings() },
        ],
      },
    });
    try {
      render(<Harness />);
      const row = await waitFor(() => rowFor("captcha_required"));
      const checkbox = within(row).getByRole("checkbox");

      fireEvent.click(checkbox);
      expect(checkbox).toBeChecked();
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

      await screen.findByText("Could not save changes.");
      expect(within(rowFor("captcha_required")).getByRole("checkbox")).not.toBeChecked();
      const put = fetcher.calls.find((call) => call.url === "/admin/api/v1/settings/captcha_required");
      expect(put?.init.method).toBe("PUT");
      expect(jsonBody(put!)).toEqual({ value: true });
    } finally {
      fetcher.restore();
    }
  });

  it("clears attempted rows but keeps unattempted deployment drafts after a partial save failure", async () => {
    const savedSettings = settings({
      settings: [
        {
          key: "signup_throttle_overrides",
          value: { per_ip_hour: 8 },
          kind: "json",
          description: "Override the per-IP / per-email signup throttles.",
          root_only: false,
          updated_at: "2026-04-28T10:15:00Z",
          updated_by: "u1",
        },
        {
          key: "captcha_required",
          value: false,
          kind: "bool",
          description: "Require Turnstile CAPTCHA on the self-serve signup form.",
          root_only: false,
          updated_at: "2026-04-28T10:00:00Z",
          updated_by: "u1",
        },
        {
          key: "llm_default_budget_cents_30d",
          value: 2500,
          kind: "int",
          description: "Default rolling 30-day LLM spend cap per workspace, in cents.",
          root_only: false,
          updated_at: "",
          updated_by: "",
        },
        {
          key: "trusted_interfaces",
          value: ["tailscale0"],
          kind: "json",
          description: "Trusted network interfaces.",
          root_only: true,
          updated_at: "",
          updated_by: "",
        },
      ],
    });
    const fetcher = installPageFetch({
      extra: {
        "/admin/api/v1/settings/signup_throttle_overrides": [
          {
            body: {
              key: "signup_throttle_overrides",
              value: { per_ip_hour: 8 },
              kind: "json",
              description: "Override the per-IP / per-email signup throttles.",
              root_only: false,
              updated_at: "2026-04-28T10:15:00Z",
              updated_by: "u1",
            },
          },
        ],
        "/admin/api/v1/settings/captcha_required": [
          { status: 500, body: { detail: "boom" } },
        ],
        "/admin/api/v1/settings": [
          { body: settings() },
          { body: savedSettings },
          { body: savedSettings },
        ],
      },
    });
    try {
      render(<Harness />);
      const throttleRow = await waitFor(() => rowFor("signup_throttle_overrides"));
      fireEvent.change(within(throttleRow).getByRole("textbox"), {
        target: { value: '{ "per_ip_hour": 8 }' },
      });
      const captchaRow = await waitFor(() => rowFor("captcha_required"));
      const budgetRow = rowFor("llm_default_budget_cents_30d");

      fireEvent.click(within(captchaRow).getByRole("checkbox"));
      fireEvent.change(within(budgetRow).getByRole("spinbutton"), {
        target: { value: "3000" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

      expect(await screen.findByText("Could not save changes.")).toBeInTheDocument();
      expect(within(rowFor("captcha_required")).getByRole("checkbox")).not.toBeChecked();
      expect(within(rowFor("llm_default_budget_cents_30d")).getByRole("spinbutton")).toHaveValue(3000);
      expect(fetcher.calls.some((call) => call.url === "/admin/api/v1/settings/llm_default_budget_cents_30d")).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("parses JSON deployment setting edits before writing", async () => {
    const fetcher = installPageFetch({
      extra: {
        "/admin/api/v1/settings/signup_throttle_overrides": [
          {
            body: {
              key: "signup_throttle_overrides",
              value: { per_ip_hour: 12, per_email_lifetime: 3 },
              kind: "json",
              description: "Override the per-IP / per-email signup throttles.",
              root_only: false,
              updated_at: "2026-04-28T10:10:00Z",
              updated_by: "u1",
            },
          },
        ],
        "/admin/api/v1/settings": [
          { body: settings() },
          {
            body: settings({
              settings: [
                {
                  key: "signup_throttle_overrides",
                  value: { per_ip_hour: 12, per_email_lifetime: 3 },
                  kind: "json",
                  description: "Override the per-IP / per-email signup throttles.",
                  root_only: false,
                  updated_at: "2026-04-28T10:10:00Z",
                  updated_by: "u1",
                },
              ],
            }),
          },
        ],
      },
    });
    try {
      render(<Harness />);
      const row = await waitFor(() => rowFor("signup_throttle_overrides"));
      fireEvent.change(within(row).getByRole("textbox"), {
        target: { value: '{ "per_ip_hour": 12, "per_email_lifetime": 3 }' },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

      await waitFor(() => {
        expect(fetcher.calls.some((call) => call.url === "/admin/api/v1/settings/signup_throttle_overrides")).toBe(true);
      });
      const put = fetcher.calls.find((call) => call.url === "/admin/api/v1/settings/signup_throttle_overrides");
      expect(jsonBody(put!)).toEqual({
        value: { per_ip_hour: 12, per_email_lifetime: 3 },
      });
    } finally {
      fetcher.restore();
    }
  });

  it("disables root-only rows for non-owner admins", async () => {
    const fetcher = installPageFetch({ me: me({ is_owner: false }) });
    try {
      render(<Harness />);
      const row = await waitFor(() => rowFor("trusted_interfaces"));
      expect(within(row).getByRole("textbox")).toBeDisabled();
      expect(within(row).getByText("owners-only")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("renders the failure state when admin settings cannot load", async () => {
    const fetcher = installFetch({
      "/admin/api/v1/me": [{ body: me() }],
      "/admin/api/v1/signup/settings": [{ body: signup() }],
      "/admin/api/v1/settings": [{ status: 500, body: { detail: "nope" } }],
    });
    try {
      render(<Harness />);
      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("redirects /admin/signup to /admin/settings#signup", async () => {
    vi.resetModules();
    vi.doMock("@/auth", () => ({
      RequireAuth: () => <Outlet />,
      WorkspaceGate: () => <Outlet />,
    }));
    vi.doMock("@/context/RoleContext", () => ({
      useRole: () => ({ role: "manager", setRole: vi.fn() }),
    }));
    const layout = () => ({ default: () => <Outlet /> });
    vi.doMock("@/layouts/PreviewShell", layout);
    vi.doMock("@/layouts/PublicLayout", layout);
    vi.doMock("@/layouts/EmployeeLayout", layout);
    vi.doMock("@/layouts/ManagerLayout", layout);
    vi.doMock("@/layouts/ClientLayout", layout);
    vi.doMock("@/layouts/AdminLayout", layout);
    vi.doMock("@/pages/admin/SettingsPage", () => ({
      default: () => {
        const location = useLocation();
        return <span data-testid="admin-location">{location.pathname + location.hash}</span>;
      },
    }));
    const { default: App } = await import("@/App");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/admin/signup"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByTestId("admin-location")).toHaveTextContent("/admin/settings#signup");
  });
});
