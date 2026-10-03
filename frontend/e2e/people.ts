import type { Page } from "@playwright/test";

export const ALICE = "alice@example.com";
export const BOB = "bob@example.com";

// What oauth2-proxy would add after a Google sign-in.
export async function signInAs(page: Page, email: string) {
  await page.setExtraHTTPHeaders({ "X-Forwarded-Email": email });
}

// The token the world's GitHub takes for a push, which playwright.config.ts gives it.
export const GITHUB_TOKEN = "e2e-github-token-of-the-world";
