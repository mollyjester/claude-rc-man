// Finding the parts of rcman's pages, for the browser tests here and in e2e-stack/.
import { expect, type Locator, type Page } from "@playwright/test";

import { m } from "../src/messages";

export const workspaces = (page: Page) => page.getByRole("region", { name: m.workspacesTitle });
export const panel = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

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
  const form = page.getByRole("region", { name: m.newWorkspace });
  await form.getByLabel(m.newWorkspaceName).fill(name);
  await form.getByRole("button", { name: m.create }).click();
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
