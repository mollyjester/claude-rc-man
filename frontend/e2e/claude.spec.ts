import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel } from "./nav";
import { BOB, signInAs } from "./people";

test("connecting a Claude account", async ({ page }) => {
  await signInAs(page, BOB);
  await page.goto("/?folder=projects/bob-alpha");
  await expect(page.getByText(m.claudeNotConnected)).toBeVisible();
  const opened = panel(page, "projects/bob-alpha");
  await expect(opened.getByText(m.notConnected.start)).toBeVisible();
  await expect(opened.getByRole("button", { name: m.start })).toHaveCount(0);

  await page.getByRole("button", { name: m.connect }).click();
  await page.getByRole("button", { name: m.connectGo }).click();
  await expect(page.getByRole("link", { name: m.connectLink })).toHaveAttribute(
    "href",
    /oauth\/authorize/,
  );

  await page.getByLabel(m.connectCode).fill("WRONG");
  await page.getByRole("button", { name: m.connectSubmit }).click();
  // rc-helper's own sentence for a code Claude did not take, MESSAGES["login_failed"]
  // in helpers/rc_helper.py
  await expect(page.getByRole("alert")).toContainText(
    "Signing in to Claude failed. Start again and use a fresh code.",
  );

  await page.getByRole("button", { name: m.connectGo }).click();
  await page.getByLabel(m.connectCode).fill("GOOD-CODE");
  await page.getByRole("button", { name: m.connectSubmit }).click();
  await expect(page.getByText(m.claudeConnectedAs("fake-user@example.com"))).toBeVisible();
  await expect(opened.getByRole("button", { name: m.start })).toBeVisible();
  await expect(opened.getByText(m.notConnected.start)).toHaveCount(0);
});
