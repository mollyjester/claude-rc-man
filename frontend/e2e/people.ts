import type { Page } from "@playwright/test";

export const ALICE = "alice@example.com";
export const BOB = "bob@example.com";

// What oauth2-proxy would add after a Google sign-in.
export async function useAs(page: Page, email: string) {
  await page.setExtraHTTPHeaders({ "X-Forwarded-Email": email });
}
