import { defineConfig } from "@playwright/test";

import { ORIGIN, PORT, WORLD } from "./e2e/world";

// The built frontend, served by the real backend (tests/e2e_server.py) with
// workspaces on the host runtime and the fake claude, in a world and on a port of
// this run's own (e2e/world.ts). oauth2-proxy's identity header is set per test with
// signInAs() in e2e/people.ts.

export default defineConfig({
  testDir: "e2e",
  // Its own, as every config has: see project-knowledge/frontend.md.
  outputDir: "test-results/e2e",
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: ORIGIN,
    trace: "retain-on-failure",
    // /dev/shm is small in a container, a workspace's among them.
    launchOptions: { args: ["--disable-dev-shm-usage"] },
  },
  webServer: {
    command: `uv run uvicorn --factory tests.e2e_server:create_app --ws websockets-sansio --host 127.0.0.1 --port ${PORT}`,
    cwd: "..",
    url: `${ORIGIN}/api/health`,
    reuseExistingServer: false,
    // Not the default SIGKILL: the backend's shutdown stops the tmux servers it started.
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
    env: {
      RCMAN_PUBLIC_ORIGIN: ORIGIN,
      RCMAN_E2E_DIR: WORLD,
    },
  },
});
