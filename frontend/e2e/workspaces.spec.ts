import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { makeWorkspace, openAdminPage, workspaces } from "./nav";
import { ALICE, BOB, signInAs } from "./people";

test("a workspace is made, and is ready", async ({ page }) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  const made = await makeWorkspace(page, "alice-made");
  await expect(made.getByRole("button", { name: m.start })).toBeVisible();
  await expect(made.getByText(m.noServer)).toBeVisible();
  const entry = workspaces(page).getByRole("button", { name: "alice-made", exact: true });
  await expect(entry).toHaveAttribute("aria-current", "true");
  expect(new URL(page.url()).searchParams.get("workspace")).toMatch(/^\d+$/);

  // the address opens it again
  await page.reload();
  await expect(made.getByRole("button", { name: m.start })).toBeVisible();
});

test("a name rcman refuses is shown under its label, and kept", async ({ page }) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  const form = page.getByRole("region", { name: m.newWorkspace });
  await form.getByLabel(m.newWorkspaceName).fill("not a name!");
  await form.getByRole("button", { name: m.create }).click();
  await expect(form.getByRole("alert")).toContainText(`${m.newWorkspaceName}:`);
  await expect(form.getByLabel(m.newWorkspaceName)).toHaveValue("not a name!");
});

test("a server's life: start, trust, stop, resume, stop and forget", async ({ page }) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  const alpha = await makeWorkspace(page, "alice-alpha");
  await alpha.getByRole("button", { name: m.start }).click();
  const card = alpha.getByRole("article", { name: "alice-alpha" });
  await expect(card.getByText(m.trustTitle)).toBeVisible();
  await card.getByRole("button", { name: m.trustYes }).click();
  const live = card
    .getByText(m.states.ready, { exact: true })
    .or(card.getByText(m.states.connected, { exact: true }));
  await expect(live).toBeVisible();
  await expect(card.getByText(/^alice-alpha-/)).toBeVisible();
  await expect(card.getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    /environment=/,
  );
  await expect(alpha.getByRole("button", { name: m.start })).toHaveCount(0);
  await expect(
    workspaces(page).getByRole("button", { name: "alice-alpha", exact: true }),
  ).toHaveAccessibleDescription(m.kinds.running);

  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.resume }).click();
  await expect(live).toBeVisible();
  await card.getByRole("button", { name: m.stop }).click();
  await expect(card.getByText(m.states.stopped, { exact: true })).toBeVisible();
  await card.getByRole("button", { name: m.forget }).click();
  await expect(card).toHaveCount(0);
  await expect(alpha.getByRole("button", { name: m.start })).toBeVisible();
});

test("a start in a workspace whose Claude is not signed in is refused, saying so", async ({
  page,
}) => {
  await signInAs(page, BOB);
  await page.goto("/");
  const made = await makeWorkspace(page, "bob-alpha");
  await made.getByRole("button", { name: m.start }).click();
  // the server's own sentence for it, PROBLEMS["claude_not_logged_in"] in backend/rcman/problems.py
  await expect(made.getByRole("alert")).toContainText("Claude is not signed in in this workspace.");
  await expect(made.getByRole("article")).toHaveCount(0);
  await expect(made.getByRole("button", { name: m.start })).toBeVisible();
});

test("a workspace is deleted with its server, after asking", async ({ page }) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  await makeWorkspace(page, "alice-kept");
  const doomed = await makeWorkspace(page, "alice-doomed");
  await doomed.getByRole("button", { name: m.start }).click();
  const card = doomed.getByRole("article", { name: "alice-doomed" });
  await card.getByRole("button", { name: m.trustYes }).click();
  await expect(card.getByRole("link", { name: m.openSession })).toBeVisible();

  await doomed.getByRole("button", { name: m.deleteWorkspace }).click();
  const dialog = page.getByRole("dialog", { name: m.deleteWorkspaceTitle("alice-doomed") });
  await expect(dialog.getByText(m.deleteWorkspaceBody)).toBeVisible();
  await dialog.getByRole("button", { name: m.deleteWorkspaceConfirm }).click();

  await expect(page.getByRole("status")).toHaveText(m.workspaceDeleted("alice-doomed"));
  expect(new URL(page.url()).searchParams.get("workspace")).toBeNull();
  await expect(
    workspaces(page).getByRole("button", { name: "alice-doomed", exact: true }),
  ).toHaveCount(0);
  const left = await page.evaluate(async () =>
    (await (await fetch("/api/workspaces")).json()).workspaces.map((w: { name: string }) => w.name),
  );
  expect(left).toContain("alice-kept"); // positive control: the listing is there
  expect(left).not.toContain("alice-doomed");
});

test("people never see each other's workspaces, nor reach them by id", async ({
  page,
  browser,
}) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  const mine = await makeWorkspace(page, "alice-private");
  await expect(mine).toBeVisible();
  const id = new URL(page.url()).searchParams.get("workspace")!;

  const bob = await browser.newPage();
  await signInAs(bob, BOB);
  await bob.goto(`/?workspace=${id}`);
  await expect(bob.getByText(m.workspaceUnknown)).toBeVisible();
  await expect(bob.getByText("alice-private")).toHaveCount(0);
  const answers = await bob.evaluate(async (theirs) => {
    const post = (path: string) =>
      fetch(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ workspace: Number(theirs) }),
      }).then((r) => r.status);
    return {
      start: await post("/api/servers"),
      output: (await fetch(`/api/servers/output?workspace=${theirs}`)).status,
      remove: (await fetch(`/api/workspaces/${theirs}`, { method: "DELETE" })).status,
    };
  }, id);
  expect(answers).toEqual({ start: 404, output: 404, remove: 404 });
  await bob.close();
  await page.reload();
  await expect(mine).toBeVisible(); // still there
});

test("administrators see everyone's workspaces, and act on none of them", async ({
  page,
  browser,
}) => {
  // A server's start, its trust question, two pages, and a stop that may wait for
  // Claude to exit: longer than one action's share of the default timeout.
  test.slow();
  await signInAs(page, ALICE);
  await page.goto("/");
  const seen = await makeWorkspace(page, "alice-seen");
  await seen.getByRole("button", { name: m.start }).click();
  const card = seen.getByRole("article", { name: "alice-seen" });
  await card.getByRole("button", { name: m.trustYes }).click();
  await expect(card.getByRole("link", { name: m.openSession })).toBeVisible();
  // Ready, with its session's name: the page below reads everyone's once, as it opens.
  await expect(card.getByText(/^alice-seen-/)).toBeVisible();

  await openAdminPage(page, m.navEveryone);
  await expect(page.getByRole("heading", { name: m.everyoneTitle })).toBeVisible();
  const alice = page.getByRole("region", { name: ALICE });
  const row = alice.getByRole("row", { name: /alice-seen/ });
  await expect(row.getByText(m.states.ready).or(row.getByText(m.states.connected))).toBeVisible();
  await expect(row.getByText(/^alice-seen-/)).toBeVisible();
  await expect(page.getByRole("region", { name: BOB })).toBeVisible();
  await expect(page.getByRole("main").getByRole("button")).toHaveCount(0);

  const bob = await browser.newPage();
  await signInAs(bob, BOB);
  await bob.goto("/");
  await expect(bob.getByText(m.signedInAs(BOB))).toBeVisible();
  await expect(bob.getByRole("button", { name: m.navAdmin })).toHaveCount(0);
  const refused = await bob.evaluate(async () => (await fetch("/api/servers/everyone")).status);
  expect(refused).toBe(403);
  await bob.close();

  // Wait for the stop to finish: the run ends when the test does, and a stop
  // still in flight would be cut off.
  await page.goBack();
  const stopped = page.waitForResponse((r) => r.url().endsWith("/api/servers/stop"));
  await card.getByRole("button", { name: m.stop }).click();
  expect((await stopped).ok()).toBe(true);
});

test("a refresh that cannot reach rcman says so, goes once rcman answers, and is reported", async ({
  page,
  context,
}) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  await expect(workspaces(page)).toBeVisible();
  const reported = page.waitForRequest(
    (r) => r.method() === "POST" && new URL(r.url()).pathname === "/api/client-errors",
  );
  await context.setOffline(true);
  const alert = page.getByRole("alert").filter({ hasText: m.unreachable });
  await expect(alert).toBeVisible();
  await context.setOffline(false);
  const request = await reported;
  const report = request.postDataJSON();
  expect(report).toMatchObject({ kind: "unreachable", method: "GET", path: "/api/workspaces" });
  expect(report.count).toBeGreaterThanOrEqual(1);
  expect((await request.response())?.status()).toBe(200);
  await expect(alert).toHaveCount(0);
  await expect(page.getByText(m.unexpectedResponse)).toHaveCount(0);
});
