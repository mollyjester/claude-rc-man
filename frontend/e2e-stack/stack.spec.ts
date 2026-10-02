// rcman as installed, signed in to through Caddy and oauth2-proxy, with each
// person a Linux account of their own. Runs in order against one installation.
import { type Page, expect, test } from "@playwright/test";

import { openAdminPage, selectInProjects } from "../e2e/nav";
import { m } from "../src/messages";

const GOOD_CODE = "GOOD-CODE";

async function signIn(page: Page, email: string) {
  await page.goto("/");
  await expect(page).toHaveURL(/auth\.test\/authorize/); // sent to the sign-in
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
}

async function connectClaude(page: Page) {
  await page.getByRole("button", { name: m.connect }).click();
  await page.getByRole("button", { name: m.connectGo }).click();
  await page.getByLabel(m.connectCode).fill(GOOD_CODE);
  await page.getByRole("button", { name: m.connectSubmit }).click();
  await expect(page.getByText(m.claudeConnectedAs("fake-user@example.com"))).toBeVisible();
}


test("a person signs in, connects Claude, and runs a server", async ({ page }) => {
  await signIn(page, "alice@example.com");
  await expect(page.getByText(m.signedInAs("alice@example.com"))).toBeVisible();
  await expect(page.getByText(m.claudeNotConnected)).toBeVisible();
  await connectClaude(page);

  await selectInProjects(page, "alice-demo");
  const panel = page.getByRole("region", { name: "projects/alice-demo", exact: true });
  await panel.getByRole("button", { name: m.start }).click();
  const card = panel.getByRole("article", { name: "projects/alice-demo" });
  await card.getByRole("button", { name: m.trustYes }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await expect(card.getByText(/^alice-/)).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.resume }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.forget }).click();
  await expect(card).toHaveCount(0);

  // The folder is trusted now, so starting it again goes straight to ready.
  await panel.getByRole("button", { name: m.start }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
});

test("another person sees only their own home and servers", async ({ page }) => {
  await signIn(page, "bob@example.com");
  await expect(page.getByText(m.signedInAs("bob@example.com"))).toBeVisible();
  await selectInProjects(page, "bob-demo");
  const panel = page.getByRole("region", { name: "projects/bob-demo", exact: true });
  await expect(panel.getByText(m.notConnected.start)).toBeVisible();
  await expect(panel.getByRole("button", { name: m.start })).toHaveCount(0);
  await expect(page.getByText("alice-demo")).toHaveCount(0);
  await expect(page.getByRole("region", { name: m.runningTitle }).getByText(m.runningEmpty)).toBeVisible();
  await expect(page.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await expect(page.getByRole("link", { name: m.navUsers })).toHaveCount(0);

  // Even asked directly, from bob's own signed-in page, rcman acts only in bob's home.
  const status = await page.evaluate(async () => {
    const r = await fetch("/api/servers/stop", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: "projects/alice-demo" }),
    });
    return r.status;
  });
  expect(status).toBe(404);
});

test("administrators see everyone's servers, and act on none of them", async ({ page, browser }) => {
  // bob runs a server, as his own Linux account.
  await signIn(page, "bob@example.com");
  await expect(page.getByText(m.signedInAs("bob@example.com"))).toBeVisible();
  await expect(page.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await connectClaude(page);
  await selectInProjects(page, "bob-demo");
  const start = page.getByRole("region", { name: "projects/bob-demo", exact: true }).getByRole("button", { name: m.start });
  await expect(start).toBeVisible(); // now that his account is connected
  await start.click();
  const card = page.getByRole("article", { name: "projects/bob-demo" });
  await card.getByRole("button", { name: m.trustYes }).click();
  await expect(card.getByText(m.states.ready, { exact: true })).toBeVisible();

  // alice, an administrator, sees it: rcman listed it through sudo -u bob.
  const admin = await browser.newPage();
  await signIn(admin, "alice@example.com");
  await openAdminPage(admin, m.navEveryone);
  const bob = admin.getByRole("region", { name: m.everyonePerson("bob", "bob@example.com") });
  await expect(bob.getByRole("row", { name: /projects\/bob-demo/ }).getByText(m.states.ready, { exact: true })).toBeVisible();
  const alice = admin.getByRole("region", { name: m.everyonePerson("alice", "alice@example.com") });
  await expect(alice.getByRole("row", { name: /projects\/alice-demo/ })).toBeVisible();
  await expect(admin.getByRole("main").getByRole("button")).toHaveCount(0);
  await admin.close();

  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
});

test("an address that is not on the list gets nowhere", async ({ page }) => {
  await signIn(page, "stranger@example.com");
  await expect(page.getByText(/403|Forbidden|Permission Denied/i).first()).toBeVisible();
  // No session came of it: rcman still sends this browser to sign in.
  const redirected = await page.evaluate(async () => (await fetch("/api/me", { redirect: "manual" })).type);
  expect(redirected).toBe("opaqueredirect");
});

test("the audit trail records what people did", async ({ page }) => {
  await signIn(page, "alice@example.com");
  await openAdminPage(page, m.navAudit);
  for (const action of ["claude.login", "claude.connect", "server.start", "server.confirm", "server.stop", "server.resume", "server.forget"]) {
    await expect(page.getByRole("cell", { name: action, exact: true }).first()).toBeVisible();
  }
  await expect(page.getByRole("cell", { name: "bob@example.com (bob)" }).first()).toBeVisible(); // bob's refused stop
});

test("people are removed, with and without their home", async ({ page }) => {
  await signIn(page, "alice@example.com");
  await openAdminPage(page, m.navUsers);
  for (const [linux, choice] of [
    ["carol", m.usersKeepHome],
    ["dave", m.usersDeleteHome],
  ]) {
    await page.getByRole("row", { name: new RegExp(linux) }).getByRole("button", { name: m.usersRemove }).click();
    const dialog = page.getByRole("dialog", { name: m.usersRemoveTitle(linux) });
    await dialog.getByLabel(choice).check();
    await dialog.getByRole("button", { name: m.usersRemoveConfirm }).click();
    await expect(page.getByText(m.usersRemoved(linux))).toBeVisible();
    await expect(page.getByRole("row", { name: new RegExp(linux) })).toHaveCount(0);
  }
});
