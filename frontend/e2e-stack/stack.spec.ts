// rcman as installed, signed in to through Caddy and oauth2-proxy, with the
// people tests/container/stack.py adds and no workspace made beforehand. Claude
// is the fake one, which takes the code tests/test_container_suite.py passes in
// RCMAN_FAKE_CLAUDE_CODE. Runs in order against one installation.
import { expect, type Locator, type Page, test } from "@playwright/test";

import { makeWorkspace, openAdminPage, panel, workspaces } from "../e2e/nav";
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

// The fake claude's one accepted code (devtools/fake_claude.py, ACCEPTED_CODE).
const CODE = process.env.RCMAN_FAKE_CLAUDE_CODE ?? "";

test("a workspace made before Claude is connected starts no server, and says why", async ({
  page,
}) => {
  expect(CODE).not.toBe("");
  await signIn(page, ALICE);
  await expect(page.getByText(m.signedInAs(ALICE))).toBeVisible();
  await expect(workspaces(page).getByText(m.workspacesEmpty)).toBeVisible();
  await makeWorkspace(page, "plain");
  const before = await makeWorkspace(page, "before");
  await before.getByRole("button", { name: m.start }).click();
  await expect(before.getByRole("alert")).toContainText(
    "Connect your Claude account on the Claude page.",
  );
  await expect(before.getByRole("article")).toHaveCount(0);
});

async function runs(panel: Locator, name: string) {
  await panel.getByRole("button", { name: m.start }).click();
  const card = panel.getByRole("article", { name });
  await card.getByRole("button", { name: m.trustYes }).click();
  const live = card
    .getByText(m.states.ready, { exact: true })
    .or(card.getByText(m.states.connected, { exact: true }));
  await expect(live).toBeVisible();
  await expect(card.getByText(new RegExp(`^${name}-`))).toBeVisible();
  return { card, live };
}

test("a person connects Claude once, and workspaces made before and after both run", async ({
  page,
}) => {
  await signIn(page, ALICE);
  await page.getByRole("banner").getByRole("link", { name: m.navClaude }).click();
  await expect(page.getByText(m.claudeNotConnected)).toBeVisible();
  await page.getByRole("button", { name: m.connect }).click();
  await page.getByRole("button", { name: m.connectGo }).click();
  await expect(page.getByRole("link", { name: m.connectLink })).toBeVisible();
  await page.getByLabel(m.connectCode).fill(CODE);
  await page.getByRole("button", { name: m.connectSubmit }).click();
  await expect(page.getByText(m.claudeConnectedAs(ALICE))).toBeVisible();

  await page.goto(`/?workspace=${await idOf(page, "before")}`);
  const made = await runs(panel(page, "before"), "before");
  await made.card.getByRole("button", { name: m.stop }).click();
  await expect(made.card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await made.card.getByRole("button", { name: m.resume }).click();
  await expect(made.live).toBeVisible();
  await made.card.getByRole("button", { name: m.stop }).click();
  await expect(made.card.getByText(m.states.stopped, { exact: true })).toBeVisible();

  const after = await runs(await makeWorkspace(page, "after"), "after");
  await after.card.getByRole("button", { name: m.stop }).click();
  await expect(after.card.getByText(m.states.stopped, { exact: true })).toBeVisible();
});

test("another person sees none of it, and reaching it by its id finds nothing", async ({
  page,
  browser,
}) => {
  const alice = await browser.newPage();
  await signIn(alice, ALICE);
  await expect(alice.getByText(m.signedInAs(ALICE))).toBeVisible();
  const theirs = await idOf(alice, "before");
  await alice.close();

  await signIn(page, BOB);
  await expect(page.getByText(m.signedInAs(BOB))).toBeVisible();
  await expect(workspaces(page).getByText(m.workspacesEmpty)).toBeVisible();
  await expect(page.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  await page.goto(`/?workspace=${theirs}`);
  await expect(page.getByText(m.workspaceUnknown)).toBeVisible();
  await expect(page.getByText("before")).toHaveCount(0);

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
  await expect(alice.getByRole("row", { name: /before/ })).toContainText(m.states.stopped);
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
