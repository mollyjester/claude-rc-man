import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { BOB, useAs } from "./people";

test("connecting a Claude account", async ({ page }) => {
  await useAs(page, BOB);
  await page.goto("/");
  await expect(page.getByText(m.claudeNotConnected)).toBeVisible();
  await page.getByRole("button", { name: m.connect }).click();
  await page.getByRole("button", { name: m.connectGo }).click();
  await expect(page.getByRole("link", { name: m.connectLink })).toHaveAttribute("href", /oauth\/authorize/);

  await page.getByLabel(m.connectCode).fill("WRONG");
  await page.getByRole("button", { name: m.connectSubmit }).click();
  await expect(page.getByRole("alert")).toBeVisible();

  await page.getByRole("button", { name: m.connectGo }).click();
  await page.getByLabel(m.connectCode).fill("GOOD-CODE");
  await page.getByRole("button", { name: m.connectSubmit }).click();
  await expect(page.getByText(m.claudeConnectedAs("fake-user@example.com"))).toBeVisible();
});
