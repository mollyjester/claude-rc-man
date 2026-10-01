import { defineConfig } from "@playwright/test";

// The built frontend, served by the real backend (tests/e2e_server.py) with a
// real rc-helper per user and the fake claude. oauth2-proxy's identity header
// is set per test with useAs() in e2e/people.ts.
const PORT = 8765;
const ORIGIN = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "e2e",
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: { baseURL: ORIGIN, trace: "retain-on-failure" },
  webServer: {
    command: `uv run uvicorn --factory tests.e2e_server:create_app --host 127.0.0.1 --port ${PORT}`,
    cwd: "..",
    url: `${ORIGIN}/api/health`,
    reuseExistingServer: false,
    // Not the default SIGKILL: the backend's shutdown stops the tmux servers it started.
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
    env: { RCMAN_PUBLIC_ORIGIN: ORIGIN, RCMAN_E2E_DIR: "/tmp/rcman-e2e" },
  },
});
