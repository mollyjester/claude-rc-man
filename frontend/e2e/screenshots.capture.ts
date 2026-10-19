import { join } from "node:path";

import { expect, type Page, test } from "@playwright/test";

import { ADMIN_PAGES } from "../src/AdminPages";
import { m } from "../src/messages";
import { PAGES } from "../src/Pages";
import { GITHUB_ADDRESS, openUpgradeDialog, personAction } from "./nav";
import { ALICE, BOB, signInAs } from "./people";

// make screenshots (playwright.screenshots.config.ts): every page of the route table, a help
// page standing for every help page, and the states and views a page only shows when asked —
// each `shot` call below names one — at a phone's and a desktop's width, light and dark, into
// the config's output folder. It asserts nothing about
// how they look; what it waits for is only what has to be there before a screenshot is worth
// taking.

const WORKSPACE = "shot-widget";
// Made after the first, so that the list has more than one.
const OTHERS = ["shot-docs-site", "shot-infra"];

async function post(page: Page, path: string, body: unknown): Promise<number> {
  return page.evaluate(
    async ({ path, body }) => {
      const reply = await fetch(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      return (await reply.json()).workspace?.id ?? 0;
    },
    { path, body },
  );
}

// The world's workspaces for the screenshots, made once and ready: their id, by name.
async function workspaces(page: Page): Promise<number> {
  const listed: { id: number; name: string; state: string }[] = await page.evaluate(
    async () => (await (await fetch("/api/workspaces")).json()).workspaces,
  );
  let id = listed.find((w) => w.name === WORKSPACE)?.id;
  if (id === undefined) {
    id = await post(page, "/api/git/clone", { url: GITHUB_ADDRESS, name: WORKSPACE });
    for (const name of OTHERS) await post(page, "/api/workspaces", { name });
  }
  await expect
    .poll(
      () =>
        page.evaluate(
          async (id) =>
            (await (await fetch("/api/workspaces")).json()).workspaces.find(
              (w: { id: number }) => w.id === id,
            )?.state,
          id,
        ),
      { timeout: 60_000 },
    )
    .toBe("ready");
  return id;
}

// Changes to show in the Changes card: files staged and not, and a commit to push.
async function withChanges(page: Page) {
  await page.route("**/api/git/status*", async (route) => {
    const status = await (await route.fetch()).json();
    await route.fulfill({
      json: {
        ...status,
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
        author: { name: "Alice", email: ALICE },
        push_has_remote: true,
        push_to_github: false,
        push_ahead: 1,
        branch: { ...status.branch, ahead: 1, behind: 0 },
      },
    });
  });
}

// Every page's path: the route table's, a help page's filled in with the first help page.
async function paths(page: Page): Promise<string[]> {
  const slugs: string[] = await page.evaluate(async () =>
    (await (await fetch("/api/docs")).json()).pages.map((p: { slug: string }) => p.slug),
  );
  return [...PAGES, ...ADMIN_PAGES].map((p) => p.path.replace(":slug", slugs[0] ?? ""));
}

// A screenshot's file name: the width's and scheme's name, then what it shows.
const named = (path: string) =>
  path === "/" ? "workspaces" : path.replace(/^\//, "").replace(/[/?=&]+/g, "-");

// Lets a page settle: its fonts, what it fetched, and a moment for what that drew.
async function settled(page: Page) {
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(300);
}

for (const scheme of ["light", "dark"] as const) {
  for (const [label, width, height, phone] of [
    ["phone", 390, 844, true],
    ["desktop", 1280, 800, false],
  ] as const) {
    test.describe(`${label} ${scheme}`, () => {
      test.use({
        colorScheme: scheme,
        viewport: { width, height },
        ...(phone ? { isMobile: true, hasTouch: true } : {}),
      });

      test("every page", async ({ page }, info) => {
        const shot = async (what: string, fullPage = true) => {
          await settled(page);
          await page.screenshot({
            path: join(info.project.outputDir, `${label}-${scheme}-${what}.png`),
            fullPage,
          });
        };
        await signInAs(page, ALICE);
        await page.goto("/");
        const id = await workspaces(page);

        for (const path of await paths(page)) {
          await page.goto(path);
          await expect(page.getByRole("main")).toBeVisible();
          await shot(named(path));
        }
        // a person not connected to Claude, and with no workspaces
        await signInAs(page, BOB);
        await page.goto("/claude");
        await shot("claude-not-connected");
        await page.goto("/");
        await shot("workspaces-empty");
        await signInAs(page, ALICE);

        await page.goto("/");
        await page.getByRole("button", { name: m.newWorkspace, exact: true }).click();
        await shot("new-workspace", false);
        await page.keyboard.press("Escape");

        await withChanges(page);
        await page.goto(`/?workspace=${id}`);
        await expect(page.getByRole("tab", { name: m.tabOverview })).toBeVisible();
        await page.waitForTimeout(1000); // the Changes card's status
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

        await page.goto(`/?workspace=${id}&tab=terminal`);
        await expect(page.getByText(m.terminalConnected)).toBeVisible();
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
        await expect(page.getByRole("form", { name: m.limitsPersonScope(ALICE) })).toBeVisible();
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

        const help = (await paths(page)).find((path) => path.startsWith("/help/"));
        if (phone && help) {
          await page.goto(help);
          await page.getByRole("button", { name: m.docsContents }).click();
          await shot("help-contents", false);
        }
      });
    });
  }
}
