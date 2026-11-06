// make screenshots (devtools/screenshots.py runs it as `node screenshots/capture.ts <origin>
// <folder>`, against the world of devtools/world.py): every page the sidebar links to, a help
// page standing for every help page, and the states and views a page only shows when asked —
// each `shot` call below names one — at a phone's and a desktop's width, light and dark, into
// the folder, which is emptied first. It is no test and asserts nothing about how they look;
// what it waits for is only what has to be there before a screenshot is worth taking. It sets
// no time limit: Ctrl-C ends a run that hangs.

import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";

import { type Browser, type BrowserContext, chromium, type Page, type Route } from "playwright";

import { m } from "../src/messages.ts";

const [origin, out] = process.argv.slice(2);
if (!origin || !out) throw new Error("usage: node screenshots/capture.ts <origin> <folder>");

// The people of the world devtools/world.py makes: alice, an administrator whose Claude is
// signed in, and bob, whose is not.
const ALICE = "alice@example.com";
const BOB = "bob@example.com";

const WORKSPACE = "shot-widget";
// Made after the first, so that the list has more than one.
const OTHERS = ["shot-docs-site", "shot-infra"];

// The repository the screenshot workspace shows, planted on it: the world's workspaces are
// plain ones, with no repository and no GitHub to clone one from.
const SOURCE = "https://github.com/octo/widget";
const CLEAN = {
  branch: {
    name: "main",
    commit: "3f9a1c2e7b4d5f60718293a4b5c6d7e8f9012345",
    upstream: "origin/main",
    ahead: 0,
    behind: 0,
  },
  merging: false,
  staged: { files: [], more: 0 },
  not_staged: { files: [], more: 0 },
  author: { name: "Alice", email: ALICE },
  push_has_remote: true,
  push_to_github: false,
  github_token: null,
  push_ahead: 0,
};
const BRANCHES = {
  local: { branches: [{ name: "main", current: true, upstream: "origin/main" }], more: 0 },
  remote: {
    branches: [
      { name: "origin/main", local: "main", taken_by: null },
      { name: "origin/feature", local: null, taken_by: null },
    ],
    more: 0,
  },
};

const REBUILD_STEPS = [
  "Reading .devcontainer/devcontainer.json",
  "Pulling mcr.microsoft.com/devcontainers/typescript-node:22",
  "Making the container again",
].join("\n");

// The states of the Container card and of a failed workspace that a screenshot shows: each by its
// name, what the listing gives and what the server says of the file.
const REBUILD_STATES: [string, Record<string, unknown>, boolean | null][] = [
  ["container-changed", { rebuildable: true }, true],
  [
    "container-rebuilding",
    { rebuildable: false, rebuild: { state: "preparing", log: REBUILD_STEPS, error: null } },
    null,
  ],
  [
    "container-rolled-back",
    {
      rebuildable: true,
      rebuild: {
        state: null,
        log: REBUILD_STEPS,
        error:
          "The rebuild failed, so the workspace was put back as it was: its container was made again from the image it had, and its create commands were run again. What the new commands wrote to the work folder stays. Why it failed is below.\npostCreateCommand failed, exit code 1.",
      },
    },
    true,
  ],
  [
    "failed-rebuildable",
    {
      state: "failed",
      rebuildable: true,
      server: null,
      error:
        "postCreateCommand failed, exit code 1. What it printed last is below.\nnpm ERR! missing script: setup",
      log: REBUILD_STEPS,
    },
    null,
  ],
];

type Device = { label: string; width: number; height: number; phone: boolean };
const DEVICES: Device[] = [
  { label: "phone", width: 390, height: 844, phone: true },
  { label: "desktop", width: 1280, height: 800, phone: false },
];

// A context of its own for each combination of scheme, size and person, signed in as
// oauth2-proxy would sign them in, with no time limit.
async function opened(
  browser: Browser,
  who: string,
  scheme: "light" | "dark" = "light",
  device: Device = DEVICES[1],
): Promise<[BrowserContext, Page]> {
  const context = await browser.newContext({
    baseURL: origin,
    colorScheme: scheme,
    viewport: { width: device.width, height: device.height },
    ...(device.phone ? { isMobile: true, hasTouch: true } : {}),
    extraHTTPHeaders: { "X-Forwarded-Email": who },
  });
  context.setDefaultTimeout(0);
  return [context, await context.newPage()];
}

type Listed = { id: number; name: string; state: string };

async function listed(page: Page): Promise<Listed[]> {
  return page.evaluate(async () => (await (await fetch("/api/workspaces")).json()).workspaces);
}

// alice's workspaces for the screenshots, made once and ready: the screenshot workspace's id.
async function workspaces(page: Page): Promise<number> {
  for (const name of [WORKSPACE, ...OTHERS]) {
    if ((await listed(page)).some((w) => w.name === name)) continue;
    await page.evaluate(
      (name) =>
        fetch("/api/workspaces", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ name }),
        }),
      name,
    );
  }
  for (;;) {
    const ours = (await listed(page)).filter((w) => [WORKSPACE, ...OTHERS].includes(w.name));
    const failed = ours.find((w) => w.state === "failed");
    if (failed) throw new Error(`workspace ${failed.name} failed`);
    if (ours.length === OTHERS.length + 1 && ours.every((w) => w.state === "ready"))
      return ours.find((w) => w.name === WORKSPACE)?.id ?? 0;
    await page.waitForTimeout(500);
  }
}

// Every page's path: the sidebar's links, as alice, an administrator, has them, and the first
// help page's.
async function pagePaths(page: Page): Promise<string[]> {
  await page.goto("/");
  const sidebar = page.getByRole("navigation", { name: m.navMain });
  await sidebar.getByRole("link", { name: m.navUsers, exact: true }).waitFor({ state: "attached" });
  const linked = await sidebar
    .getByRole("link")
    .evaluateAll((links) => links.map((a) => new URL((a as HTMLAnchorElement).href).pathname));
  const slugs: string[] = await page.evaluate(async () =>
    (await (await fetch("/api/docs")).json()).pages.map((p: { slug: string }) => p.slug),
  );
  return [...new Set(linked), ...(slugs[0] ? [`/help/${slugs[0]}`] : [])];
}

// The listing as the server gives it, the screenshot workspace with its repository's address and
// `extra` laid over it.
async function listing(route: Route, id: number, extra: Record<string, unknown> = {}) {
  if (route.request().method() !== "GET") return route.fallback();
  const answer = await (await route.fetch()).json();
  answer.workspaces = answer.workspaces.map((w: { id: number }) =>
    w.id === id ? { ...w, source: SOURCE, source_page: SOURCE, ...extra } : w,
  );
  await route.fulfill({ json: answer });
}

// The screenshot workspace's repository views, for every page of the context: a clean checkout
// of main, and its branches.
async function plantRepository(context: BrowserContext, id: number) {
  const ours = (route: Route) =>
    new URL(route.request().url()).searchParams.get("workspace") === String(id);
  await context.route("**/api/git/status*", (route) =>
    ours(route) ? route.fulfill({ json: CLEAN }) : route.fallback(),
  );
  await context.route("**/api/git/branches*", (route) =>
    ours(route) ? route.fulfill({ json: BRANCHES }) : route.fallback(),
  );
  await context.route("**/api/workspaces", (route) => listing(route, id));
}

// Changes to show in the Changes card: files staged and not, and a commit to push.
async function withChanges(page: Page) {
  await page.route("**/api/git/status*", (route) =>
    route.fulfill({
      json: {
        ...CLEAN,
        staged: {
          files: [
            { path: "src/app.ts", change: "modified" },
            { path: "README.md", change: "added" },
          ],
          more: 0,
        },
        not_staged: {
          files: [
            { path: "notes/todo.txt", change: "untracked" },
            { path: "src/old.ts", change: "deleted" },
          ],
          more: 0,
        },
        push_ahead: 1,
        branch: { ...CLEAN.branch, ahead: 1, behind: 0 },
      },
    }),
  );
}

// The screenshot workspace as the listing gives it, with `extra` laid over it, and what the server
// says of its devcontainer.json (`changed`).
async function asListed(
  page: Page,
  id: number,
  extra: Record<string, unknown>,
  changed: boolean | null,
) {
  await page.route("**/api/workspaces", (route) => listing(route, id, extra));
  await page.route(`**/api/workspaces/${id}/devcontainer`, (route) =>
    route.fulfill({ json: { changed } }),
  );
}

// Presses one of a person's actions on the People page, which it is on: the row's own button
// where it is shown, and on a phone, where a row's actions are behind its More actions button,
// that item of its menu.
async function personAction(page: Page, email: string, action: string) {
  const row = page.getByRole("row", { name: new RegExp(email) });
  const more = row.getByRole("button", { name: m.usersMoreActionsFor(email) });
  await row.waitFor();
  if (await more.isVisible()) await more.click();
  await row.getByRole("button", { name: action, exact: true }).click();
}

// Opens the Upgrade rcman page's question with the upgrade offered: the world's rcman has the
// upgrade from the web interface off, which offers no button.
async function openUpgradeDialog(page: Page) {
  await page.route("**/api/rcman/upgrade", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const answer = await (await route.fetch()).json();
    await route.fulfill({ json: { ...answer, enabled: true, available: true, reason: null } });
  });
  await page.goto("/admin/rcman");
  await page.getByRole("button", { name: m.upgradeRcman, exact: true }).click();
  await page.getByRole("dialog", { name: m.upgradeRcman }).waitFor();
}

// A screenshot's file name: the width's and scheme's name, then what it shows.
const named = (path: string) =>
  path === "/" ? "workspaces" : path.replace(/^\//, "").replace(/[/?=&]+/g, "-");

// Lets a page settle: its fonts, what it fetched, and what that drew — two frames pass, and every
// animation that ends has ended, a dialog's or a pop-up's opening among them (one that repeats
// without end, a spinner's, is left out).
async function settled(page: Page) {
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(async () => {
    await new Promise<void>((done) =>
      requestAnimationFrame(() => requestAnimationFrame(() => done())),
    );
    const ending = document
      .getAnimations()
      .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity);
    await Promise.all(ending.map((a) => a.finished.catch(() => undefined)));
  });
}

async function capture(
  browser: Browser,
  scheme: "light" | "dark",
  device: Device,
  paths: string[],
  id: number,
) {
  const { label, phone } = device;
  const shooter =
    (page: Page) =>
    async (what: string, fullPage = true) => {
      await settled(page);
      await page.screenshot({ path: join(out, `${label}-${scheme}-${what}.png`), fullPage });
    };

  const [context, page] = await opened(browser, ALICE, scheme, device);
  const shot = shooter(page);
  await plantRepository(context, id);

  for (const path of paths) {
    await page.goto(path);
    await page.getByRole("main").waitFor();
    await shot(named(path));
  }

  await page.goto("/");
  await page.getByRole("button", { name: m.newWorkspace, exact: true }).click();
  await shot("new-workspace", false);
  await page.keyboard.press("Escape");

  await withChanges(page);
  await page.goto(`/?workspace=${id}`);
  await page.getByRole("tab", { name: m.tabOverview }).waitFor();
  // the Changes card's status, which withChanges gives
  await page
    .getByRole("region", { name: m.changesTitle })
    .getByText("README.md", { exact: false })
    .waitFor();
  await shot("workspace-overview");
  if (phone) {
    await page.getByRole("button", { name: m.switchWorkspaceFrom(WORKSPACE) }).click();
    await shot("switcher", false);
    await page.keyboard.press("Escape");
    await page.getByRole("banner").getByRole("button", { name: m.navMenu }).click();
    await shot("drawer", false);
    await page.keyboard.press("Escape");
  }
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // the Container card with the file changed, a rebuild under way and one rolled back, and
  // a failed workspace offered a rebuild
  for (const [what, extra, changed] of REBUILD_STATES) {
    await asListed(page, id, extra, changed);
    await page.goto(`/?workspace=${id}`);
    await page.getByRole("heading", { name: WORKSPACE, level: 2 }).waitFor();
    await shot(what);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
  await asListed(page, id, { rebuildable: true }, true);
  await page.goto(`/?workspace=${id}`);
  await page
    .getByRole("region", { name: m.containerTitle })
    .getByRole("button", { name: m.rebuild })
    .click();
  await shot("rebuild-workspace", false);
  await page.keyboard.press("Escape");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.goto(`/?workspace=${id}&tab=terminal`);
  await page.getByText(m.terminalConnected).waitFor();
  await shot("workspace-terminal", false);

  await page.goto("/admin/people");
  if (phone) {
    await page
      .getByRole("row", { name: new RegExp(BOB) })
      .getByRole("button", { name: m.usersMoreActionsFor(BOB) })
      .click();
    await shot("people-menu", false);
    await page.keyboard.press("Escape");
  }
  await personAction(page, ALICE, m.usersLimits);
  await page.getByRole("form", { name: m.limitsPersonScope(ALICE) }).waitFor();
  await shot("limits", false);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: m.usersAdd }).click();
  await shot("add-person", false);
  await page.keyboard.press("Escape");

  // the question before an upgrade of rcman, the upgrade offered
  await openUpgradeDialog(page);
  await shot("upgrade-rcman", false);
  await page.keyboard.press("Escape");
  await page.unroute("**/api/rcman/upgrade");

  // the question that marks a Claude Code version as failed
  await page.goto("/admin/claude");
  await page.getByRole("button", { name: m.versionsMarkAnyFailed }).click();
  await page.getByRole("dialog", { name: m.versionsMarkAnyFailed }).waitFor();
  await shot("mark-version-failed", false);
  await page.keyboard.press("Escape");

  // a sign-out that waits for the last server, as the Claude page shows it
  await page.route("**/api/claude", async (route) => {
    const answer = await (await route.fetch()).json();
    await route.fulfill({ json: { ...answer, signing_out: true } });
  });
  await page.goto("/claude");
  await shot("claude-signing-out");
  await page.unroute("**/api/claude");

  // a GitHub token stored
  await page.route("**/api/github", (route) => route.fulfill({ json: { token: true } }));
  await page.goto("/settings");
  await shot("settings-token-stored");
  await page.unroute("**/api/github");

  const help = paths.find((path) => path.startsWith("/help/"));
  if (phone && help) {
    await page.goto(help);
    await page.getByRole("button", { name: m.docsContents }).click();
    await shot("help-contents", false);
  }
  await context.close();

  // a person not connected to Claude, and with no workspaces
  const [bobs, bobsPage] = await opened(browser, BOB, scheme, device);
  const bobsShot = shooter(bobsPage);
  await bobsPage.goto("/claude");
  await bobsShot("claude-not-connected");
  await bobsPage.goto("/");
  await bobsShot("workspaces-empty");
  await bobs.close();
}

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
// /dev/shm is small in a container, a workspace's among them.
const browser = await chromium.launch({ args: ["--disable-dev-shm-usage"] });
try {
  const [context, page] = await opened(browser, ALICE);
  const paths = await pagePaths(page);
  const id = await workspaces(page);
  await context.close();
  for (const scheme of ["light", "dark"] as const)
    for (const device of DEVICES) await capture(browser, scheme, device, paths, id);
} finally {
  await browser.close();
}
