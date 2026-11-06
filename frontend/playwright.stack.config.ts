import { defineConfig } from "@playwright/test";

import { STACK_HANG_GUARD_MS, STACK_TEST_GUARD_MS } from "./src/test/hangGuard";

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
  // The whole stack's hang guard (src/test/hangGuard.ts): a spec's, and a step's for each wait,
  // action and navigation, a person's first workspace among them. Each ends only a hang; a spec
  // waits on what it observes. The run has no bound of its own, which would name no test: the
  // podman client's kill of the run is beyond it (tests/hang_guard.py).
  timeout: STACK_TEST_GUARD_MS,
  globalTimeout: 0,
  expect: { timeout: STACK_HANG_GUARD_MS },
  use: {
    baseURL: "https://rcman.test",
    actionTimeout: STACK_HANG_GUARD_MS,
    navigationTimeout: STACK_HANG_GUARD_MS,
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    launchOptions: {
      args: [
        "--disable-dev-shm-usage",
        `--host-resolver-rules=MAP rcman.test:443 127.0.0.1:${port}, MAP auth.test:443 127.0.0.1:${port}, ` +
          `MAP *.ws.rcman.test:443 127.0.0.1:${port}`,
      ],
    },
  },
});
