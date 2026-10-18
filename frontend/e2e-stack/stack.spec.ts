// The whole stack, signed in to through Caddy and oauth2-proxy, with the
// people tests/container/stack.py lets in and no workspace made beforehand. Claude
// is the fake one, which takes the code tests/container/test_stack_in_a_browser.py passes in
// RCMAN_FAKE_CLAUDE_CODE. Runs in order against one stack.
import { execFileSync } from "node:child_process";

import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  deleteWorkspace,
  expectSignedInAs,
  makeWorkspace,
  newWorkspace,
  openAdminPage,
  panel,
  runInTerminal,
  terminalLines,
  workspaces,
} from "../e2e/nav";
import { m } from "../src/messages";
import { FIRST_WORKSPACE_WITHIN, TEST_WITH_FIRST_WORKSPACE_WITHIN } from "./timing";

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

// What the suite gives to run a command in the stack's container, as JSON: the
// command up to the one to run (tests/container/test_stack_in_a_browser.py).
const IN_HOST: string[] = JSON.parse(process.env.RCMAN_STACK_EXEC ?? "[]");

// Makes a repository in a workspace with a bare origin of its own, and changes to stage
// (tests/container/stack.py), as Claude would leave one.
function makeRepository(workspace: number) {
  expect(IN_HOST).not.toEqual([]);
  const [command, ...rest] = IN_HOST;
  execFileSync(
    command,
    [...rest, "python3", "tests/container/stack.py", "repository", String(workspace)],
    { stdio: "pipe" },
  );
}

// The stand-in GitHub the stack holds at github.com (tests/container/stack.py): the token it
// takes and the addresses of its public and its private repository, which the container
// suite passes as JSON (tests/container/test_stack_in_a_browser.py).
const GITHUB: { token: string; public: string; private: string } = JSON.parse(
  process.env.RCMAN_STACK_GITHUB ?? '{"token":"","public":"","private":""}',
);

// The fake claude's one accepted code (devtools/fake_claude.py, ACCEPTED_CODE).
const CODE = process.env.RCMAN_FAKE_CLAUDE_CODE ?? "";

test("a workspace made before Claude is connected starts no server, and says why", async ({
  page,
}) => {
  test.setTimeout(TEST_WITH_FIRST_WORKSPACE_WITHIN); // "plain" is alice's first workspace
  expect(CODE).not.toBe("");
  await signIn(page, ALICE);
  await expectSignedInAs(page, ALICE);
  await expect(workspaces(page).getByText(m.workspacesEmpty)).toBeVisible();
  await makeWorkspace(page, "plain", FIRST_WORKSPACE_WITHIN);
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

test("changes in a workspace's repository are staged, committed and pushed from its panel", async ({
  page,
}) => {
  await signIn(page, ALICE);
  const id = await idOf(page, "after");
  await page.goto(`/?workspace=${id}`);
  const opened = panel(page, "after");
  const changes = opened.getByRole("region", { name: m.changesTitle });
  // A new workspace holds no repository, which the panel says without raising an alert.
  await expect(changes.getByText(m.notARepository)).toBeVisible();
  await expect(opened.getByRole("alert")).toHaveCount(0);

  makeRepository(id);
  await changes.getByRole("button", { name: m.refreshStatus }).click();
  await expect(changes.getByText(m.notARepository)).toHaveCount(0);
  await expect(changes.getByRole("button", { name: m.onBranch("main") })).toBeVisible();

  // Committing needs an author name, which the section links to Settings for.
  await changes.getByRole("link", { name: m.commitNeedsAuthor.link }).click();
  await page.getByLabel(m.authorNameLabel).fill("Alice");
  const author = page.getByRole("region", { name: m.settingsAuthorTitle });
  await author.getByRole("button", { name: m.save }).click();
  await expect(author.getByText(m.saved)).toBeVisible();
  await page.goto(`/?workspace=${id}`);

  const notStaged = changes.getByRole("group", { name: m.notStagedTitle });
  await expect(notStaged.getByText("notes.txt")).toBeVisible();
  await expect(notStaged.getByText("hello.txt")).toBeVisible();
  await notStaged.getByRole("checkbox", { name: m.selectAll }).check();
  await notStaged.getByRole("button", { name: m.stageSelected }).click();
  const staged = changes.getByRole("group", { name: m.stagedTitle });
  await expect(staged.getByText("notes.txt")).toBeVisible();
  await expect(staged.getByText("hello.txt")).toBeVisible();

  await changes.getByLabel(m.commitMessage).fill("Add notes");
  await changes.getByRole("button", { name: m.commit, exact: true }).click();
  await expect(changes.getByText(m.commitNothingStaged)).toBeVisible();

  await changes.getByRole("button", { name: m.push, exact: true }).click();
  await expect(changes.getByText(m.pushed("main", "origin/main"))).toBeVisible();
});

test("a workspace is made by cloning from GitHub, a private repository once a token is stored", async ({
  page,
}) => {
  expect(GITHUB.token).not.toBe("");
  await signIn(page, ALICE);

  // A public repository, with no name: named after the repository, being made, then ready.
  let form = await newWorkspace(page);
  await form.getByLabel(m.newWorkspaceSource).fill(GITHUB.public);
  await form.getByRole("button", { name: m.create }).click();
  const publicOne = panel(page, "stack-public");
  await expect(publicOne.getByText(m.workspaceCreating)).toBeVisible();
  await expect(publicOne.getByRole("button", { name: m.start })).toBeVisible();
  const changes = publicOne.getByRole("region", { name: m.changesTitle });
  await expect(changes.getByRole("button", { name: m.onBranch("main") })).toBeVisible();
  await expect(publicOne.getByRole("figure", { name: m.workspaceLog })).toHaveCount(0);

  // A private one, without a token: it fails, in plain words and with what git said.
  form = await newWorkspace(page);
  await form.getByLabel(m.newWorkspaceSource).fill(GITHUB.private);
  await form.getByRole("button", { name: m.create }).click();
  const privateOne = panel(page, "stack-private");
  await expect(privateOne.getByText(m.workspaceFailed)).toBeVisible();
  // a plain sentence in the open; the raw words are behind Details
  await expect(privateOne.getByText(/^GitHub asked for a sign-in/).first()).toBeVisible();
  await privateOne.getByText(m.detailsTitle, { exact: true }).click();
  const said = privateOne.locator("pre.workspace-error");
  await expect(said).toBeVisible();
  // the server's sentence, CLONE_MESSAGES["private_no_token"] in backend/rcman/problems.py
  await expect(said).toContainText("GitHub asked for a sign-in");
  await expect(said).toContainText("git clone failed, exit code 128:");
  await expect(said).toContainText("could not read Username");
  await expect(privateOne.getByRole("figure", { name: m.workspaceLog })).toContainText(
    "The clone failed",
  );
  await deleteWorkspace(page, "stack-private");

  // With a token stored in Settings, the same address is cloned.
  await page.getByRole("banner").getByRole("link", { name: m.navSettings }).click();
  const github = page.getByRole("region", { name: m.settingsGithubTitle });
  await github.getByLabel(m.githubTokenLabel).fill(GITHUB.token);
  await github.getByRole("button", { name: m.save }).click();
  await expect(github.getByText(m.githubTokenSaved)).toBeVisible();
  await page.getByRole("banner").getByRole("link", { name: m.navDashboard }).click();
  form = await newWorkspace(page);
  await form.getByLabel(m.newWorkspaceSource).fill(GITHUB.private);
  await form.getByRole("button", { name: m.create }).click();
  const again = panel(page, "stack-private");
  await expect(again.getByRole("button", { name: m.start })).toBeVisible();
  await expect(
    again.getByRole("region", { name: m.changesTitle }).getByRole("button", {
      name: m.onBranch("main"),
    }),
  ).toBeVisible();
  await expect(again.getByText(m.workspaceFailed)).toHaveCount(0);
});

test("another person sees none of it, and reaching it by its id finds nothing", async ({
  page,
  browser,
}) => {
  const alice = await browser.newPage();
  await signIn(alice, ALICE);
  await expectSignedInAs(alice, ALICE);
  const theirs = await idOf(alice, "before");
  await alice.close();

  await signIn(page, BOB);
  await expectSignedInAs(page, BOB);
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
  test.setTimeout(TEST_WITH_FIRST_WORKSPACE_WITHIN); // "carol-box" is carol's first workspace
  // carol has a workspace of her own, which the People page later removes with her.
  const carol = await browser.newPage();
  await signIn(carol, CAROL);
  await makeWorkspace(carol, "carol-box", FIRST_WORKSPACE_WITHIN);
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
  await expectSignedInAs(dave, DAVE);
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
    "git.stage",
    "git.commit",
    "git.push",
    "git.clone",
    "person.add",
    "person.delete",
  ]) {
    // the page says each action in plain words, from the same catalogue as the unit tests
    await expect(
      page.getByRole("cell", { name: m.auditActions[action], exact: true }).first(),
    ).toBeVisible();
  }
  // bob's refused stop: the whole row, as rcman recorded it; a failed result is the word for a
  // failure and the code of the error the request failed with
  let refusedStop = page.getByRole("row");
  for (const cell of [BOB, m.auditActions["server.stop"], `${m.auditFailed} workspace_not_found`])
    refusedStop = refusedStop.filter({ has: page.getByRole("cell", { name: cell, exact: true }) });
  await expect(refusedStop).toHaveCount(1);
  // carol's removal, by her address
  let removal = page.getByRole("row");
  for (const cell of [ALICE, m.auditActions["person.delete"], CAROL])
    removal = removal.filter({ has: page.getByRole("cell", { name: cell, exact: true }) });
  await expect(removal).toHaveCount(1);
});

// Last, so that nothing before it depends on the workspace it makes.
test("a workspace's terminal runs a shell in its container, through Caddy and oauth2-proxy", async ({
  page,
}) => {
  await signIn(page, ALICE);
  await expectSignedInAs(page, ALICE);
  const made = await makeWorkspace(page, "terminal");
  await made.getByRole("tab", { name: m.tabTerminal }).click();
  await expect(made.getByRole("status")).toHaveText(m.terminalConnected);

  await runInTerminal(page, made, "echo $((6*7))");
  await expect.poll(() => terminalLines(made)).toContain("42");
  // the default image's user: the container's root
  await runInTerminal(page, made, "echo uid=$(id -u)");
  await expect.poll(() => terminalLines(made)).toContain("uid=0");

  // a reload opens it again, at the tab the address keeps
  await page.reload();
  await expect(made.getByRole("tab", { name: m.tabTerminal })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(made.getByRole("status")).toHaveText(m.terminalConnected);
  await runInTerminal(page, made, "exit 3");
  await expect(made.getByRole("status")).toHaveText(m.terminalExited(3));
  await expect(made.getByRole("button", { name: m.terminalReconnect })).toBeVisible();
});
