import { defineConfig } from "@playwright/test";

import { TEST_WITHIN } from "./e2e-stack/timing";

// The whole stack in a container of the tests' engine (tests/container/stack.py): Caddy with
// its own certificate authority, oauth2-proxy, rcman, and its workspaces in real Podman. The
// browser runs in a container that joins the stack's network;
// tests/container/test_stack_in_a_browser.py starts both and says the port Caddy listens on there.
const port = process.env.RCMAN_STACK_PORT;
if (!port) {
  throw new Error(
    "RCMAN_STACK_PORT is not set: tests/container/test_stack_in_a_browser.py starts the stack and sets it",
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
        "--disable-dev-shm-usage",
        `--host-resolver-rules=MAP rcman.test:443 127.0.0.1:${port}, MAP auth.test:443 127.0.0.1:${port}`,
      ],
    },
  },
});
