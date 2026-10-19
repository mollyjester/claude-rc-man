// Finding the parts of rcman's pages, for the browser tests here and in e2e-stack/.
import { expect, type Locator, type Page } from "@playwright/test";

import { m } from "../src/messages";

// The list of workspaces, which the page's own heading names.
export const workspaces = (page: Page) =>
  page.getByRole("region", { name: m.navDashboard, exact: true });
export const panel = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

// Opens New workspace's dialog with its button on the Workspaces page, and returns it.
export async function newWorkspace(page: Page) {
  await page.getByRole("button", { name: m.newWorkspace, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: m.newWorkspace });
  await expect(dialog).toBeVisible();
  return dialog;
}

// Deletes a workspace with its trash button in the list, after the question.
export async function deleteWorkspace(page: Page, name: string) {
  await workspaces(page)
    .getByRole("button", { name: m.deleteWorkspaceNamed(name), exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: m.deleteWorkspaceTitle(name) });
  await expect(dialog.getByText(m.deleteWorkspaceBody)).toBeVisible();
  await dialog.getByRole("button", { name: m.deleteWorkspaceConfirm }).click();
  await expect(page.getByRole("status")).toHaveText(m.workspaceDeleted(name));
}

// Who the top bar says is signed in, read from its account menu, which is closed again after.
export async function expectSignedInAs(page: Page, email: string) {
  const button = page.getByRole("banner").getByRole("button", { name: m.account });
  await button.click();
  await expect(page.getByRole("banner").getByText(m.signedInAs(email))).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(button).toHaveAttribute("aria-expanded", "false");
}

// The one navigation of the pages: the sidebar, and on a phone a drawer its menu button opens.
export const sidebar = (page: Page) => page.getByRole("navigation", { name: m.navMain });

// Opens a page through the sidebar, opening it as a drawer first where it is one.
export async function openPage(page: Page, label: string) {
  const menu = page.getByRole("banner").getByRole("button", { name: m.navMenu });
  if (await menu.isVisible()) await menu.click();
  await sidebar(page).getByRole("link", { name: label, exact: true }).click();
}

// Opens one of the administrators' pages through the sidebar's group of them.
export async function openAdminPage(page: Page, label: string) {
  await openPage(page, label);
}

// Makes a workspace from the Workspaces page, which it is on, and waits until it
// is ready; it is then selected, and its panel is returned. `wait` is how long to
// wait for that, in milliseconds, and the expectation timeout when it is not given;
// a caller whose workspace takes longer than that says so (e2e-stack/timing.ts).
export async function makeWorkspace(page: Page, name: string, wait?: number) {
  const dialog = await newWorkspace(page);
  await dialog.getByLabel(m.newWorkspaceName).fill(name);
  await dialog.getByRole("button", { name: m.create }).click();
  await expect(dialog).toHaveCount(0);
  const opened = panel(page, name);
  await expect(
    opened.getByRole("heading", { name: m.serverTitle }),
    `workspace "${name}" was not ready${wait === undefined ? "" : ` within ${wait} ms`}`,
  ).toBeVisible({ timeout: wait });
  return opened;
}

// The public repository the browser tests' backend has at this address on GitHub
// (SEEDED_ADDRESS in tests/e2e_server.py), the only one a clone there can reach.
export const GITHUB_ADDRESS = "https://github.com/octo/widget";

// What a panel's terminal shows, line by line, as xterm.js draws it: a row is an element of
// its own, so its text has no line breaks but those innerText gives it.
export async function terminalLines(panel: Locator): Promise<string[]> {
  const text = await panel.locator(".terminal-screen .xterm-rows").innerText();
  return text.split("\n").map((line) => line.trim());
}

// Types a command into a panel's terminal and presses Enter.
export async function runInTerminal(page: Page, panel: Locator, command: string) {
  await panel.locator(".terminal-screen").click();
  await page.keyboard.type(command);
  await page.keyboard.press("Enter");
}

// Presses one of a person's actions on the People page, which it is on: the row's own button
// where it is shown, and on a phone, where a row's actions are behind its More actions button,
// that item of its menu.
export async function personAction(page: Page, email: string, action: string) {
  const row = page.getByRole("row", { name: new RegExp(email) });
  const more = row.getByRole("button", { name: m.usersMoreActionsFor(email) });
  await expect(row).toBeVisible();
  if (await more.isVisible()) await more.click();
  await row.getByRole("button", { name: action, exact: true }).click();
}

// The Upgrade rcman page's report as the server gives it with the upgrade from the web interface
// on and one that may be asked for: the browser tests' rcman has it off, which offers no button.
export const plantUpgradeOffered = (page: Page) =>
  page.route("**/api/rcman/upgrade", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const answer = await (await route.fetch()).json();
    await route.fulfill({ json: { ...answer, enabled: true, available: true, reason: null } });
  });

// Opens the Upgrade rcman page's question, the upgrade offered, and returns it.
export async function openUpgradeDialog(page: Page) {
  await plantUpgradeOffered(page);
  await page.goto("/admin/rcman");
  await page.getByRole("button", { name: m.upgradeRcman, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: m.upgradeRcman });
  await expect(dialog).toBeVisible();
  return dialog;
}
