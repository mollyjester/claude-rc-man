import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { FIELDS } from "../src/Settings";
import { makeWorkspace, openAdminPage } from "./nav";
import { ALICE, BOB, signInAs } from "./people";

test("settings: the server's verdict is shown as given", async ({ page }) => {
  await signInAs(page, BOB);
  await page.goto("/settings");
  const part = page.getByRole("region", { name: m.settingsPrefixTitle });
  const prefix = part.getByLabel(FIELDS.prefix.label);
  await expect(prefix).toHaveValue("");
  await prefix.fill("not valid!");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByRole("alert")).toContainText(`${FIELDS.prefix.label}:`);
  await expect(part.getByRole("alert")).not.toContainText("pattern"); // rcman's words, not pydantic's
  await prefix.fill("bobs-box");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByText(m.saved)).toBeVisible();
  // and cleared again, to name sessions after each workspace
  await page.reload();
  await expect(prefix).toHaveValue("bobs-box");
  await prefix.fill("");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByText(m.saved)).toBeVisible();
  await page.reload();
  await expect(prefix).toHaveValue("");
});

test("settings: a GitHub token is stored, never shown again, and removed", async ({ page }) => {
  const token = "ghp_e2e-token-never-shown";
  await signInAs(page, BOB);
  await page.goto("/settings");
  const github = page.getByRole("region", { name: m.settingsGithubTitle });
  await expect(github.getByText(m.githubTokenNotStored)).toBeVisible();
  await github.getByLabel(m.githubTokenLabel).fill(token);
  await github.getByRole("button", { name: m.save }).click();
  await expect(github.getByText(m.githubTokenSaved)).toBeVisible();
  await expect(github.getByText(m.githubTokenStored)).toBeVisible();
  await expect(github.getByLabel(m.githubTokenLabel)).toHaveValue("");
  await page.reload();
  await expect(github.getByText(m.githubTokenStored)).toBeVisible();
  await expect(github.getByLabel(m.githubTokenLabel)).toHaveValue("");
  await expect(page.getByText(token)).toHaveCount(0);
  await github.getByRole("button", { name: m.githubTokenRemove }).click();
  const dialog = page.getByRole("dialog", { name: m.githubTokenRemoveTitle });
  await dialog.getByRole("button", { name: m.githubTokenRemoveConfirm }).click();
  await expect(github.getByText(m.githubTokenNotStored)).toBeVisible();
  await expect(github.getByRole("button", { name: m.githubTokenRemove })).toHaveCount(0);
  await page.reload();
  await expect(github.getByText(m.githubTokenNotStored)).toBeVisible();
});

test("settings: the commit author falls back to the sign-in address", async ({ page }) => {
  await signInAs(page, BOB);
  await page.goto("/settings");
  const part = page.getByRole("region", { name: m.settingsAuthorTitle });
  const name = part.getByLabel(FIELDS.author_name.label);
  await expect(part.getByText(m.authorNotSet)).toBeVisible();
  await name.fill("<b>");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByRole("alert")).toContainText(`${FIELDS.author_name.label}:`);
  await name.fill("Bob B");
  await expect(part.getByLabel(FIELDS.author_email.label)).toHaveValue("");
  await part.getByRole("button", { name: m.save }).click();
  await expect(part.getByText(m.authorEffective("Bob B", BOB))).toBeVisible();
  await page.reload();
  await expect(page.getByText(m.authorEffective("Bob B", BOB))).toBeVisible();
});

test("help: pages from docs/, with links between them", async ({ page }) => {
  await signInAs(page, BOB);
  await page.goto("/help");
  const nav = page.getByRole("navigation", { name: m.docsTitle });
  await nav.getByRole("link", { name: "Workspaces" }).click();
  await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
});

test("administration is offered to administrators only", async ({ page }) => {
  await signInAs(page, BOB);
  await page.goto("/");
  await expect(page.getByText(m.signedInAs(BOB))).toBeVisible();
  await expect(page.getByRole("link", { name: m.navSettings })).toBeVisible();
  await expect(page.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await expect(page.getByRole("link", { name: m.navUsers })).toHaveCount(0);
  await page.goto("/admin/people");
  await expect(page).toHaveURL("/");

  await signInAs(page, ALICE);
  await page.goto("/");
  await openAdminPage(page, m.navUsers);
  await expect(page.getByRole("row", { name: /bob@example.com/ })).toBeVisible();
  await openAdminPage(page, m.navEveryone);
  await expect(page.getByRole("heading", { name: m.everyoneTitle })).toBeVisible();
  await openAdminPage(page, m.navAudit);
  await expect(page.getByRole("heading", { name: m.auditTitle })).toBeVisible();
  await page.getByRole("banner").getByRole("button", { name: m.navAdmin }).click();
  const menu = page.getByRole("banner").getByRole("listitem");
  await expect(menu).toHaveCount(4);
  await expect(menu.getByRole("link", { name: m.navVersions })).toBeVisible();
});

test("the Claude Code versions page shows the approved version and the installed ones, to administrators only", async ({
  page,
}) => {
  // What the release site would name is not asked of the real one from a test.
  await page.route("**/api/claude/latest", (route) => route.fulfill({ json: { latest: null } }));
  await signInAs(page, ALICE);
  await page.goto("/");
  await openAdminPage(page, m.navVersions);
  await expect(page.getByRole("heading", { name: m.versionsTitle })).toBeVisible();
  const approved = page
    .getByText(m.versionsApproved, { exact: true })
    .locator("xpath=following-sibling::dd[1]");
  await expect(approved).toHaveText(/^\d+(\.\d+)+$/);
  const installed = page.getByRole("region", { name: m.versionsInstalledTitle });
  await expect(
    installed.getByRole("row", { name: new RegExp(m.versionsStandingApproved) }),
  ).toBeVisible();
  await expect(installed.getByRole("button")).toHaveCount(0); // the one installed version is the approved one
  await expect(page.getByText(m.versionsLatestUnknown)).toBeVisible();
  await expect(page.getByText(m.versionsDriftNone)).toBeVisible();

  await signInAs(page, BOB);
  await page.goto("/admin/claude");
  await expect(page).toHaveURL("/");
});

test("people are added and removed, and the audit trail shows it", async ({ page, browser }) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  await openAdminPage(page, m.navUsers);
  await page.getByLabel(m.usersEmail).fill("dave@example.com");
  await page.getByRole("button", { name: m.usersAdd }).click();
  await expect(page.getByText(m.usersAdded("dave@example.com"))).toBeVisible();
  const dave = page.getByRole("row", { name: /dave@example.com/ });
  await expect(dave.getByText(m.usersUser)).toBeVisible();

  // dave can sign in now, and makes a workspace
  const signedIn = await browser.newPage();
  await signInAs(signedIn, "dave@example.com");
  await signedIn.goto("/");
  await makeWorkspace(signedIn, "dave-box");
  await page.reload();
  await expect(dave.getByRole("cell", { name: "1", exact: true })).toBeVisible();

  await dave.getByRole("button", { name: m.usersRemove }).click();
  const dialog = page.getByRole("dialog", { name: m.usersRemoveTitle("dave@example.com") });
  await expect(dialog.getByText(m.usersRemoveBody)).toBeVisible();
  await dialog.getByRole("button", { name: m.usersRemoveConfirm }).click();
  await expect(page.getByText(m.usersRemoved("dave@example.com"))).toBeVisible();
  await expect(dave).toHaveCount(0);

  // dave is let in no more
  await signedIn.reload();
  await expect(signedIn.getByRole("alert")).toContainText("may not use rcman");
  await signedIn.close();

  await openAdminPage(page, m.navAudit);
  for (const action of ["person.add", "workspace.create", "person.delete"]) {
    await expect(
      page.getByRole("row").filter({ hasText: action }).filter({ hasText: "dave@example.com" }),
    ).not.toHaveCount(0);
  }
});

test("administrators are warned, with a link to everyone's workspaces, while a server is on an unrecognised screen", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  // No workspace's fake claude here shows a screen rcman does not recognise, so the
  // report is planted; what the page does with it is real.
  await page.route("**/api/attention", (route) =>
    route.fulfill({ json: { unrecognised: [{ email: BOB, workspace: "bob-odd" }] } }),
  );
  await page.goto("/");
  const banner = page.getByRole("region", { name: m.attentionTitle });
  await expect(banner).toContainText(m.attentionUnrecognised([`${BOB}: bob-odd`]));
  await banner.getByRole("link", { name: m.navEveryone }).click();
  await expect(page.getByRole("heading", { name: m.everyoneTitle })).toBeVisible();
});

test("an address rcman does not know is refused", async ({ page }) => {
  await signInAs(page, "stranger@example.com");
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("may not use rcman");
});

test("the tab's icon is *rc, white on a black circle", async ({ page, request }) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  const href = await page.locator('link[rel="icon"]').getAttribute("href");
  const icon = await request.get(href!);
  expect(icon.headers()["content-type"]).toContain("image/svg+xml");
  const svg = await icon.text();
  expect(svg).toContain("<circle");
  expect(svg).toContain(">*rc</text>");
});
