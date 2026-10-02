import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { FIELDS } from "../src/Settings";
import { openAdminPage } from "./nav";
import { ALICE, BOB, useAs } from "./people";

test("settings: the server's verdict is shown as given", async ({ page }) => {
  await useAs(page, BOB);
  await page.goto("/settings");
  const part = page.getByRole("region", { name: m.settingsPrefixTitle });
  const prefix = part.getByLabel(FIELDS.prefix.label);
  await expect(prefix).toHaveValue("bob");
  await prefix.fill("not valid!");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByRole("alert")).toContainText("prefix:");
  await prefix.fill("bobs-box");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByText(m.saved)).toBeVisible();
});

test("settings: the commit author falls back to the sign-in address", async ({ page }) => {
  await useAs(page, BOB);
  await page.goto("/settings");
  const part = page.getByRole("region", { name: m.settingsAuthorTitle });
  const name = part.getByLabel(FIELDS.author_name.label);
  await expect(part.getByText(m.authorNotSet)).toBeVisible();
  await name.fill("<b>");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByRole("alert")).toContainText("author_name:");
  await name.fill("Bob B");
  await expect(part.getByLabel(FIELDS.author_email.label)).toHaveValue("");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByText(m.authorEffective("Bob B", BOB))).toBeVisible();
  await page.reload();
  await expect(page.getByText(m.authorEffective("Bob B", BOB))).toBeVisible();
});

test("help: pages from docs/, with links between them", async ({ page }) => {
  await useAs(page, BOB);
  await page.goto("/help");
  const nav = page.getByRole("navigation", { name: m.docsTitle });
  await nav.getByRole("link", { name: "Servers" }).click();
  await expect(page.getByRole("heading", { name: "Servers", level: 1 })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Servers", level: 1 })).toBeVisible();
});

test("administration is offered to administrators only", async ({ page }) => {
  await useAs(page, BOB);
  await page.goto("/");
  await expect(page.getByText(m.signedInAs(BOB))).toBeVisible();
  await expect(page.getByRole("link", { name: m.navSettings })).toBeVisible();
  await expect(page.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await expect(page.getByRole("link", { name: m.navUsers })).toHaveCount(0);
  await page.goto("/admin/people");
  await expect(page).toHaveURL("/");

  await useAs(page, ALICE);
  await page.goto("/");
  await openAdminPage(page, m.navUsers);
  await expect(page.getByRole("row", { name: /bob@example.com/ })).toBeVisible();
  await openAdminPage(page, m.navAudit);
  await expect(page.getByRole("heading", { name: m.auditTitle })).toBeVisible();
  await openAdminPage(page, m.navVersions);
  await expect(page.getByText(m.versionsApproved)).toBeVisible();
});

test("a refused change shows the server's reason", async ({ page }) => {
  await useAs(page, ALICE);
  await page.goto("/admin/claude");
  await page.getByLabel(m.versionsUpgrade).fill("9.9.9");
  await page.getByRole("button", { name: m.versionsUpgradeGo }).click();
  await expect(page.getByRole("alert")).toContainText("version");
});

test("an address rcman does not know is refused", async ({ page }) => {
  await useAs(page, "stranger@example.com");
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("may not use rcman");
});
