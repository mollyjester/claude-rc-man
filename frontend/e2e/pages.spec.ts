import { expect, test } from "@playwright/test";

import { ADMIN_PAGES } from "../src/AdminPages";
import { m } from "../src/messages";
import { FIELDS } from "../src/Settings";
import { expectSignedInAs, makeWorkspace, openAdminPage, sidebar } from "./nav";
import { ALICE, BOB, signInAs } from "./people";

test("settings: the server's verdict is shown as given", async ({ page }) => {
  await signInAs(page, BOB);
  await page.goto("/settings");
  const part = page.getByRole("region", { name: m.settingsPrefixTitle });
  const prefix = part.getByLabel(FIELDS.prefix.label);
  await expect(prefix).toHaveValue("");
  await prefix.fill("not valid!");
  await part.getByRole("button", { name: m.save }).click();
  // under its field, where focus goes: the alert is only the summary
  await expect(part.getByRole("alert")).toHaveText(m.problemCheckFields);
  await expect(prefix).toHaveAttribute("aria-invalid", "true");
  await expect(prefix).toBeFocused();
  await expect(prefix).not.toHaveAccessibleDescription(/pattern/); // rcman's words, not pydantic's
  await expect(prefix).not.toHaveAccessibleDescription("");
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
  await expect(name).toHaveAttribute("aria-invalid", "true");
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
  await expectSignedInAs(page, BOB);
  await expect(page.getByRole("link", { name: m.navSettings })).toBeVisible();
  await expect(page.getByRole("list", { name: m.navAdmin })).toHaveCount(0);
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
  await openAdminPage(page, m.navRcman);
  await expect(page.getByRole("heading", { name: m.upgradeRcman, level: 1 })).toBeVisible();
  const group = sidebar(page).getByRole("list", { name: m.navAdmin });
  await expect(group.getByRole("listitem")).toHaveCount(ADMIN_PAGES.length);
  await expect(group.getByRole("link", { name: m.navVersions })).toBeVisible();
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
  await page.getByRole("button", { name: m.usersAdd }).click();
  const adding = page.getByRole("dialog", { name: m.usersAdd });
  await adding.getByLabel(m.usersEmail).fill("dave@example.com");
  await adding.getByRole("button", { name: m.usersAddGo }).click();
  await expect(adding).toHaveCount(0);
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
      page
        .getByRole("row")
        .filter({ hasText: m.auditActions[action] })
        .filter({ hasText: "dave@example.com" }),
    ).not.toHaveCount(0);
  }
});

test("an administrator limits a person, the server refuses a limit that is too small, and the limit is kept", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  await openAdminPage(page, m.navUsers);
  const bob = page.getByRole("row", { name: /bob@example.com/ });
  await bob.getByRole("button", { name: m.usersLimits }).click();
  const dialog = page.getByRole("dialog", { name: m.limitsTitle(BOB) });
  const scope = dialog.getByRole("form", { name: m.limitsPersonScope(BOB) });
  await expect(dialog).toBeFocused();
  // the explanation is folded away until asked for
  await expect(dialog.getByText(m.limitsMemoryHard)).toBeHidden();
  await dialog.getByText(m.limitsHowTitle).click();
  await expect(dialog.getByText(m.limitsMemoryHard)).toBeVisible();

  // too small: the server's own sentence is shown under the field
  await scope.getByLabel(m.usersMemory).fill("1M");
  await scope.getByRole("button", { name: m.save }).click();
  await expect(scope.getByLabel(m.usersMemory)).toHaveAttribute("aria-invalid", "true");
  await expect(scope.getByLabel(m.usersMemory)).toBeFocused();

  await scope.getByLabel(m.usersMemory).fill("1G");
  await scope.getByLabel(m.usersCpu).fill("1.5");
  await scope.getByRole("button", { name: m.save }).click();
  await expect(scope.getByRole("status")).toHaveText(m.limitsSaved(m.limitsPersonScope(BOB)));
  await expect(bob.getByRole("cell", { name: "1G", exact: true })).toBeVisible();
  await expect(bob.getByRole("cell", { name: "1.5", exact: true })).toBeVisible();
  await expect(bob.getByRole("cell", { name: m.usersNoLimit })).toHaveCount(1);

  await dialog.getByRole("button", { name: m.close, exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(bob.getByRole("button", { name: m.usersLimits })).toBeFocused();

  // kept: the dialog shows it again after a reload
  await page.reload();
  await bob.getByRole("button", { name: m.usersLimits }).click();
  await expect(
    page.getByRole("form", { name: m.limitsPersonScope(BOB) }).getByLabel(m.usersMemory),
  ).toHaveValue("1G");
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
