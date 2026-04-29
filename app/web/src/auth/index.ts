// crewday — auth module barrel.
//
// Re-exports the public surface so consumers import from `@/auth`
// instead of poking module-internal paths. The store / passkey
// helpers are exported individually too — `LoginPage` (cd-4z54) will
// reach for `runPasskeyLoginCeremony` and the typed errors directly,
// without going through `useAuth()`.

export { AuthProvider } from "./AuthProvider";
export { RequireAuth } from "./RequireAuth";
export { RequirePermission, ForbiddenPanel } from "./RequirePermission";
export { WorkspaceGate } from "./WorkspaceGate";
export {
  useAuth,
  useAuthBootstrap,
  PasskeyCancelledError,
  PasskeyUnsupportedError,
  __resetAuthStoreForTests,
} from "./useAuth";
export type { UseAuthApi } from "./useAuth";
export {
  getAuthState,
  setAuthenticated,
  setAuthToken,
  setLoading,
  setUnauthenticated,
  subscribeAuth,
} from "./authStore";
export type { AuthState } from "./authStore";
export {
  beginPasskeyLogin,
  finishPasskeyLogin,
  runPasskeyLoginCeremony,
  decodeRequestOptions,
  encodeAssertion,
} from "./passkey";
export { createOnUnauthorized, isAuthEndpoint, sanitizeNext } from "./onUnauthorized";
export type { AuthMe, PasskeyLoginFinish, PasskeyLoginStart } from "./types";
