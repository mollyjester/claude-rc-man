import { type Page, expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel, summary, tree } from "./nav";
import { ALICE, useAs } from "./people";

// The summary's heading, its first path's button and text, and that path's
// badge, measured in one go: anything that loads meanwhile and moves the
// summary, such as the Claude account above it, moves them all alike.
async function measured(page: Page) {
  return summary(page).evaluate((list) => {
    const box = (el: Element | null) => {
      const r = el!.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const path = list.querySelector("li button.name")!;
    const range = document.createRange();
    range.selectNodeContents(path);
    return {
      heading: box(list.querySelector("h2")),
      path: box(path),
      textLeft: range.getBoundingClientRect().left,
      badge: box(list.querySelector("li .badge")),
    };
  });
}

// 900px is wide enough for the folders beside the panel; 600px is not, and
// the stylesheet puts one below the other.
for (const [width, columns] of [
  [900, 2],
  [600, 1],
] as const) {
  test(`at ${width}px wide, a server's screen scrolls inside its card instead of widening the page`, async ({ page }) => {
    await useAs(page, ALICE);
    await page.setViewportSize({ width, height: 800 });
    // A server that stopped on a screen wider than any window.
    const server = {
      path: "projects/alice-alpha",
      state: "exited",
      environment_url: null,
      session_name: null,
      exit_code: 1,
      screen: ["x".repeat(400)],
    };
    await page.route("**/api/servers", (route) => route.fulfill({ json: { servers: [server] } }));
    await page.goto(`/?folder=${server.path}`);
    const card = page.getByRole("article", { name: server.path });
    await card.getByRole("button", { name: m.showScreen }).click();
    const screen = card.getByLabel(m.screenTitle(server.path));
    await expect(screen).toBeVisible();

    // positive controls: the layout is the one this width gets, and the
    // screen is wider than the room it has
    const folders = (await tree(page).boundingBox())!;
    const opened = (await panel(page, server.path).boundingBox())!;
    expect(opened.x > folders.x ? 2 : 1).toBe(columns);
    expect(await screen.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  });
}

test("Your servers lines its paths up with its heading, each badge on its path's line", async ({ page }) => {
  await useAs(page, ALICE);
  await page.setViewportSize({ width: 1200, height: 800 });
  // the longest badge there is, beside a path
  const server = { path: "projects/alice-alpha", state: "trust_prompt", environment_url: null, session_name: null, exit_code: null };
  await page.route("**/api/servers", (route) => route.fulfill({ json: { servers: [server] } }));
  await page.goto("/");
  const list = summary(page);
  const path = list.getByRole("button", { name: server.path });
  const badge = list.getByText(m.states.trust_prompt, { exact: true });
  await expect(badge).toBeVisible();
  // the tree above it has loaded, so nothing moves the summary while it is measured
  await expect(tree(page).getByRole("button", { name: "projects", exact: true })).toBeVisible();

  await expect(path).toBeVisible();
  // where the path's text starts, not its button's padding
  const { heading, path: pathBox, textLeft, badge: badgeBox } = await measured(page);
  expect(Math.abs(textLeft - heading.x)).toBeLessThanOrEqual(1);
  expect(badgeBox.x).toBeGreaterThan(pathBox.x + pathBox.width - 1); // beside it, not under it
  expect(badgeBox.y).toBeLessThan(pathBox.y + pathBox.height);
});

test("a badge with no room beside its path goes below it, lined up with it", async ({ page }) => {
  await useAs(page, ALICE);
  await page.setViewportSize({ width: 780, height: 800 });
  const server = { path: "projects/alice-alpha", state: "trust_prompt", environment_url: null, session_name: null, exit_code: null };
  await page.route("**/api/servers", (route) => route.fulfill({ json: { servers: [server] } }));
  await page.goto("/");
  await expect(tree(page).getByRole("button", { name: "projects", exact: true })).toBeVisible();
  const list = summary(page);
  const path = list.getByRole("button", { name: server.path });
  const badge = list.getByText(m.states.trust_prompt, { exact: true });
  await expect(badge).toBeVisible();

  await expect(path).toBeVisible();
  const { path: pathBox, textLeft, badge: badgeBox } = await measured(page);
  expect(badgeBox.y).toBeGreaterThanOrEqual(pathBox.y + pathBox.height - 1); // positive control: it went below
  expect(Math.abs(badgeBox.x - textLeft)).toBeLessThanOrEqual(1);
});

test("a file that cannot be selected lines up with those that can", async ({ page }) => {
  await useAs(page, ALICE);
  const path = "projects/alice-repo";
  const status = {
    path,
    branch: { name: "main", commit: "0123456789", upstream: null, ahead: null, behind: null },
    merging: false,
    staged: { files: [], more: 0 },
    not_staged: {
      files: [
        { path: "plain.txt", change: "untracked" },
        { path: "caf�.txt", change: "untracked", selectable: false },
      ],
      more: 0,
    },
    author: null,
  };
  await page.route("**/api/git/status*", (route) => route.fulfill({ json: status }));
  await page.goto(`/?folder=${path}`);
  const list = page.getByRole("region", { name: m.changesTitle }).getByRole("group", { name: m.notStagedTitle });
  const plain = list.getByText("plain.txt", { exact: true });
  const odd = list.getByText("caf�.txt", { exact: true });
  await expect(plain).toBeVisible();
  await expect(odd).toBeVisible();
  const [plainBox, oddBox] = await Promise.all([plain.boundingBox(), odd.boundingBox()]);
  expect(Math.abs(plainBox!.x - oddBox!.x)).toBeLessThanOrEqual(1);
});
