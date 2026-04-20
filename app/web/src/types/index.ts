// crewday — JSON API types barrel.
//
// Shapes mirror the dataclasses in mocks/app/mock_data.py (and, once
// the production API is wired up, the FastAPI dataclasses). The
// FastAPI layer serializes via dataclasses.asdict, so dates arrive as
// ISO-8601 strings and enums as their literal string values.
//
// One file per bounded context; this barrel preserves every symbol
// the legacy `mocks/web/src/types/api.ts` exported. Prefer importing
// directly from the specific sub-module in new code; the `api.ts`
// shim re-exports this barrel so existing `from "@/types/api"`
// imports keep resolving.

export * from "./core";
export * from "./property";
export * from "./employee";
export * from "./task";
export * from "./booking";
export * from "./approval";
export * from "./billing";
export * from "./inventory";
export * from "./expense";
export * from "./asset";
export * from "./messaging";
export * from "./llm";
export * from "./auth";
export * from "./settings";
export * from "./me";
export * from "./admin";
export * from "./dashboard";
export * from "./sse";
