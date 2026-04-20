import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PasskeyCancelledError,
  PasskeyUnsupportedError,
  beginPasskeyLogin,
  decodeRequestOptions,
  encodeAssertion,
  finishPasskeyLogin,
  runPasskeyLoginCeremony,
} from "./passkey";
import { __resetApiProvidersForTests } from "@/lib/api";

// Helpers ----------------------------------------------------------

function bytes(...vals: number[]): ArrayBuffer {
  return new Uint8Array(vals).buffer;
}

function bufToB64Url(buf: ArrayBuffer): string {
  const bytesArr = new Uint8Array(buf);
  let s = "";
  for (const b of bytesArr) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

interface FakeResponse {
  status: number;
  body: unknown;
}

function installFetch(responses: FakeResponse[]): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error(`Unexpected fetch call: ${resolved}`);
    const ok = next.status >= 200 && next.status < 300;
    const text = next.body === undefined ? "" : JSON.stringify(next.body);
    return {
      ok,
      status: next.status,
      statusText: ok ? "OK" : "Error",
      text: async () => text,
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

beforeEach(() => {
  __resetApiProvidersForTests();
});

afterEach(() => {
  __resetApiProvidersForTests();
  vi.unstubAllGlobals();
});

describe("decodeRequestOptions — fallback path (no native parse helper)", () => {
  beforeEach(() => {
    // Ensure the fast path is unavailable so we exercise the manual
    // decoder. jsdom omits `PublicKeyCredential` entirely; the explicit
    // delete keeps the test honest if a future polyfill leaks one in.
    vi.stubGlobal("PublicKeyCredential", undefined);
  });

  it("decodes the challenge from base64url to ArrayBuffer", () => {
    const decoded = decodeRequestOptions({ challenge: "AQID" /* 0x01 0x02 0x03 */ });
    expect(decoded.challenge).toBeInstanceOf(ArrayBuffer);
    const view = new Uint8Array(decoded.challenge as ArrayBuffer);
    expect([...view]).toEqual([1, 2, 3]);
  });

  it("decodes allowCredentials[].id and preserves transports", () => {
    const decoded = decodeRequestOptions({
      challenge: "AQID",
      allowCredentials: [
        { id: "AQID", type: "public-key", transports: ["internal", "hybrid"] },
      ],
    });
    const list = decoded.allowCredentials!;
    expect(list).toHaveLength(1);
    const item = list[0]!;
    expect(item.type).toBe("public-key");
    expect(item.transports).toEqual(["internal", "hybrid"]);
    expect(new Uint8Array(item.id as ArrayBuffer)).toEqual(new Uint8Array([1, 2, 3]));
  });

  it("passes through rpId, timeout, and userVerification", () => {
    const decoded = decodeRequestOptions({
      challenge: "AQID",
      rpId: "crew.day",
      timeout: 60_000,
      userVerification: "required",
    });
    expect(decoded.rpId).toBe("crew.day");
    expect(decoded.timeout).toBe(60_000);
    expect(decoded.userVerification).toBe("required");
  });

  it("tolerates a server that emits padded base64 instead of base64url", () => {
    // `+`, `/`, and `=` should still decode — server-rollouts that
    // forget to switch to base64url shouldn't crash the SPA.
    const decoded = decodeRequestOptions({ challenge: "+/8=" });
    const view = new Uint8Array(decoded.challenge as ArrayBuffer);
    expect([...view]).toEqual([0xfb, 0xff]);
  });
});

describe("encodeAssertion", () => {
  it("base64url-encodes every BufferSource and forwards the user handle when present", () => {
    // jsdom doesn't define `PublicKeyCredential` — `encodeAssertion`
    // doesn't `instanceof`-check, it just reads the documented fields,
    // so a duck-typed object is enough for the encoder unit test.
    const credential = {
      id: "test-id",
      rawId: bytes(0xaa, 0xbb),
      type: "public-key",
      response: {
        authenticatorData: bytes(0x01),
        clientDataJSON: bytes(0x02),
        signature: bytes(0x03),
        userHandle: bytes(0x04),
      },
      authenticatorAttachment: "platform" as const,
      getClientExtensionResults: () => ({}),
    } as unknown as PublicKeyCredential;

    const encoded = encodeAssertion(credential);
    expect(encoded.id).toBe("test-id");
    expect(encoded.rawId).toBe(bufToB64Url(bytes(0xaa, 0xbb)));
    expect(encoded.response.authenticatorData).toBe(bufToB64Url(bytes(0x01)));
    expect(encoded.response.clientDataJSON).toBe(bufToB64Url(bytes(0x02)));
    expect(encoded.response.signature).toBe(bufToB64Url(bytes(0x03)));
    expect(encoded.response.userHandle).toBe(bufToB64Url(bytes(0x04)));
    expect(encoded.authenticatorAttachment).toBe("platform");
  });

  it("emits userHandle = null when the authenticator omits it", () => {
    const credential = {
      id: "no-handle",
      rawId: bytes(0xaa),
      type: "public-key",
      response: {
        authenticatorData: bytes(0x01),
        clientDataJSON: bytes(0x02),
        signature: bytes(0x03),
        userHandle: new ArrayBuffer(0),
      },
      getClientExtensionResults: () => ({}),
    } as unknown as PublicKeyCredential;
    const encoded = encodeAssertion(credential);
    expect(encoded.response.userHandle).toBeNull();
  });

  it("forwards extension results when the authenticator returns any", () => {
    const credential = {
      id: "with-ext",
      rawId: bytes(0xaa),
      type: "public-key",
      response: {
        authenticatorData: bytes(0x01),
        clientDataJSON: bytes(0x02),
        signature: bytes(0x03),
        userHandle: new ArrayBuffer(0),
      },
      getClientExtensionResults: () => ({ appid: true } as Record<string, unknown>),
    } as unknown as PublicKeyCredential;
    const encoded = encodeAssertion(credential);
    expect(encoded.clientExtensionResults).toEqual({ appid: true });
  });
});

describe("beginPasskeyLogin / finishPasskeyLogin", () => {
  it("POSTs an empty body to /api/v1/auth/passkey/login/start and returns the challenge envelope", async () => {
    const { calls, restore } = installFetch([
      { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
    ]);
    try {
      const out = await beginPasskeyLogin();
      expect(out.challenge_id).toBe("ch_1");
      expect(calls[0]!.url).toBe("/api/v1/auth/passkey/login/start");
      expect(calls[0]!.init.method).toBe("POST");
    } finally {
      restore();
    }
  });

  it("finishPasskeyLogin POSTs the assertion and returns the user_id", async () => {
    const { calls, restore } = installFetch([{ status: 200, body: { user_id: "01HZ_USER" } }]);
    try {
      const out = await finishPasskeyLogin("ch_1", {
        id: "x",
        rawId: "x",
        type: "public-key",
        response: { authenticatorData: "a", clientDataJSON: "c", signature: "s", userHandle: null },
      });
      expect(out.user_id).toBe("01HZ_USER");
      expect(calls[0]!.url).toBe("/api/v1/auth/passkey/login/finish");
      const body = JSON.parse(calls[0]!.init.body as string) as { challenge_id: string };
      expect(body.challenge_id).toBe("ch_1");
    } finally {
      restore();
    }
  });
});

describe("runPasskeyLoginCeremony — error mapping", () => {
  it("throws PasskeyUnsupportedError when navigator.credentials is missing", async () => {
    const orig = navigator.credentials;
    Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
    try {
      await expect(runPasskeyLoginCeremony()).rejects.toBeInstanceOf(PasskeyUnsupportedError);
    } finally {
      Object.defineProperty(navigator, "credentials", { value: orig, configurable: true });
    }
  });

  it("translates DOMException(NotAllowedError) from navigator.credentials.get into PasskeyCancelledError", async () => {
    const { restore } = installFetch([
      { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
    ]);
    const fakeCreds = {
      get: vi.fn(async () => {
        throw new DOMException("user cancelled", "NotAllowedError");
      }),
    };
    Object.defineProperty(navigator, "credentials", { value: fakeCreds, configurable: true });
    try {
      await expect(runPasskeyLoginCeremony()).rejects.toBeInstanceOf(PasskeyCancelledError);
    } finally {
      restore();
      Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
    }
  });

  it("translates DOMException(SecurityError) into PasskeyUnsupportedError", async () => {
    const { restore } = installFetch([
      { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
    ]);
    const fakeCreds = {
      get: vi.fn(async () => {
        throw new DOMException("insecure context", "SecurityError");
      }),
    };
    Object.defineProperty(navigator, "credentials", { value: fakeCreds, configurable: true });
    try {
      await expect(runPasskeyLoginCeremony()).rejects.toBeInstanceOf(PasskeyUnsupportedError);
    } finally {
      restore();
      Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
    }
  });
});
