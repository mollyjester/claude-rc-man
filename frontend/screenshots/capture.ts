// make screenshot (devtools/screenshots.py runs it as `node screenshots/capture.ts <origin>
// <folder> <page> [<state>]`, against the world of devtools/world.py): one page, as the world
// draws it or in one of the states of STATES below, at a phone's and a desktop's width, light
// and dark, into the folder, which is emptied first. A page or state it does not know, or a
// state the page does not have, ends it before it starts the browser, with what it knows. It is
// no test and asserts nothing about how the page looks; what it waits for is only what has to be
// drawn before a screenshot is worth taking. It sets no time limit: Ctrl-C ends a run that hangs.

import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import { type Browser, type BrowserContext, chromium, type Page, type Route } from "playwright";

import { m } from "../src/messages.ts";

const [origin, out, target, stateName = ""] = process.argv.slice(2);
if (!origin || !out || target === undefined)
  throw new Error("usage: node screenshots/capture.ts <origin> <folder> <page> [<state>]");

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

// The pages: the paths the app routes (src/Pages.tsx and src/AdminPages.tsx), a `:name` segment
// standing for any one segment, as `/help/:slug` stands for every help page.
const PAGES = ["Pages.tsx", "AdminPages.tsx"].flatMap((file) =>
  [
    ...readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8").matchAll(
      /\bpath: "(\/[^"]*)"/g,
    ),
  ].map((found) => found[1]),
);
if (PAGES.length === 0) throw new Error("no page's path found in src/Pages.tsx");

const routes = (pattern: string, path: string) => {
  const [want, got] = [pattern.split("/"), path.split("/")];
  return want.length === got.length && want.every((part, i) => part === got[i] || /^:./.test(part));
};

type Opens = (page: Page, path: string, id: number) => Promise<void>;

// A state of a page: the pages it is a state of (a page pattern of PAGES, or every page), whose
// it is, whether only a phone has it (the stylesheet shows its button there alone, so a desktop
// view of it is not taken), whether the screenshot is of the whole page or only the window, as
// for an open dialog, and what brings it about, ending once what it shows is drawn.
type State = {
  pages: string[] | "every";
  who?: string;
  phone?: true;
  window?: true;
  opens: Opens;
};

// The page as the world draws it.
const plain: Opens = async (page, path) => {
  await page.goto(path);
  await page.getByRole("main").waitFor();
};

// The screenshot workspace selected on the Workspaces page, once its panel is drawn.
const panel: Opens = async (page, _path, id) => {
  await page.goto(`/?workspace=${id}`);
  await page.getByRole("heading", { name: WORKSPACE, level: 2 }).waitFor();
};

// The screenshot workspace as the listing gives it, with `extra` laid over it, and what the
// server says of its devcontainer.json (`changed`), its panel drawn.
const listedAs =
  (extra: Record<string, unknown>, changed: boolean | null): Opens =>
  async (page, path, id) => {
    await page.route("**/api/workspaces", (route) => listing(route, id, extra));
    await page.route(`**/api/workspaces/${id}/devcontainer`, (route) =>
      route.fulfill({ json: { changed } }),
    );
    await panel(page, path, id);
  };

// `route`'s answer as the server gives it, `extra` laid over it.
const answered = (route: Route, extra: Record<string, unknown>) =>
  route.fetch({ timeout: 0 }).then(async (got) => ({ ...(await got.json()), ...extra }));

// The page's answer from `api` as the server gives it, with `extra` laid over it.
const answeredWith =
  (api: string, extra: Record<string, unknown>, then: Opens): Opens =>
  async (page, path, id) => {
    await page.route(api, async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      await route.fulfill({ json: await answered(route, extra) });
    });
    await then(page, path, id);
  };

// A dialog, opened on the page by the button named as it is.
const dialog =
  (name: string): Opens =>
  async (page, path, id) => {
    await plain(page, path, id);
    await page.getByRole("button", { name, exact: true }).click();
    await page.getByRole("dialog", { name }).waitFor();
  };

// A pop-up, opened on the page `then` brings about by the button named so, drawn once the button
// says it is open.
const popUp =
  (button: string, then: Opens = plain): Opens =>
  async (page, path, id) => {
    await then(page, path, id);
    await page.getByRole("button", { name: button, exact: true }).click();
    await page.getByRole("button", { name: button, exact: true, expanded: true }).waitFor();
  };

// The screenshot workspace stopped, as the listing gives it: no server read, no ports.
const STOPPED = {
  state: "halted",
  startable: true,
  stoppable: false,
  rebuildable: true,
  server: null,
  server_error: { code: "workspace_stopped", title: "stopped" },
  claude_version: null,
  ports: [],
};

// alice's workspaces on Everyone's workspaces, in order: running and stoppable, stopped, and
// being stopped.
const EVERYONE_MIX: Record<string, unknown>[] = [
  {
    stoppable: true,
    server: { state: "ready", session_name: "shot-widget-quiet-otter", exit_code: null },
    claude_version: "1.0.0",
  },
  { state: "halted", stoppable: false, server: null, claude_version: null },
  {
    stoppable: false,
    stopping: true,
    server: null,
    server_error: { code: "workspace_stopping", title: "stopping" },
  },
];

// The states a page has only when asked, by name.
const STATES: Record<string, State> = {
  // the New workspace dialog
  "new-workspace": { pages: ["/"], window: true, opens: dialog(m.newWorkspace) },
  // the screenshot workspace's panel, its repository clean
  workspace: {
    pages: ["/"],
    opens: async (page, path, id) => {
      await panel(page, path, id);
      await page.getByRole("tab", { name: m.tabOverview }).waitFor();
    },
  },
  // the Changes card with files staged and not, and a commit to push
  changes: {
    pages: ["/"],
    opens: async (page, path, id) => {
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
      await panel(page, path, id);
      await page
        .getByRole("region", { name: m.changesTitle })
        .getByText("README.md", { exact: false })
        .waitFor();
    },
  },
  // the switcher's sheet over the screenshot workspace's panel
  switcher: {
    pages: ["/"],
    phone: true,
    window: true,
    opens: popUp(m.switchWorkspaceFrom(WORKSPACE), panel),
  },
  // the navigation's drawer, open
  drawer: { pages: "every", phone: true, window: true, opens: popUp(m.navMenu) },
  // the screenshot workspace's Terminal tab, connected
  terminal: {
    pages: ["/"],
    window: true,
    opens: async (page, _path, id) => {
      await page.goto(`/?workspace=${id}&tab=terminal`);
      await page.getByText(m.terminalConnected).waitFor();
    },
  },
  // the Container card with the devcontainer.json changed
  "container-changed": { pages: ["/"], opens: listedAs({ rebuildable: true }, true) },
  // the Container card with a rebuild under way
  rebuilding: {
    pages: ["/"],
    opens: listedAs(
      { rebuildable: false, rebuild: { state: "preparing", log: REBUILD_STEPS, error: null } },
      null,
    ),
  },
  // the Container card after a rebuild that failed and was rolled back
  "rolled-back": {
    pages: ["/"],
    opens: listedAs(
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
    ),
  },
  // a failed workspace, offered a rebuild
  "rebuild-failed": {
    pages: ["/"],
    opens: listedAs(
      {
        state: "failed",
        rebuildable: true,
        server: null,
        error:
          "postCreateCommand failed, exit code 1. What it printed last is below.\nnpm ERR! missing script: setup",
        log: REBUILD_STEPS,
      },
      null,
    ),
  },
  // the question before a rebuild, from the Container card
  rebuild: {
    pages: ["/"],
    window: true,
    opens: async (page, path, id) => {
      await listedAs({ rebuildable: true }, true)(page, path, id);
      await page
        .getByRole("region", { name: m.containerTitle })
        .getByRole("button", { name: m.rebuild })
        .click();
      await page.getByRole("dialog", { name: m.rebuildTitle(WORKSPACE) }).waitFor();
    },
  },
  // a ready workspace that may be stopped, Stop workspace in its Container card
  stoppable: { pages: ["/"], opens: listedAs({ stoppable: true, rebuildable: true }, false) },
  // the question before a Stop, from the Container card
  stop: {
    pages: ["/"],
    window: true,
    opens: async (page, path, id) => {
      await listedAs({ stoppable: true, rebuildable: true }, false)(page, path, id);
      await page
        .getByRole("region", { name: m.containerTitle })
        .getByRole("button", { name: m.stopWorkspace })
        .click();
      await page.getByRole("dialog", { name: m.stopWorkspaceTitle(WORKSPACE) }).waitFor();
    },
  },
  // a stopped workspace, Start workspace leading, its ports none, as the server lists them
  stopped: {
    pages: ["/"],
    opens: async (page, path, id) => {
      await listedAs(STOPPED, null)(page, path, id);
      await page.getByRole("button", { name: m.startWorkspace }).waitFor();
    },
  },
  // a stopped workspace's Terminal tab
  "stopped-terminal": {
    pages: ["/"],
    window: true,
    opens: async (page, path, id) => {
      await listedAs(STOPPED, null)(page, path, id);
      await page.getByRole("tab", { name: m.tabTerminal }).click();
      await page.getByText(m.terminalPower.workspace_stopped).waitFor();
    },
  },
  // a workspace being started
  starting: {
    pages: ["/"],
    opens: async (page, path, id) => {
      await listedAs(
        {
          starting: true,
          stoppable: false,
          server: null,
          server_error: { code: "workspace_starting", title: "starting" },
          ports: [],
        },
        null,
      )(page, path, id);
      await page.getByRole("button", { name: m.startWorkspaceBusy }).waitFor();
    },
  },
  // Everyone's workspaces, alice's three as an administrator sees them: one with a running server
  // that may be stopped, one stopped and one being stopped
  "everyone-stop": {
    pages: ["/admin/workspaces"],
    opens: async (page, path, id) => {
      await page.route("**/api/servers/everyone", async (route) => {
        const got = await answered(route, {});
        got.people = got.people.map((person: { workspaces: Record<string, unknown>[] }) => ({
          ...person,
          workspaces: person.workspaces.map((w, at) => ({ ...w, ...EVERYONE_MIX[at] })),
        }));
        await route.fulfill({ json: got });
      });
      await plain(page, path, id);
      await page.getByRole("button", { name: m.stopWorkspaceBusy }).waitFor();
    },
  },
  // the menu of bob's actions on People
  "person-menu": {
    pages: ["/admin/people"],
    phone: true,
    window: true,
    opens: popUp(m.usersMoreActionsFor(BOB)),
  },
  // the Limits dialog of alice
  limits: {
    pages: ["/admin/people"],
    window: true,
    opens: async (page, path, id) => {
      await plain(page, path, id);
      await personAction(page, ALICE, m.usersLimits);
      await page.getByRole("form", { name: m.limitsPersonScope(ALICE) }).waitFor();
    },
  },
  // the Add person dialog
  "add-person": { pages: ["/admin/people"], window: true, opens: dialog(m.usersAdd) },
  // the question before an upgrade of rcman, the upgrade offered: the world's rcman has the
  // upgrade from the web interface off, which offers no button
  upgrade: {
    pages: ["/admin/rcman"],
    window: true,
    opens: answeredWith(
      "**/api/rcman/upgrade",
      { enabled: true, available: true, reason: null },
      dialog(m.upgradeRcman),
    ),
  },
  // the question that marks a Claude Code version as failed
  "mark-failed": {
    pages: ["/admin/claude"],
    window: true,
    opens: dialog(m.versionsMarkAnyFailed),
  },
  // a sign-out that waits for the last server
  "signing-out": {
    pages: ["/claude"],
    opens: answeredWith("**/api/claude", { signing_out: true }, plain),
  },
  // bob's, whose Claude is not connected
  "not-connected": { pages: ["/claude"], who: BOB, opens: plain },
  // bob's, who has no workspaces
  empty: { pages: ["/"], who: BOB, opens: plain },
  // a GitHub token stored
  "token-stored": {
    pages: ["/settings"],
    opens: async (page, path, id) => {
      await page.route("**/api/github", (route) => route.fulfill({ json: { token: true } }));
      await plain(page, path, id);
    },
  },
  // Refresh pressed, once the rounds are in and the Storage card has its figures
  refreshed: {
    pages: ["/admin/monitor"],
    opens: async (page, path, id) => {
      await plain(page, path, id);
      const storage = page.getByRole("region", { name: m.monitorStorage });
      await storage.getByText(m.monitorStorageLine).waitFor();
      await page.getByRole("button", { name: m.monitorRefresh }).click();
      // the total, which is a figure only once every part is
      await storage.locator(".monitor-total .figure").waitFor();
      await page.locator('.page-head button[aria-disabled="false"]').waitFor();
    },
  },
  // a help page's contents, unfolded
  contents: { pages: ["/help/:slug"], phone: true, window: true, opens: popUp(m.docsContents) },
};

// What a page and a state may be, for a target that is neither.
function allowed(): string {
  const width = Math.max(...Object.keys(STATES).map((name) => name.length));
  return [
    "Pages (PAGE):",
    ...PAGES.map((page) => `  ${page}`),
    "States (STATE), each with the pages it is a state of:",
    ...Object.entries(STATES).map(([name, { pages, phone }]) => {
      const of = pages === "every" ? "every page" : pages.join(", ");
      return `  ${name.padEnd(width)}  ${of}${phone ? " (a phone's only)" : ""}`;
    }),
  ].join("\n");
}

// The page pattern the target is, and its state: the target is refused, with what is allowed,
// before anything starts.
const pattern = PAGES.find((page) => routes(page, target));
const state = stateName === "" ? undefined : STATES[stateName];
const refusal =
  pattern === undefined
    ? `no page ${JSON.stringify(target)}`
    : stateName !== "" && state === undefined
      ? `no state ${JSON.stringify(stateName)}`
      : state !== undefined && state.pages !== "every" && !state.pages.includes(pattern)
        ? `${target} has no state ${stateName}`
        : null;
if (refusal !== null) {
  console.error(`screenshot: ${refusal}.\n${allowed()}`);
  process.exit(2);
}

type Device = { label: string; width: number; height: number; phone: boolean };
const DEVICES: Device[] = [
  { label: "phone", width: 390, height: 844, phone: true },
  { label: "desktop", width: 1280, height: 800, phone: false },
];
const SCHEMES = ["light", "dark"] as const;

// A context of its own for each combination of scheme, size and person, signed in as
// oauth2-proxy would sign them in, with no time limit.
async function opened(
  browser: Browser,
  who: string,
  scheme: (typeof SCHEMES)[number] = "light",
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
  context.setDefaultNavigationTimeout(0);
  return [context, await context.newPage()];
}

type Listed = { id: number; name: string; state: string };

async function listed(page: Page): Promise<Listed[]> {
  return page.evaluate(async () => (await (await fetch("/api/workspaces")).json()).workspaces);
}

// alice's workspaces for the screenshots, made once and ready, as the Workspaces page's list
// draws them: the screenshot workspace's id.
async function workspaces(page: Page): Promise<number> {
  const names = [WORKSPACE, ...OTHERS];
  // fetch's paths are the page's: the page is rcman's first.
  await plain(page, "/", 0);
  for (const name of names) {
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
  await page.reload();
  // Each one's row, with its badge while it is not ready, until every one is ready or one has
  // failed.
  const drawn = await page.waitForFunction(
    ({ names, failed }) => {
      const rows = [...document.querySelectorAll("li.list-row")];
      const badges = names.map((name) => {
        const row = rows.find((r) => r.querySelector(".name-text")?.textContent === name);
        return row === undefined ? undefined : (row.querySelector(".badge")?.textContent ?? null);
      });
      const at = badges.indexOf(failed);
      if (at >= 0) return { failed: names[at] };
      return badges.every((badge) => badge === null) && { failed: null };
    },
    { names, failed: m.workspaceStates.failed },
  );
  const { failed } = (await drawn.jsonValue()) as { failed: string | null };
  if (failed !== null) throw new Error(`workspace ${failed} failed`);
  return (await listed(page)).find((w) => w.name === WORKSPACE)?.id ?? 0;
}

// The listing as the server gives it, the screenshot workspace with its repository's address and
// `extra` laid over it.
async function listing(route: Route, id: number, extra: Record<string, unknown> = {}) {
  if (route.request().method() !== "GET") return route.fallback();
  const answer = await answered(route, {});
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

// The requests a page has made that are not answered yet, and a wait for there to be none.
function requests(page: Page) {
  let open = 0;
  let waiting: (() => void)[] = [];
  page.on("request", () => {
    open += 1;
  });
  const settle = () => {
    open -= 1;
    if (open > 0) return;
    for (const done of waiting) done();
    waiting = [];
  };
  page.on("requestfinished", settle);
  page.on("requestfailed", settle);
  return {
    open: () => open,
    none: () => (open === 0 ? Promise.resolve() : new Promise<void>((done) => waiting.push(done))),
  };
}

// Two frames pass, and every animation that ends has ended, a dialog's or a pop-up's opening
// among them (one that repeats without end, a spinner's, is left out).
async function drawn(page: Page) {
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

// Lets a page settle once what it shows is there: its fonts load, and every request it made is
// answered and what the answers drew is drawn, until a page drawn so has made no request since.
async function settled(page: Page, made: ReturnType<typeof requests>) {
  await page.evaluate(() => document.fonts.ready);
  do {
    await made.none();
    await drawn(page);
  } while (made.open() > 0);
}

// A screenshot's file name: the page, the state, then the width's and the scheme's name.
const named = (path: string, scheme: string, device: Device) =>
  [
    path === "/" ? "workspaces" : path.replace(/^\//, "").replace(/\//g, "-"),
    ...(stateName === "" ? [] : [stateName]),
    device.label,
    scheme,
  ].join("-");

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
// /dev/shm is small in a container, a workspace's among them.
const browser = await chromium.launch({ args: ["--disable-dev-shm-usage"], timeout: 0 });
try {
  const [context, page] = await opened(browser, ALICE);
  const id = await workspaces(page);
  await context.close();
  const who = state?.who ?? ALICE;
  for (const device of DEVICES) {
    if (state?.phone && !device.phone) continue;
    for (const scheme of SCHEMES) {
      const [context, page] = await opened(browser, who, scheme, device);
      const made = requests(page);
      if (who === ALICE) await plantRepository(context, id);
      await (state?.opens ?? plain)(page, target, id);
      await settled(page, made);
      await page.screenshot({
        path: join(out, `${named(target, scheme, device)}.png`),
        fullPage: !state?.window,
      });
      await context.close();
    }
  }
} finally {
  await browser.close();
}
