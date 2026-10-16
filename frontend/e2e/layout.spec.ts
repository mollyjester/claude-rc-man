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
// stylesheet puts one below the other.
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
    const list = (await workspaces(page).boundingBox())!;
    const opened = (await panel(page, WIDE.name).boundingBox())!;
    expect(opened.x > list.x ? 2 : 1).toBe(columns);
    expect(await screen.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  });
}

test("Your workspaces lines its names up with its heading, each dot on its name's line", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.route("**/api/workspaces", (route) =>
    route.fulfill({ json: { workspaces: [WIDE], claude_approved: null } }),
  );
  await page.goto("/");
  const name = workspaces(page).getByRole("button", { name: WIDE.name });
  await expect(name).toHaveAccessibleDescription(m.kinds.problem);
  // measured in one go, so that nothing loading meanwhile moves one and not the others
  const { heading, button, textLeft, dot } = await workspaces(page).evaluate((list) => {
    const box = (el: Element) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const found = list.querySelector("li button.name")!;
    const range = document.createRange();
    range.selectNodeContents(found);
    return {
      heading: box(list.querySelector("h2")!),
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
