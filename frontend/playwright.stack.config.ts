import { defineConfig } from "@playwright/test";

import { TEST_WITHIN } from "./e2e-stack/timing";

// The whole stack in the podman host (tests/container/stack.py): Caddy with its
// own certificate authority, oauth2-proxy, rcman as installed, and its workspaces
// in real Podman.
// tests/test_container_suite.py starts it and passes its published HTTPS port,
// which only that run knows.
const port = process.env.RCMAN_STACK_PORT;
if (!port) {
  throw new Error(
    "RCMAN_STACK_PORT is not set: tests/test_container_suite.py starts the stack and sets it",
  );
}

export default defineConfig({
  testDir: "e2e-stack",
  // Its own, as every config has: see project-knowledge/frontend.md.
  outputDir: "test-results/stack",
  workers: 1,
  fullyParallel: false,
  timeout: TEST_WITHIN,
  expect: { timeout: 20_000 },
  use: {
    baseURL: "https://rcman.test",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    launchOptions: {
      args: [
        `--host-resolver-rules=MAP rcman.test:443 127.0.0.1:${port}, MAP auth.test:443 127.0.0.1:${port}`,
      ],
    },
  },
});
