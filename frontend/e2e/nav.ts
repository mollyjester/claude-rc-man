// Finding the parts of rcman's pages, for the browser tests here and in e2e-stack/.
import { type Page, expect } from "@playwright/test";

import { m } from "../src/messages";

export const tree = (page: Page) => page.getByRole("region", { name: m.foldersTitle });
export const summary = (page: Page) => page.getByRole("region", { name: m.yourServersTitle });
export const panel = (page: Page, path: string) => page.getByRole("region", { name: path, exact: true });

// Opens one of the administrators' pages through the navigation's Admin menu.
export async function openAdminPage(page: Page, label: string) {
  await page.getByRole("banner").getByRole("button", { name: m.navAdmin }).click();
  await page.getByRole("banner").getByRole("link", { name: label }).click();
}

// Selects a folder in projects, opening projects first unless it is open.
export async function selectInProjects(page: Page, name: string) {
  const open = tree(page).getByRole("button", { name: m.expand("projects") });
  await expect(open.or(tree(page).getByRole("button", { name: m.collapse("projects") }))).toBeVisible();
  if (await open.isVisible()) await open.click();
  await tree(page).getByRole("button", { name, exact: true }).click();
}
