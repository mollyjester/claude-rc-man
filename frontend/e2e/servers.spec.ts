import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { ALICE, BOB, useAs } from "./people";

test("start, trust, ready, stop, resume, forget", async ({ page }) => {
  await useAs(page, ALICE);
  await page.goto("/");
  await expect(page.getByText(m.claudeConnectedAs("fake-user@example.com"))).toBeVisible();
  await page.getByRole("button", { name: m.expand("projects") }).click();
  await page.getByRole("button", { name: m.startIn("projects/alice-alpha") }).click();

  const card = page.getByRole("article", { name: "projects/alice-alpha" });
  await expect(card.getByText(m.trustTitle)).toBeVisible();
  await card.getByRole("button", { name: m.trustYes }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await expect(card.getByText(/^alice-/)).toBeVisible();
  await expect(card.getByRole("link", { name: m.openSession })).toHaveAttribute("href", /environment=/);
  await expect(page.getByRole("button", { name: m.startIn("projects/alice-alpha") })).toHaveCount(0);

  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.resume }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.forget }).click();
  await expect(card).toHaveCount(0);
});

test("people never see each other's folders or servers", async ({ page, browser }) => {
  await useAs(page, ALICE);
  await page.goto("/");
  await page.getByRole("button", { name: m.expand("projects") }).click();
  await page.getByRole("button", { name: m.startIn("projects/alice-beta") }).click();
  await expect(page.getByRole("article", { name: "projects/alice-beta" })).toBeVisible();

  const bob = await browser.newPage();
  await useAs(bob, BOB);
  await bob.goto("/");
  await bob.getByRole("button", { name: m.expand("projects") }).click();
  await expect(bob.getByRole("button", { name: m.startIn("projects/bob-alpha") })).toBeVisible();
  await expect(bob.getByText("alice-beta")).toHaveCount(0);
  await expect(bob.getByText(m.serversEmpty)).toBeVisible();

  // Wait for the stop to finish: the run ends when the test does, and a stop
  // still in flight would be cut off.
  const stopped = page.waitForResponse((r) => r.url().endsWith("/api/servers/stop"));
  await page.getByRole("article", { name: "projects/alice-beta" }).getByRole("button", { name: m.stop }).click();
  expect((await stopped).ok()).toBe(true);
});

test("administrators see everyone's servers, and act on none of them", async ({ page, browser }) => {
  await useAs(page, ALICE);
  await page.goto("/");
  await page.getByRole("button", { name: m.expand("projects") }).click();
  await page.getByRole("button", { name: m.startIn("projects/alice-alpha") }).click();
  const card = page.getByRole("article", { name: "projects/alice-alpha" });
  // The folder is trusted already when the first test ran before this one.
  const trust = card.getByRole("button", { name: m.trustYes });
  const ready = card.getByText(m.states.ready, { exact: true });
  await expect(trust.or(ready)).toBeVisible();
  if (await trust.isVisible()) await trust.click();
  await expect(ready).toBeVisible();

  await page.getByRole("link", { name: m.navEveryone }).click();
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
  await expect(bob.getByRole("link", { name: m.navEveryone })).toHaveCount(0);
  const refused = await bob.evaluate(async () => (await fetch("/api/servers/everyone")).status);
  expect(refused).toBe(403);

  await page.goto("/");
  const stopped = page.waitForResponse((r) => r.url().endsWith("/api/servers/stop"));
  await page.getByRole("article", { name: "projects/alice-alpha" }).getByRole("button", { name: m.stop }).click();
  expect((await stopped).ok()).toBe(true);
});
