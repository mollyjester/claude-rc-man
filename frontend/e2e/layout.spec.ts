import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel, tree } from "./nav";
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
