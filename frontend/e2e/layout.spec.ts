import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { ALICE, useAs } from "./people";

test("a server's screen scrolls inside its card instead of widening the page", async ({ page }) => {
  await useAs(page, ALICE);
  await page.setViewportSize({ width: 900, height: 800 });
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
  await page.goto("/?folder=projects/alice-alpha");
  const card = page.getByRole("article", { name: server.path });
  await card.getByRole("button", { name: m.showScreen }).click();
  const screen = card.getByLabel(m.screenTitle(server.path));
  await expect(screen).toBeVisible();

  // positive control: the screen is wider than the room it has
  expect(await screen.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
});
