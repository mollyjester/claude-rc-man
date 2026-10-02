import { type Page, expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { openAdminPage } from "./nav";
import { ALICE, BOB, useAs } from "./people";

const tree = (page: Page) => page.getByRole("region", { name: m.foldersTitle });
const summary = (page: Page) => page.getByRole("region", { name: m.runningTitle });
const panel = (page: Page, path: string) => page.getByRole("region", { name: path, exact: true });

// Selects a folder in projects, opening projects first unless it is open.
async function select(page: Page, name: string) {
  const open = tree(page).getByRole("button", { name: m.expand("projects") });
  await expect(open.or(tree(page).getByRole("button", { name: m.collapse("projects") }))).toBeVisible();
  if (await open.isVisible()) await open.click();
  await tree(page).getByRole("button", { name, exact: true }).click();
}

test("start, trust, stop, resume and forget through the folder panel", async ({ page }) => {
  await useAs(page, ALICE);
  await page.goto("/");
  await expect(page.getByText(m.claudeConnectedAs("fake-user@example.com"))).toBeVisible();
  await select(page, "alice-alpha");

  const alpha = panel(page, "projects/alice-alpha");
  await alpha.getByRole("button", { name: m.start }).click();
  const card = alpha.getByRole("article", { name: "projects/alice-alpha" });
  await expect(card.getByText(m.trustTitle)).toBeVisible();
  await card.getByRole("button", { name: m.trustYes }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await expect(card.getByText(/^alice-/)).toBeVisible();
  await expect(card.getByRole("link", { name: m.openSession })).toHaveAttribute("href", /environment=/);
  await expect(alpha.getByRole("button", { name: m.start })).toHaveCount(0);
  await expect(tree(page).getByRole("button", { name: "alice-alpha", exact: true })).toHaveAccessibleDescription(
    m.kinds.running,
  );
  await expect(summary(page).getByRole("button", { name: "projects/alice-alpha" })).toBeVisible();

  await page.reload();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await select(page, "alice-beta");
  await expect(panel(page, "projects/alice-beta").getByRole("button", { name: m.start })).toBeVisible();
  await page.goBack();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();

  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.resume }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.forget }).click();
  await expect(card).toHaveCount(0);
  await expect(alpha.getByRole("button", { name: m.start })).toBeVisible();
});

test("people never see each other's folders or servers", async ({ page, browser }) => {
  await useAs(page, ALICE);
  await page.goto("/");
  await select(page, "alice-beta");
  await panel(page, "projects/alice-beta").getByRole("button", { name: m.start }).click();
  const card = page.getByRole("article", { name: "projects/alice-beta" });
  await expect(card).toBeVisible();

  const bob = await browser.newPage();
  await useAs(bob, BOB);
  await bob.goto("/");
  await bob.getByRole("button", { name: m.expand("projects") }).click();
  await expect(tree(bob).getByRole("button", { name: "bob-alpha", exact: true })).toBeVisible();
  await expect(bob.getByText("alice-beta")).toHaveCount(0);
  await expect(summary(bob).getByText(m.runningEmpty)).toBeVisible();

  // Wait for the stop to finish: the run ends when the test does, and a stop
  // still in flight would be cut off.
  const stopped = page.waitForResponse((r) => r.url().endsWith("/api/servers/stop"));
  await card.getByRole("button", { name: m.stop }).click();
  expect((await stopped).ok()).toBe(true);
});

test("administrators see everyone's servers, and act on none of them", async ({ page, browser }) => {
  await useAs(page, ALICE);
  await page.goto("/");
  await select(page, "alice-alpha");
  await panel(page, "projects/alice-alpha").getByRole("button", { name: m.start }).click();
  const card = page.getByRole("article", { name: "projects/alice-alpha" });
  // The folder is trusted already when the first test ran before this one.
  const trust = card.getByRole("button", { name: m.trustYes });
  const ready = card.getByText(m.states.ready, { exact: true });
  await expect(trust.or(ready)).toBeVisible();
  if (await trust.isVisible()) await trust.click();
  await expect(ready).toBeVisible();

  await openAdminPage(page, m.navEveryone);
  await expect(page.getByRole("heading", { name: m.everyoneTitle })).toBeVisible();
  const alice = page.getByRole("region", { name: m.everyonePerson("alice", ALICE) });
  const row = alice.getByRole("row", { name: /projects\/alice-alpha/ });
  await expect(row.getByText(m.states.ready, { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: m.everyonePerson("bob", BOB) })).toBeVisible();
  await expect(page.getByRole("main").getByRole("button")).toHaveCount(0);

  const bob = await browser.newPage();
  await useAs(bob, BOB);
  await bob.goto("/");
  await expect(bob.getByText(m.signedInAs(BOB))).toBeVisible();
  await expect(bob.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await expect(bob.getByRole("link", { name: m.navEveryone })).toHaveCount(0);
  const refused = await bob.evaluate(async () => (await fetch("/api/servers/everyone")).status);
  expect(refused).toBe(403);

  await page.goto("/?folder=projects/alice-alpha");
  const stopped = page.waitForResponse((r) => r.url().endsWith("/api/servers/stop"));
  await card.getByRole("button", { name: m.stop }).click();
  expect((await stopped).ok()).toBe(true);
});
