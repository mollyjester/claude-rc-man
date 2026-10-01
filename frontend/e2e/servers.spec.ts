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

  await page.getByRole("article", { name: "projects/alice-beta" }).getByRole("button", { name: m.stop }).click();
});
