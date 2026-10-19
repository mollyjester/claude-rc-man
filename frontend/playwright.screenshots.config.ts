import { defineConfig } from "@playwright/test";

import base from "./playwright.config";

// make screenshots: every page captured for review, at a phone's and a desktop's width, light
// and dark, against the same backend as the browser tests (playwright.config.ts), in a world
// and on a port of this run's own. It asserts nothing and is no test: the browser tests' config
// does not match its file, and no test run uses this one (tests/test_e2e_server.py).

export default defineConfig({
  ...base,
  testMatch: "**/screenshots.capture.ts",
  // Its own, as every config has: see project-knowledge/frontend.md. Playwright empties it as
  // a run starts, so it holds this run's screenshots only.
  outputDir: "test-results/screenshots",
  timeout: 300_000,
  use: {
    ...base.use,
    trace: "off",
    // /dev/shm is small in a container, a workspace's among them.
    launchOptions: { args: ["--disable-dev-shm-usage"] },
  },
});
