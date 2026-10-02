import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel, summary, tree } from "./nav";
import { ALICE, useAs } from "./people";

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

  // where the path's text starts, not its button's padding
  const textLeft = await path.evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    return range.getBoundingClientRect().left;
  });
  const heading = (await list.getByRole("heading").boundingBox())!;
  expect(Math.abs(textLeft - heading.x)).toBeLessThanOrEqual(1);

  const pathBox = (await path.boundingBox())!;
  const badgeBox = (await badge.boundingBox())!;
  expect(badgeBox.x).toBeGreaterThan(pathBox.x + pathBox.width - 1); // beside it, not under it
  expect(badgeBox.y).toBeLessThan(pathBox.y + pathBox.height);
});
