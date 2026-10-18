import { expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel, workspaces } from "./nav";
import { ALICE, signInAs } from "./people";

// A workspace whose server stopped on a screen wider than any window.
const WIDE = {
  id: 1,
  name: "alice-wide",
  state: "ready",
  error: null,
  server_error: null,
  claude_version: null,
  server: {
    workspace: 1,
    state: "exited",
    environment_url: null,
    session_name: null,
    exit_code: 1,
    screen: ["x".repeat(400)],
  },
};

// 900px is wide enough for the list beside the panel; 600px is not, and the
// stylesheet shows the panel in the list's place.
for (const [width, columns] of [
  [900, 2],
  [600, 1],
] as const) {
  test(`at ${width}px wide, a server's screen scrolls inside its card instead of widening the page`, async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.setViewportSize({ width, height: 800 });
    await page.route("**/api/workspaces", (route) =>
      route.fulfill({ json: { workspaces: [WIDE], claude_approved: null } }),
    );
    await page.goto(`/?workspace=${WIDE.id}`);
    const card = page.getByRole("article", { name: WIDE.name });
    await card.getByRole("button", { name: m.showScreen }).click();
    const screen = card.getByRole("figure", { name: m.screenTitle(WIDE.name) }).locator("pre");
    await expect(screen).toBeVisible();

    // positive controls: the layout is the one this width gets, and the
    // screen is wider than the room it has
    if (columns === 2) {
      const list = (await workspaces(page).boundingBox())!;
      const opened = (await panel(page, WIDE.name).boundingBox())!;
      expect(opened.x).toBeGreaterThan(list.x);
    } else {
      await expect(panel(page, WIDE.name)).toBeVisible();
      await expect(workspaces(page)).toBeHidden();
    }
    expect(await screen.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  });
}

test("the list lines its names up with the page's heading, each dot on its name's line", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.route("**/api/workspaces", (route) =>
    route.fulfill({ json: { workspaces: [WIDE], claude_approved: null } }),
  );
  await page.goto("/");
  const name = workspaces(page).getByRole("button", { name: WIDE.name, exact: true });
  await expect(name).toHaveAccessibleDescription(m.kinds.problem);
  // measured in one go, so that nothing loading meanwhile moves one and not the others
  const { heading, button, textLeft, dot } = await workspaces(page).evaluate((list) => {
    const named = document.getElementById(list.getAttribute("aria-labelledby")!)!;
    const box = (el: Element) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const found = list.querySelector("li button.name")!;
    const range = document.createRange();
    range.selectNodeContents(found);
    return {
      heading: box(named),
      button: box(found),
      // where the name's text starts, not its button's padding
      textLeft: range.getBoundingClientRect().left,
      dot: box(list.querySelector("li .dot")!),
    };
  });
  expect(heading.height).toBeGreaterThan(0); // positive control: both are there
  expect(Math.abs(textLeft - heading.x)).toBeLessThanOrEqual(1);
  expect(dot.x).toBeGreaterThan(button.x + button.width - 1); // beside it, not under it
  expect(dot.y).toBeLessThan(button.y + button.height);
});

test("on a phone a guide's table scrolls inside its box", async ({ page }) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/help/workspaces");
  const table = page.getByRole("table").first();
  await expect(table).toBeVisible();
  expect(await table.evaluate((el) => el.closest(".table-scroll") !== null)).toBe(true);
});

test("a skip link leads the page, each page is titled, and the Workspaces page has its own heading", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: m.navDashboard })).toBeVisible();
  await expect(page).toHaveTitle(m.pageTitle(m.navDashboard));
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: m.skipToContent });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible(); // shown once it has focus
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  await page.goto("/settings");
  await expect(page).toHaveTitle(m.pageTitle(m.navSettings));
});

test.describe("on a phone's header", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 800 } });

  test("the account menu shares the first row with rcman's name, and the links take two rows at most", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    const banner = page.getByRole("banner");
    const brand = (await banner.getByText(m.appName, { exact: true }).boundingBox())!;
    const account = (await banner.getByRole("button", { name: m.account }).boundingBox())!;
    expect(account.y).toBeLessThan(brand.y + brand.height); // on the same row
    expect(account.y + account.height).toBeGreaterThan(brand.y);
    const tops = new Set<number>();
    for (const item of await banner.locator("nav > a, nav > .menu > button").all())
      tops.add(Math.round((await item.boundingBox())!.y));
    expect(tops.size).toBeGreaterThan(0); // positive control: the links are measured
    expect(tops.size).toBeLessThanOrEqual(2);
  });
});
