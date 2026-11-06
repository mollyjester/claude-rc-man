import { vi } from "vitest";

import { UNIT_HANG_GUARD_MS } from "./hangGuard";

// vi.waitFor bounded by the component tests' step (src/test/hangGuard.ts): it looks again until
// the callback stops throwing, and fails only on a hang. vi.waitFor's own bound is a second and
// Testing Library's asyncUtilTimeout does not reach it, so no test calls it but through this.
export function waitForIt<T>(callback: () => T | Promise<T>): Promise<T> {
  return vi.waitFor(callback, { timeout: UNIT_HANG_GUARD_MS });
}
