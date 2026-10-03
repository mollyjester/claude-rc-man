import type { Page } from "@playwright/test";

export const ALICE = "alice@example.com";
export const BOB = "bob@example.com";

// What oauth2-proxy would add after a Google sign-in.
export async function useAs(page: Page, email: string) {
  await page.setExtraHTTPHeaders({ "X-Forwarded-Email": email });
}

// The world tests/e2e_server.py builds: each person's home under homes/, and
// the origins of their repositories under origins/, which the world's GitHub
// serves as https://github.com/<owner>/<name>.git.
export const WORLD = "/tmp/rcman-e2e";
// The token the world's GitHub takes for a push, which playwright.config.ts gives it.
export const GITHUB_TOKEN = "e2e-github-token-of-the-world";
