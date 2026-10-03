import { defineConfig } from "@playwright/test";

// The whole stack in one container (tests/container/stack.py): Caddy with its
// own certificate authority, oauth2-proxy, rcman, real Linux accounts and sudo.
// tests/test_container_suite.py starts it and passes its published HTTPS port.
const port = process.env.RCMAN_STACK_PORT ?? "44300";

export default defineConfig({
  testDir: "e2e-stack",
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
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
