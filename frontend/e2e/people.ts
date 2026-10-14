import type { Page } from "@playwright/test";

// The people in the world tests/e2e_server.py makes: alice, an administrator,
// whose workspaces' Claude is signed in as each is made, and bob, whose are not.
export const ALICE = "alice@example.com";
export const BOB = "bob@example.com";

// What oauth2-proxy would add after a Google sign-in.
export async function signInAs(page: Page, email: string) {
  await page.setExtraHTTPHeaders({ "X-Forwarded-Email": email });
}
