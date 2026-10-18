// Finding the parts of rcman's pages, for the browser tests here and in e2e-stack/.
import { expect, type Locator, type Page } from "@playwright/test";

import { m } from "../src/messages";

// The list of workspaces, which the page's own heading names.
export const workspaces = (page: Page) =>
  page.getByRole("region", { name: m.navDashboard, exact: true });
export const panel = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

// Opens New workspace's dialog with the + beside the Workspaces page's heading, and returns it.
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

// Who the header says is signed in, read from its account menu, which is closed again after.
export async function expectSignedInAs(page: Page, email: string) {
  const button = page.getByRole("banner").getByRole("button", { name: m.account });
  await button.click();
  await expect(page.getByRole("banner").getByText(m.signedInAs(email))).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(button).toHaveAttribute("aria-expanded", "false");
}

// Opens one of the administrators' pages through the navigation's Admin menu.
export async function openAdminPage(page: Page, label: string) {
  await page.getByRole("banner").getByRole("button", { name: m.navAdmin }).click();
  await page.getByRole("banner").getByRole("link", { name: label }).click();
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
