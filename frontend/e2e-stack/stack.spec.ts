// rcman as installed, signed in to through Caddy and oauth2-proxy, with the
// people tests/container/stack.py adds and no workspace made beforehand. The
// stack signs in the fake claude of every workspace whose name begins with
// "signed-" as it is made, and of no other. Runs in order against one
// installation.
import { expect, type Page, test } from "@playwright/test";

import { makeWorkspace, openAdminPage, workspaces } from "../e2e/nav";
import { m } from "../src/messages";

const ALICE = "alice@example.com"; // an administrator
const BOB = "bob@example.com";
const CAROL = "carol@example.com";
const DAVE = "dave@example.com"; // added by the People page

async function signIn(page: Page, email: string) {
  await page.goto("/");
  await expect(page).toHaveURL(/auth\.test\/authorize/); // sent to the sign-in
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
}

// The id of one of the signed-in person's workspaces, by name.
async function idOf(page: Page, name: string): Promise<number> {
  return page.evaluate(async (wanted) => {
    const listed = (await (await fetch("/api/workspaces")).json()).workspaces;
    return listed.find((w: { name: string }) => w.name === wanted).id;
  }, name);
}

test("a person signs in, makes a workspace, and runs its server", async ({ page }) => {
  await signIn(page, ALICE);
  await expect(page.getByText(m.signedInAs(ALICE))).toBeVisible();
  await expect(workspaces(page).getByText(m.workspacesEmpty)).toBeVisible();

  const alpha = await makeWorkspace(page, "signed-alpha");
  await alpha.getByRole("button", { name: m.start }).click();
  const card = alpha.getByRole("article", { name: "signed-alpha" });
  await card.getByRole("button", { name: m.trustYes }).click();
  const live = card
    .getByText(m.states.ready, { exact: true })
    .or(card.getByText(m.states.connected, { exact: true }));
  await expect(live).toBeVisible();
  await expect(card.getByText(/^signed-alpha-/)).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.resume }).click();
  await expect(live).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
});

test("a workspace whose Claude is not signed in starts no server, and says why", async ({
  page,
}) => {
  await signIn(page, ALICE);
  const plain = await makeWorkspace(page, "plain");
  await plain.getByRole("button", { name: m.start }).click();
  await expect(plain.getByRole("alert")).toContainText(
    "Claude is not signed in in this workspace.",
  );
  await expect(plain.getByRole("article")).toHaveCount(0);
});

test("another person sees none of it, and reaching it by its id finds nothing", async ({
  page,
  browser,
}) => {
  const alice = await browser.newPage();
  await signIn(alice, ALICE);
  await expect(alice.getByText(m.signedInAs(ALICE))).toBeVisible();
  const theirs = await idOf(alice, "signed-alpha");
  await alice.close();

  await signIn(page, BOB);
  await expect(page.getByText(m.signedInAs(BOB))).toBeVisible();
  await expect(workspaces(page).getByText(m.workspacesEmpty)).toBeVisible();
  await expect(page.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await page.goto(`/?workspace=${theirs}`);
  await expect(page.getByText(m.workspaceUnknown)).toBeVisible();
  await expect(page.getByText("signed-alpha")).toHaveCount(0);

  // Even asked directly, from bob's own signed-in page, rcman finds no such workspace.
  const answers = await page.evaluate(async (id) => {
    const post = async (path: string) => {
      const r = await fetch(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ workspace: id }),
      });
      return [r.status, (await r.json()).code];
    };
    const removed = await fetch(`/api/workspaces/${id}`, { method: "DELETE" });
    return {
      start: await post("/api/servers"),
      stop: await post("/api/servers/stop"),
      remove: [removed.status, (await removed.json()).code],
    };
  }, theirs);
  expect(answers).toEqual({
    start: [404, "workspace_not_found"],
    stop: [404, "workspace_not_found"],
    remove: [404, "workspace_not_found"],
  });
});

test("an address that is not on the list gets nowhere", async ({ page }) => {
  await signIn(page, "stranger@example.com");
  await expect(page.getByText(/403|Forbidden|Permission Denied/i).first()).toBeVisible();
  // No session came of it: rcman still sends this browser to sign in.
  const redirected = await page.evaluate(
    async () => (await fetch("/api/me", { redirect: "manual" })).type,
  );
  expect(redirected).toBe("opaqueredirect");
});

test("administrators see everyone's workspaces, and act on none of them", async ({
  page,
  browser,
}) => {
  // carol has a workspace of her own, which the People page later removes with her.
  const carol = await browser.newPage();
  await signIn(carol, CAROL);
  await makeWorkspace(carol, "carol-box");
  await carol.close();

  await signIn(page, ALICE);
  await openAdminPage(page, m.navEveryone);
  await expect(page.getByRole("heading", { name: m.everyoneTitle })).toBeVisible();
  const alice = page.getByRole("region", { name: ALICE });
  await expect(alice.getByRole("row", { name: /signed-alpha/ })).toContainText(m.states.stopped);
  await expect(alice.getByRole("row", { name: /plain/ })).toContainText(m.everyoneNoServer);
  await expect(
    page.getByRole("region", { name: CAROL }).getByRole("row", { name: /carol-box/ }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: BOB }).getByText(m.everyoneNone)).toBeVisible();
  await expect(page.getByRole("main").getByRole("button")).toHaveCount(0);
});

test("the People page adds a person and removes another, with her workspaces", async ({
  page,
  browser,
}) => {
  await signIn(page, ALICE);
  await openAdminPage(page, m.navUsers);
  await page.getByLabel(m.usersEmail).fill(DAVE);
  await page.getByRole("button", { name: m.usersAdd }).click();
  await expect(page.getByText(m.usersAdded(DAVE))).toBeVisible();
  await expect(page.getByRole("row", { name: new RegExp(DAVE) })).toBeVisible();

  // dave is let in at once
  const dave = await browser.newPage();
  await signIn(dave, DAVE);
  await expect(dave.getByText(m.signedInAs(DAVE))).toBeVisible();
  await dave.close();

  const carol = page.getByRole("row", { name: new RegExp(CAROL) });
  await expect(carol.getByRole("cell", { name: "1", exact: true })).toBeVisible();
  await carol.getByRole("button", { name: m.usersRemove }).click();
  const dialog = page.getByRole("dialog", { name: m.usersRemoveTitle(CAROL) });
  await expect(dialog.getByText(m.usersRemoveBody)).toBeVisible();
  await dialog.getByRole("button", { name: m.usersRemoveConfirm }).click();
  await expect(page.getByText(m.usersRemoved(CAROL))).toBeVisible();
  await expect(carol).toHaveCount(0);

  await openAdminPage(page, m.navEveryone);
  await expect(page.getByRole("region", { name: ALICE })).toBeVisible(); // positive control
  await expect(page.getByRole("region", { name: CAROL })).toHaveCount(0);
  await expect(page.getByText("carol-box")).toHaveCount(0);
});

test("the audit trail records what people did", async ({ page }) => {
  await signIn(page, ALICE);
  await openAdminPage(page, m.navAudit);
  for (const action of [
    "workspace.create",
    "server.start",
    "server.confirm",
    "server.stop",
    "server.resume",
    "person.add",
    "person.delete",
  ]) {
    await expect(page.getByRole("cell", { name: action, exact: true }).first()).toBeVisible();
  }
  // bob's refused stop: the whole row, as rcman recorded it
  let refusedStop = page.getByRole("row");
  for (const cell of [BOB, "server.stop", "workspace_not_found"])
    refusedStop = refusedStop.filter({ has: page.getByRole("cell", { name: cell, exact: true }) });
  await expect(refusedStop).toHaveCount(1);
  // carol's removal, by her address
  let removal = page.getByRole("row");
  for (const cell of [ALICE, "person.delete", CAROL])
    removal = removal.filter({ has: page.getByRole("cell", { name: cell, exact: true }) });
  await expect(removal).toHaveCount(1);
});
