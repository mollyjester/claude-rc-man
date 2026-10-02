import { type Page, expect } from "@playwright/test";

import { m } from "../src/messages";

// Opens one of the administrators' pages through the navigation's Admin menu.
export async function openAdminPage(page: Page, label: string) {
  await page.getByRole("banner").getByRole("button", { name: m.navAdmin }).click();
  await page.getByRole("banner").getByRole("link", { name: label }).click();
}

// Selects a folder in projects, opening projects first unless it is open.
export async function selectInProjects(page: Page, name: string) {
  const tree = page.getByRole("region", { name: m.foldersTitle });
  const open = tree.getByRole("button", { name: m.expand("projects") });
  await expect(open.or(tree.getByRole("button", { name: m.collapse("projects") }))).toBeVisible();
  if (await open.isVisible()) await open.click();
  await tree.getByRole("button", { name, exact: true }).click();
}
