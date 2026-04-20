import { describe, it, expect } from "vitest";

// Regression guard: every symbol from the original api.ts must be
// exported from the new barrel. TypeScript types are erased at
// runtime so `tsc --noEmit` is the authoritative check. This smoke
// test ensures the barrel and the legacy shim remain importable and
// resolve to the same module graph.
import * as typesBarrel from "@/types";
import * as typesShim from "@/types/api";

describe("types barrel", () => {
  it("the barrel module is importable", () => {
    expect(typesBarrel).toBeDefined();
    expect(typeof typesBarrel).toBe("object");
  });

  it("the legacy `@/types/api` shim is importable and re-exports the barrel", () => {
    expect(typesShim).toBeDefined();
    // Both modules are type-only today (no runtime exports). If the
    // shim ever drifts from re-exporting `./index`, the surface of
    // runtime keys will diverge.
    expect(Object.keys(typesShim)).toEqual(Object.keys(typesBarrel));
  });
});
