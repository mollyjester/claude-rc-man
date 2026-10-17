import { expect, type Locator, test } from "@playwright/test";

import { m } from "../src/messages";
import { terminalLines as lines, makeWorkspace, runInTerminal as run } from "./nav";
import { ALICE, signInAs } from "./people";

// The size the shell says its terminal has, from what `stty size` printed last, if it has.
async function sttySize(panel: Locator): Promise<{ rows: number; cols: number } | null> {
  const said = (await lines(panel)).filter((line) => /^\d+ \d+$/.test(line)).at(-1);
  if (said === undefined) return null;
  const [rows, cols] = said.split(" ").map(Number);
  return { rows, cols };
}

test("the Terminal tab runs a shell in the workspace, whose size follows the window's", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("/");
  const made = await makeWorkspace(page, "alice-terminal");
  await made.getByRole("tab", { name: m.tabTerminal }).click();
  await expect(made.getByRole("tab", { name: m.tabTerminal })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(new URL(page.url()).searchParams.get("tab")).toBe("terminal");
  await expect(made.getByRole("status")).toHaveText(m.terminalConnected);

  // what is typed reaches the shell, and what it works out is shown
  await run(page, made, "echo $((6*7))");
  await expect.poll(() => lines(made)).toContain("42");

  await run(page, made, "stty size");
  await expect.poll(() => sttySize(made)).not.toBeNull();
  const wide = (await sttySize(made)) as { rows: number; cols: number };
  expect(wide.cols).toBeGreaterThan(0);

  // a narrower window fits the screen again, and the shell is told the new size
  await page.setViewportSize({ width: 900, height: 900 });
  await run(page, made, "clear; stty size");
  await expect
    .poll(async () => (await sttySize(made))?.cols ?? Number.POSITIVE_INFINITY, {
      message: "the columns did not follow the window",
    })
    .toBeLessThan(wide.cols);
  expect((await sttySize(made))?.rows).toBeGreaterThan(0);
});

test("opening the terminal in another page takes it over, and the first says so", async ({
  page,
  context,
}) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  const made = await makeWorkspace(page, "alice-taken-over");
  await made.getByRole("tab", { name: m.tabTerminal }).click();
  await expect(made.getByRole("status")).toHaveText(m.terminalConnected);

  const other = await context.newPage();
  await signInAs(other, ALICE);
  await other.goto(page.url());
  const there = other.getByRole("region", { name: "alice-taken-over", exact: true });
  await expect(there.getByRole("status")).toHaveText(m.terminalConnected);

  await expect(made.getByRole("status")).toHaveText(m.terminalClosed.terminal_replaced);
  await expect(made.getByRole("button", { name: m.terminalReconnect })).toBeVisible();
  await run(other, there, "echo $((6*7))");
  await expect.poll(() => lines(there)).toContain("42");
});

test("a Terminal tab left at once and shown again fits the shell to its screen", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("/");
  const made = await makeWorkspace(page, "alice-back-and-forth");
  await made.getByRole("tab", { name: m.tabTerminal }).click();
  await made.getByRole("tab", { name: m.tabOverview }).click();
  await made.getByRole("tab", { name: m.tabTerminal }).click();
  await expect(made.getByRole("status")).toHaveText(m.terminalConnected);
  await run(page, made, "stty size");
  await expect.poll(() => sttySize(made)).not.toBeNull();
  const said = (await sttySize(made)) as { rows: number; cols: number };
  // the rows xterm.js draws are the rows it was fitted to
  expect(said.rows).toBe(await made.locator(".terminal-screen .xterm-rows > div").count());
  expect(said).not.toEqual({ rows: 24, cols: 80 }); // not xterm.js's size before any fit
});
