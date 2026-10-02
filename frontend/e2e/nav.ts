import type { Page } from "@playwright/test";

import { m } from "../src/messages";

// Opens one of the administrators' pages through the navigation's Admin menu.
export async function openAdminPage(page: Page, label: string) {
  await page.getByRole("banner").getByRole("button", { name: m.navAdmin }).click();
  await page.getByRole("banner").getByRole("link", { name: label }).click();
}
