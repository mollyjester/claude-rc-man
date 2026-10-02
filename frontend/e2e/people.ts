import type { Page } from "@playwright/test";

export const ALICE = "alice@example.com";
export const BOB = "bob@example.com";

// What oauth2-proxy would add after a Google sign-in.
export async function useAs(page: Page, email: string) {
  await page.setExtraHTTPHeaders({ "X-Forwarded-Email": email });
}

// The world tests/e2e_server.py builds: each person's home under homes/, and
// the origins of their repositories under origins/.
export const WORLD = "/tmp/rcman-e2e";
