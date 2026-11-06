import { defineConfig } from "@playwright/test";

import { ORIGIN, PORT, WORLD } from "./e2e/world";
import { BROWSER_HANG_GUARD_MS, BROWSER_TEST_GUARD_MS } from "./src/test/hangGuard";

// The built frontend, served by the real backend (tests/e2e_server.py) with
// workspaces on the host runtime and the fake claude, in a world and on a port of
// this run's own (e2e/world.ts). oauth2-proxy's identity header is set per test with
// signInAs() in e2e/people.ts.

export default defineConfig({
  testDir: "e2e",
  // Its own, as every config has: see project-knowledge/frontend.md.
  outputDir: "test-results/e2e",
  workers: 1,
  // The browser tests' hang guard (src/test/hangGuard.ts): a test's, and a step's for each wait,
  // action and navigation, the backend's start and its stop. Each ends only a hang; a test waits
  // on what it observes. The run has no bound of its own, as each test has.
  timeout: BROWSER_TEST_GUARD_MS,
  globalTimeout: 0,
  expect: { timeout: BROWSER_HANG_GUARD_MS },
  use: {
    baseURL: ORIGIN,
    actionTimeout: BROWSER_HANG_GUARD_MS,
    navigationTimeout: BROWSER_HANG_GUARD_MS,
    trace: "retain-on-failure",
    // /dev/shm is small in a container, a workspace's among them.
    launchOptions: { args: ["--disable-dev-shm-usage"] },
  },
  webServer: {
    command: `uv run uvicorn --factory tests.e2e_server:create_app --ws websockets-sansio --host 127.0.0.1 --port ${PORT}`,
    cwd: "..",
    url: `${ORIGIN}/api/health`,
    reuseExistingServer: false,
    timeout: BROWSER_HANG_GUARD_MS,
    // Not the default SIGKILL: the backend's shutdown stops the tmux servers it started.
    gracefulShutdown: { signal: "SIGTERM", timeout: BROWSER_HANG_GUARD_MS },
    env: {
      RCMAN_PUBLIC_ORIGIN: ORIGIN,
      RCMAN_E2E_DIR: WORLD,
    },
  },
});
