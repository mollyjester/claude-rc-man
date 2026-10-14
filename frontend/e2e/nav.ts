// Finding the parts of rcman's pages, for the browser tests here and in e2e-stack/.
import { expect, type Page } from "@playwright/test";

import { m } from "../src/messages";

export const workspaces = (page: Page) => page.getByRole("region", { name: m.workspacesTitle });
export const panel = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

// Opens one of the administrators' pages through the navigation's Admin menu.
export async function openAdminPage(page: Page, label: string) {
  await page.getByRole("banner").getByRole("button", { name: m.navAdmin }).click();
  await page.getByRole("banner").getByRole("link", { name: label }).click();
}

// Makes a workspace from the Workspaces page, which it is on, and waits until it
// is ready; it is then selected, and its panel is returned.
export async function makeWorkspace(page: Page, name: string) {
  const form = page.getByRole("region", { name: m.newWorkspace });
  await form.getByLabel(m.newWorkspaceName).fill(name);
  await form.getByRole("button", { name: m.create }).click();
  const opened = panel(page, name);
  await expect(opened.getByRole("heading", { name: m.serverTitle })).toBeVisible();
  return opened;
}
