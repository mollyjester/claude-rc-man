import { fileURLToPath } from "node:url";

import { expect, type Locator, type Page, test } from "@playwright/test";

import { ADMIN_PAGES } from "../src/AdminPages";
import { m } from "../src/messages";
import { PAGES } from "../src/Pages";
import { callSites, pageSources } from "../src/test/callSites";
import { openUpgradeDialog, panel, personAction, workspaces } from "./nav";
import { ALICE, BOB, signInAs } from "./people";

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

// A workspace with no server, so with no dot, to stand beside WIDE in the list.
const PLAIN = { ...WIDE, id: 2, name: "alice-plain", server: null };

test("each row of the list holds its dot before its name, on its line, and the names line up with a dot or without", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.route("**/api/workspaces", (route) =>
    route.fulfill({ json: { workspaces: [WIDE, PLAIN], claude_approved: null } }),
  );
  await page.goto("/");
  const name = workspaces(page).getByRole("button", { name: WIDE.name, exact: true });
  await expect(name).toHaveAccessibleDescription(m.kinds.problem);
  // measured in one go, so that nothing loading meanwhile moves one and not the others
  const rows = await workspaces(page).evaluate((list) =>
    [...list.querySelectorAll("li")].map((row) => {
      const button = row.querySelector("button.name")!;
      const text = button.querySelector(".name-text")!;
      const range = document.createRange();
      range.selectNodeContents(text);
      const dot = row.querySelector(".dot")?.getBoundingClientRect() ?? null;
      const b = button.getBoundingClientRect();
      return {
        textLeft: range.getBoundingClientRect().left,
        button: { y: b.y, bottom: b.bottom },
        dot: dot && { x: dot.x, y: dot.y, bottom: dot.bottom },
      };
    }),
  );
  expect(rows).toHaveLength(2); // positive control: both rows are there
  const [wide, plain] = rows;
  expect(wide.dot).not.toBeNull();
  expect(plain.dot).toBeNull();
  expect(wide.dot!.x).toBeLessThan(wide.textLeft); // before the name
  expect(wide.dot!.y).toBeGreaterThanOrEqual(wide.button.y); // on its line
  expect(wide.dot!.bottom).toBeLessThanOrEqual(wide.button.bottom);
  expect(Math.abs(wide.textLeft - plain.textLeft)).toBeLessThanOrEqual(1);
});

test("where no row has a dot, no room is kept for one: each name starts at its button's edge", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.route("**/api/workspaces", (route) =>
    route.fulfill({
      json: {
        workspaces: [PLAIN, { ...PLAIN, id: 3, name: "alice-plain-2" }],
        claude_approved: null,
      },
    }),
  );
  await page.goto("/");
  await expect(
    workspaces(page).getByRole("button", { name: PLAIN.name, exact: true }),
  ).toBeVisible();
  const offsets = await workspaces(page).evaluate((list) =>
    [...list.querySelectorAll("button.name")].map((button) => {
      const range = document.createRange();
      range.selectNodeContents(button.querySelector(".name-text")!);
      return range.getBoundingClientRect().left - button.getBoundingClientRect().left;
    }),
  );
  expect(offsets).toHaveLength(2); // positive control: both rows are there
  for (const offset of offsets) expect(offset).toBeLessThanOrEqual(1);
});

test("the sidebar's column is painted down to the foot of a page longer than the window", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1280, height: 600 });
  await page.goto("/help/workspaces");
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  const { column, shell, top, background } = await page.evaluate(() => {
    const el = document.querySelector(".shell")!;
    const painted = getComputedStyle(el, "::before");
    return {
      column: parseFloat(painted.height),
      shell: el.getBoundingClientRect().height,
      top: document.querySelector(".topbar")!.getBoundingClientRect().height,
      background: painted.backgroundColor,
    };
  });
  expect(shell).toBeGreaterThan(1200); // positive control: the page is longer than the window
  expect(column).toBeGreaterThanOrEqual(shell - top - 1);
  expect(background).not.toBe("rgba(0, 0, 0, 0)");
});

test("a heading's semibold is drawn in a face of its own, not the regular one", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  // the same words at 400 and at 600 in the page's own face: a real semibold face is wider
  const widths = await page.evaluate(() => {
    const family = getComputedStyle(document.querySelector("h1")!).fontFamily;
    return [400, 600].map((weight) => {
      const span = document.createElement("span");
      span.style.cssText = `font-family: ${family}; font-weight: ${weight}; font-size: 24px; font-synthesis: none; position: absolute`;
      span.textContent = "Workspaces and their servers";
      document.body.append(span);
      const width = span.getBoundingClientRect().width;
      span.remove();
      return width;
    });
  });
  expect(widths[0]).toBeGreaterThan(0);
  expect(widths[1]).toBeGreaterThan(widths[0] + 1);
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

// --- the app shell: the top bar, the sidebar and its drawer, and dialogs ------------------

// The page's own source folder, whose dialogs the check of every dialog is held to.
const SRC = fileURLToPath(new URL("../src", import.meta.url));

// The one navigation of the pages, and the button that opens it as a drawer on a phone.
const sidebar = (page: Page) => page.getByRole("navigation", { name: m.navMain });
const menuButton = (page: Page) =>
  page.getByRole("banner").getByRole("button", { name: m.navMenu });
// The pages the sidebar offers everyone, from the route table.
const SIDEBAR = PAGES.filter((p) => p.icon !== undefined);

const box = async (locator: Locator) => (await locator.boundingBox())!;

// Two phones' widths and a desktop's: on a phone the sidebar is a drawer, and dialogs sheets.
for (const [width, phone] of [
  [360, true],
  [390, true],
  [1280, false],
] as const) {
  test.describe(`${width}px wide`, () => {
    test.use(
      phone
        ? { isMobile: true, hasTouch: true, viewport: { width, height: 800 } }
        : { viewport: { width, height: 800 } },
    );

    test("the top bar is one row at most 56px high, holding what this width shows", async ({
      page,
    }) => {
      await signInAs(page, ALICE);
      await page.goto("/settings");
      const banner = page.getByRole("banner");
      const bar = await box(banner);
      expect(bar.height).toBeLessThanOrEqual(56);
      expect(bar.width).toBe(width); // across the window
      const shown = phone
        ? [menuButton(page), banner.locator(".crumb")]
        : [banner.getByRole("link", { name: m.appName }), banner.locator(".crumb")];
      shown.push(
        banner.getByRole("button", { name: m.account }),
        ...(phone ? [] : [banner.getByRole("link", { name: m.navDocs })]),
      );
      for (const part of shown) {
        await expect(part).toBeVisible();
        const at = await box(part);
        expect(at.y).toBeGreaterThanOrEqual(bar.y - 0.5); // inside the one row
        expect(at.y + at.height).toBeLessThanOrEqual(bar.y + bar.height + 0.5);
      }
      await expect(banner.locator(".crumb")).toHaveText(m.navSettings);
      // on a phone: the menu button, where you are and the avatar, and nothing else
      if (phone) {
        await expect(banner.getByRole("link", { name: m.appName })).toBeHidden();
        await expect(banner.getByRole("link", { name: m.navDocs })).toBeHidden();
      } else await expect(menuButton(page)).toBeHidden();
    });

    test("one navigation offers every page, with the administrators' group for administrators only", async ({
      page,
    }) => {
      for (const [who, admin] of [
        [ALICE, true],
        [BOB, false],
      ] as const) {
        await signInAs(page, who);
        await page.goto("/");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.locator("nav.sidebar")).toHaveCount(1); // one element at every width
        const nav = sidebar(page);
        if (phone) {
          await expect(nav).toBeHidden();
          await menuButton(page).click();
        }
        await expect(nav).toBeVisible();
        for (const { name } of SIDEBAR)
          await expect(nav.getByRole("link", { name, exact: true })).toBeVisible();
        const group = nav.getByRole("list", { name: m.navAdmin });
        if (admin) {
          await expect(group.getByRole("link")).toHaveCount(ADMIN_PAGES.length);
          for (const { label } of ADMIN_PAGES)
            await expect(group.getByRole("link", { name: label })).toBeVisible();
        } else {
          await expect(group).toHaveCount(0);
          for (const { label } of ADMIN_PAGES)
            await expect(page.getByRole("link", { name: label })).toHaveCount(0);
        }
        const at = await box(nav);
        const main = await box(page.getByRole("main"));
        if (phone) {
          // a drawer over the page, from its left edge, under the top bar
          const bar = await box(page.getByRole("banner"));
          expect(at.x).toBe(0);
          expect(at.y).toBeGreaterThanOrEqual(bar.y + bar.height - 0.5);
          expect(at.width).toBeLessThan(width);
          expect(main.x).toBe(0); // the page stays where it is, under the drawer
        } else {
          // a column beside the page
          expect(at.x).toBe(0);
          expect(at.width).toBe(240);
          expect(main.x).toBeGreaterThanOrEqual(at.x + at.width - 0.5);
        }
      }
    });

    test("every dialog is a sheet from the foot of a phone's screen, and in the middle of a desktop's", async ({
      page,
    }) => {
      test.slow(); // each dialog of the page
      await signInAs(page, ALICE);
      const sources = callSites(pageSources(SRC));
      expect(sources.faults).toEqual([]);
      expect(Object.keys(OPENERS).sort()).toEqual(sources.dialogs); // every dialog is opened
      for (const [owner, open] of Object.entries(OPENERS)) {
        await open(page);
        const dialog = page.locator(`dialog[open][data-component="${owner}"]`);
        await expect(dialog).toBeVisible();
        const at = await box(dialog);
        const view = page.viewportSize()!;
        if (phone) {
          expect(at.x, owner).toBe(0);
          expect(at.width, owner).toBe(view.width);
          expect(Math.abs(at.y + at.height - view.height), owner).toBeLessThanOrEqual(1);
          expect(at.y, owner).toBeGreaterThan(0); // the page above it shows
        } else {
          expect(Math.abs(at.x - (view.width - at.x - at.width)), owner).toBeLessThanOrEqual(1);
          expect(Math.abs(at.y - (view.height - at.y - at.height)), owner).toBeLessThanOrEqual(1);
        }
        await page.unrouteAll({ behavior: "ignoreErrors" });
      }
    });
  });
}

// Workspaces to show the trash buttons' dialogs: a ready one, and one being made.
const READY = { ...WIDE, id: 7001, name: "alice-sheet-ready", source: null, source_page: null };
const MAKING = { ...READY, id: 7002, name: "alice-sheet-making", state: "creating", server: null };

// How each dialog of the page is opened, by the component that draws it.
const OPENERS: Record<string, (page: Page) => Promise<void>> = {
  NewWorkspace: async (page) => {
    await page.goto("/");
    await page.getByRole("button", { name: m.newWorkspace, exact: true }).click();
  },
  DeleteWorkspaceDialog: async (page) => {
    await page.route("**/api/workspaces", (route) =>
      route.fulfill({ json: { workspaces: [READY, MAKING], claude_approved: null } }),
    );
    await page.goto("/");
    await page
      .getByRole("button", { name: m.deleteWorkspaceNamed(READY.name), exact: true })
      .click();
  },
  CannotDeleteDialog: async (page) => {
    await page.route("**/api/workspaces", (route) =>
      route.fulfill({ json: { workspaces: [READY, MAKING], claude_approved: null } }),
    );
    await page.goto("/");
    await page
      .getByRole("button", { name: m.deleteWorkspaceNamed(MAKING.name), exact: true })
      .click();
  },
  RemoveTokenDialog: async (page) => {
    await page.route("**/api/github", (route) => route.fulfill({ json: { token: true } }));
    await page.goto("/settings");
    await page.getByRole("button", { name: m.githubTokenRemove }).click();
  },
  LimitsDialog: async (page) => {
    await page.goto("/admin/people");
    await personAction(page, BOB, m.usersLimits);
  },
  AddPersonDialog: async (page) => {
    await page.goto("/admin/people");
    await page.getByRole("button", { name: m.usersAdd }).click();
  },
  RemoveDialog: async (page) => {
    await page.goto("/admin/people");
    await personAction(page, BOB, m.usersRemove);
  },
  UpgradeDialog: async (page) => void (await openUpgradeDialog(page)),
};

// --- the Workspaces page: the list beside the panel, and on a phone the bar over the panel ---

// Two ready workspaces to switch between.
const FIRST = { ...READY, id: 7101, name: "alice-bar-first" };
const SECOND = { ...READY, id: 7102, name: "alice-bar-second", server: null };
const plantTwo = (page: Page) =>
  page.route("**/api/workspaces", (route) =>
    route.fulfill({ json: { workspaces: [FIRST, SECOND], claude_approved: null } }),
  );
const selected = (page: Page) => new URL(page.url()).searchParams.get("workspace");

test.describe("a workspace's panel on a phone", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 500 } });

  test("is under one sticky bar of a back button and the switcher named by the workspace, in one row, where no row of All workspaces and Switch workspace stands", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await plantTwo(page);
    await page.goto(`/?workspace=${FIRST.id}`);
    const back = page.getByRole("link", { name: m.backToWorkspaces });
    const switcher = page.getByRole("button", { name: m.switchWorkspaceFrom(FIRST.name) });
    await expect(switcher).toBeVisible();
    await expect(switcher).toHaveText(FIRST.name);
    await expect(back).toBeVisible();
    const bar = page.locator(".panel-bar");
    const at = await box(bar);
    expect(at.height).toBeLessThanOrEqual(56);
    expect(at.width).toBe(390); // across the window
    for (const part of [back, switcher]) {
      const b = await box(part);
      expect(b.y).toBeGreaterThanOrEqual(at.y - 0.5); // inside the one row
      expect(b.y + b.height).toBeLessThanOrEqual(at.y + at.height + 0.5);
    }
    // the row it takes the place of is gone, its words on no control
    await expect(page.getByRole("link", { name: "All workspaces" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Switch workspace", exact: true })).toHaveCount(
      0,
    );
    // nothing stands between it and the top bar: the page's heading row is out of sight
    await expect(page.getByRole("button", { name: m.newWorkspace, exact: true })).toBeHidden();
    const top = await box(page.getByRole("banner"));
    expect(Math.abs(at.y - (top.y + top.height))).toBeLessThanOrEqual(1);
    // what is scrolled to stops below it as well as below the top bar
    const padding = await page.evaluate(() =>
      parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop),
    );
    expect(padding).toBeGreaterThanOrEqual(top.height + at.height - 1);
    // and it stays there while the panel scrolls under it
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeGreaterThan(600);
    await page.evaluate(() => window.scrollBy(0, 300));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    const scrolled = await box(bar);
    expect(Math.abs(scrolled.y - (top.y + top.height))).toBeLessThanOrEqual(1);
  });

  test("the switcher opens the workspaces as a sheet from the screen's foot, which switches, closes on Escape and on a press above it", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await plantTwo(page);
    await page.goto(`/?workspace=${FIRST.id}`);
    const switcher = page.getByRole("button", { name: m.switchWorkspaceFrom(FIRST.name) });
    await switcher.click();
    const sheet = page.locator('.sheet[data-component="PanelBar"]');
    await expect(sheet).toBeVisible();
    const at = await box(sheet);
    const view = page.viewportSize()!;
    expect(at.x).toBe(0);
    expect(at.width).toBe(view.width);
    expect(Math.abs(at.y + at.height - view.height)).toBeLessThanOrEqual(1);
    expect(at.y).toBeGreaterThan(0); // the page above it shows
    await expect(sheet.getByText(m.switchWorkspace)).toBeVisible(); // its title
    await expect(sheet.getByRole("button", { name: FIRST.name, exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(switcher).toBeFocused();

    await switcher.click();
    await sheet.getByRole("button", { name: SECOND.name, exact: true }).click();
    await expect(sheet).toBeHidden();
    const now = page.getByRole("button", { name: m.switchWorkspaceFrom(SECOND.name) });
    await expect(now).toBeVisible();
    expect(selected(page)).toBe(String(SECOND.id));

    // a press on the page dimmed above it closes it, and chooses nothing
    await now.click();
    await expect(sheet).toBeVisible();
    const back = await box(page.getByRole("link", { name: m.backToWorkspaces }));
    await page.touchscreen.tap(back.x + back.width / 2, back.y + back.height / 2);
    await expect(sheet).toBeHidden();
    await expect(now).toBeFocused(); // back on the switcher
    await page.waitForTimeout(300); // a press that went through would have left by now
    expect(selected(page)).toBe(String(SECOND.id));
  });

  test("the list's heading row is a count of the workspaces at one edge and New at the other", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await plantTwo(page);
    await page.goto("/");
    const head = page.locator(".page-head");
    const count = head.getByText(m.workspaceCount(2));
    const make = head.getByRole("button", { name: m.newWorkspace, exact: true });
    await expect(count).toBeVisible();
    await expect(make).toBeVisible();
    // compact on a phone: its short words shown, its whole name kept for assistive technology
    await expect(make.locator(".label-short")).toHaveText(m.newWorkspaceShort);
    await expect(make.locator(".label-short")).toBeVisible();
    await expect(make.locator(".label-long")).toBeHidden();
    const row = await box(head);
    const left = await box(count);
    const right = await box(make);
    expect(Math.abs(left.x - row.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(right.x + right.width - (row.x + row.width))).toBeLessThanOrEqual(1);
    expect(left.y).toBeLessThan(right.y + right.height); // one row
    expect(right.y).toBeLessThan(left.y + left.height);
  });
});

test.describe("the Workspaces page on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("shows the list as a column of its own beside the panel, the selected row marked by the accent's bar on a quiet fill", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await plantTwo(page);
    await page.goto(`/?workspace=${FIRST.id}`);
    const list = await box(workspaces(page));
    const opened = await box(panel(page, FIRST.name));
    expect(list.width).toBeGreaterThanOrEqual(260);
    expect(list.width).toBeLessThanOrEqual(300);
    expect(opened.x).toBeGreaterThan(list.x + list.width); // beside it
    expect(Math.abs(opened.y - list.y)).toBeLessThanOrEqual(8); // their tops level
    expect(opened.width).toBeGreaterThan(list.width);
    await expect(page.locator(".panel-bar")).toBeHidden(); // the list is the way to another
    const look = (name: string) =>
      workspaces(page)
        .getByRole("button", { name, exact: true })
        .evaluate((button) => {
          const row = button.closest("li")!;
          const bar = getComputedStyle(row, "::before");
          return {
            fill: getComputedStyle(row).backgroundColor,
            bar: bar.content === "none" ? null : { width: bar.width, colour: bar.backgroundColor },
            name: getComputedStyle(button).backgroundColor,
          };
        });
    const chosen = await look(FIRST.name);
    expect(chosen.bar).toEqual({ width: "3px", colour: expect.any(String) });
    expect(chosen.fill).not.toBe("rgba(0, 0, 0, 0)"); // a fill of its own
    expect(chosen.fill).not.toBe(chosen.bar!.colour); // a quiet one, not the accent itself
    expect(chosen.name).toBe("rgba(0, 0, 0, 0)"); // and no solid pill on the name
    const other = await look(SECOND.name);
    expect(other.bar).toBeNull();
    expect(other.fill).toBe("rgba(0, 0, 0, 0)");
  });
});

test.describe("on a phone", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 844 } });

  test("the drawer closes on Escape, back to its button, on choosing a page and on a press outside it", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    const nav = sidebar(page);
    // the drawer that opens is the same element the desktop's sidebar is
    await page.locator("nav.sidebar").evaluate((el) => {
      el.setAttribute("data-probe", "the one");
    });
    await menuButton(page).click();
    await expect(nav).toBeVisible();
    await expect(page.locator('nav.sidebar[data-probe="the one"]')).toBeVisible();
    await nav.getByRole("link", { name: m.navClaude, exact: true }).focus();
    await page.keyboard.press("Escape");
    await expect(nav).toBeHidden();
    await expect(menuButton(page)).toBeFocused();

    await menuButton(page).click();
    await nav.getByRole("link", { name: m.navSettings, exact: true }).click();
    await expect(page).toHaveURL("/settings");
    await expect(nav).toBeHidden();

    await menuButton(page).click();
    await expect(nav).toBeVisible();
    // a press on the page beside the drawer, on its dimmed part
    const at = await box(nav);
    await page.mouse.click(at.x + at.width + 20, at.y + 200);
    await expect(nav).toBeHidden();
    await expect(page).toHaveURL("/settings"); // the press chose nothing
  });

  test("a press beside the open drawer closes it and does nothing else, not even to what is under it", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.route("**/api/workspaces", (route) =>
      route.fulfill({ json: { workspaces: [READY], claude_approved: null } }),
    );
    await page.goto("/");
    const trash = page.getByRole("button", {
      name: m.deleteWorkspaceNamed(READY.name),
      exact: true,
    });
    await expect(trash).toBeVisible();
    const at = await box(trash);
    const nav = sidebar(page);
    await menuButton(page).click();
    await expect(nav).toBeVisible();
    // positive control: the trash button is beside the drawer, where the press lands
    expect(at.x).toBeGreaterThan((await box(nav)).x + (await box(nav)).width);
    await page.touchscreen.tap(at.x + at.width / 2, at.y + at.height / 2);
    await expect(nav).toBeHidden();
    await page.waitForTimeout(300); // a click that went through would open its dialog by now
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(page).toHaveURL("/");
    // and the same with a mouse's press
    await menuButton(page).click();
    await expect(nav).toBeVisible();
    await page.mouse.click(at.x + at.width / 2, at.y + at.height / 2);
    await expect(nav).toBeHidden();
    await page.waitForTimeout(300);
    await expect(page.locator("dialog[open]")).toHaveCount(0);
  });

  test("the drawer takes focus to the page where you are, and choosing a page takes it to the page", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/settings");
    const nav = sidebar(page);
    await menuButton(page).focus();
    await page.keyboard.press("Enter");
    await expect(nav.getByRole("link", { name: m.navSettings, exact: true })).toBeFocused();
    await page.keyboard.press("Shift+Tab"); // to the page before it, Claude
    await expect(nav.getByRole("link", { name: m.navClaude, exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL("/claude");
    await expect(nav).toBeHidden();
    await expect(page.getByRole("main")).toBeFocused();
  });

  test("a dialog as a sheet keeps focus in it, closes on Escape, and keeps its footer above the keyboard", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    const opener = page.getByRole("button", { name: m.newWorkspace, exact: true });
    await opener.click();
    const dialog = page.getByRole("dialog", { name: m.newWorkspace });
    await expect(dialog).toBeVisible();
    expect(await dialog.evaluate((el) => el.matches(":modal"))).toBe(true);
    const name = dialog.getByLabel(m.newWorkspaceName);
    await expect(name).toBeFocused();
    // Tab goes round what the dialog holds, and past its end to the browser's own controls,
    // which leave the page's focus on its body, never on what is behind the dialog
    let inside = 0;
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press("Tab");
      const where = await dialog.evaluate((el) =>
        el.contains(document.activeElement)
          ? "inside"
          : document.activeElement === document.body
            ? "browser"
            : (document.activeElement?.outerHTML.slice(0, 60) ?? "none"),
      );
      expect(["inside", "browser"]).toContain(where);
      if (where === "inside") inside++;
    }
    expect(inside).toBeGreaterThan(2); // positive control: Tab went through the dialog
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(opener).toBeFocused();

    // the on-screen keyboard: with interactive-widget=resizes-content (index.html), Chrome on
    // Android takes its room from the window, as this does; one that leaves less room than the
    // dialog needs, so that what it holds scrolls
    await opener.click();
    await expect(name).toBeFocused();
    await page.setViewportSize({ width: 390, height: 320 });
    await expect
      .poll(async () => {
        const at = await box(dialog);
        return at.y + at.height;
      })
      .toBeLessThanOrEqual(320.5);
    const footer = dialog.locator(".footer");
    await expect(footer.getByRole("button", { name: m.create })).toBeVisible();
    const sheet = await box(dialog);
    const foot = await box(footer);
    expect(foot.y).toBeGreaterThanOrEqual(sheet.y);
    expect(foot.y + foot.height).toBeLessThanOrEqual(320.5); // above the keyboard
    // and stays at the sheet's foot while what is above it scrolls
    await dialog.evaluate((el) => el.scrollTo(0, 0));
    const atTop = await box(footer);
    expect(atTop.y + atTop.height).toBeLessThanOrEqual(320.5);
    expect(await dialog.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true); // it does scroll
  });
});

// --- the other pages: Settings' cards, Help's contents, the tables' cards ------------------

// What a part of the Settings page is, by its title: its card, its title and line, its form.
async function settingsParts(page: Page) {
  const parts = [m.settingsPrefixTitle, m.settingsAuthorTitle, m.settingsGithubTitle];
  const found = [];
  for (const title of parts) {
    const card = page.getByRole("region", { name: title });
    await expect(card.getByRole("button", { name: m.save })).toBeVisible();
    found.push({
      title,
      card: await box(card),
      intro: await box(card.locator(".setting-intro")),
      form: await box(card.locator(".setting-body")),
    });
  }
  return found;
}

test.describe("the Settings page on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("puts each part's title and line beside its form, inside its card", async ({ page }) => {
    await signInAs(page, BOB);
    await page.goto("/settings");
    for (const part of await settingsParts(page)) {
      expect(part.intro.x + part.intro.width, part.title).toBeLessThanOrEqual(part.form.x);
      expect(Math.abs(part.intro.y - part.form.y), part.title).toBeLessThanOrEqual(2);
      expect(part.form.x + part.form.width, part.title).toBeLessThanOrEqual(
        part.card.x + part.card.width,
      );
    }
  });
});

test.describe("the Settings page on a phone", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 800 } });

  test("stacks each part's title and line over its form, inside the window", async ({ page }) => {
    await signInAs(page, BOB);
    await page.goto("/settings");
    for (const part of await settingsParts(page)) {
      expect(part.intro.y + part.intro.height, part.title).toBeLessThanOrEqual(part.form.y);
      expect(Math.abs(part.intro.x - part.form.x), part.title).toBeLessThanOrEqual(1);
      expect(part.card.x + part.card.width, part.title).toBeLessThanOrEqual(390);
    }
  });
});

// The Help page's contents, and the button that discloses them where they are folded.
const helpContents = (page: Page) => page.getByRole("navigation", { name: m.docsTitle });
const helpToggle = (page: Page) => page.getByRole("button", { name: m.docsContents });

test.describe("Help on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 600 } });

  test("keeps its contents in a column beside the article, in view as the article scrolls", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/help/workspaces");
    const article = page.locator("article.docs-article");
    await expect(article.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(helpToggle(page)).toBeHidden();
    const nav = await box(helpContents(page));
    const at = await box(article);
    expect(nav.x + nav.width).toBeLessThanOrEqual(at.x);
    // a line no longer than reads well
    const measure = await article
      .locator("p")
      .first()
      .evaluate((p) => {
        const size = parseFloat(getComputedStyle(p).fontSize);
        return p.getBoundingClientRect().width / size;
      });
    expect(measure).toBeLessThanOrEqual(45); // about 72 characters of 14px text
    await page.mouse.wheel(0, 1500);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(500); // positive control
    const after = await box(helpContents(page));
    expect(after.y).toBeGreaterThanOrEqual(0);
    expect(after.y).toBeLessThan(200); // stuck under the top bar
  });
});

test.describe("Help on a phone", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 800 } });

  test("folds its contents under a button above the article, which opens them in place and closes them on a page chosen", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/help/workspaces");
    const article = page.locator("article.docs-article");
    await expect(article.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(helpToggle(page)).toBeVisible();
    await expect(helpContents(page)).toBeHidden();
    expect((await box(helpToggle(page))).y).toBeLessThan((await box(article)).y);
    await helpToggle(page).click();
    await expect(helpContents(page)).toBeVisible();
    const nav = await box(helpContents(page));
    expect(nav.y + nav.height).toBeLessThanOrEqual((await box(article)).y);
    expect(nav.width).toBeGreaterThan(300); // across the window, not a column
    await helpContents(page).getByRole("link").nth(1).click();
    await expect(helpContents(page)).toBeHidden();
    // focus goes to the page chosen, not back to the body
    await expect
      .poll(() => page.evaluate(() => document.activeElement?.tagName ?? "BODY"))
      .not.toBe("BODY");
    expect(await page.evaluate(() => document.activeElement!.closest("main") !== null)).toBe(true);
    await expect(helpToggle(page)).toHaveAttribute("aria-expanded", "false");
  });
});

// --- People and Audit as lists on a phone, the Versions tables scrolling sideways ----------

// What a finger needs of a target: WCAG 2.2's 2.5.5, the --touch token in src/styles.css.
const FINGER = 44;

// A person and an event longer than any the world has, and a failure, to stretch the rows.
const LONG_PERSON = {
  id: 9001,
  email: "a.rather.long.address.for.the.list@subdomain.example.com",
  admin: true,
  state: "active",
  workspaces: 12,
  limits: { memory: "16G", cpu: "2.5", pids: "1000" },
};
const FAILED_EVENT = {
  id: 9_000_001,
  at: "2026-10-01T10:05:09+00:00",
  email: LONG_PERSON.email,
  action: "git.push",
  target: `projects/${"x".repeat(80)}`,
  result: "workspace_not_found",
};
async function plantLongRows(page: Page) {
  await page.route("**/api/people", async (route) => {
    const answer = await (await route.fetch()).json();
    await route.fulfill({ json: { people: [...answer.people, LONG_PERSON] } });
  });
  await page.route("**/api/audit*", async (route) => {
    const answer = await (await route.fetch()).json();
    await route.fulfill({ json: { events: [FAILED_EVENT, ...answer.events] } });
  });
}

// What is wrong with the body rows of the table in a box as the page draws them now: the box
// scrolling sideways; a shown cell outside its row, over another shown cell, or with what it
// holds wider than it; a row's lead (td.lead) squeezed to under a third of the row; and, where
// a row has actions, none shown, or one outside the window or the box, or smaller than a finger.
async function rowFaults(scroll: Locator, actions: boolean) {
  return scroll.evaluate(
    (box, { actions, least }) => {
      const faults: string[] = [];
      if (box.scrollWidth > box.clientWidth)
        faults.push(`the box scrolls sideways: ${box.scrollWidth} > ${box.clientWidth}`);
      const shown = (el: Element) => {
        const at = el.getBoundingClientRect();
        return at.width > 0 && at.height > 0 && getComputedStyle(el).visibility !== "hidden";
      };
      const edge = box.getBoundingClientRect();
      const rows = [...box.querySelectorAll("tbody tr")];
      rows.forEach((row, i) => {
        const r = row.getBoundingClientRect();
        const cells = [...row.children].filter(shown);
        const boxes = cells.map((c) => c.getBoundingClientRect());
        boxes.forEach((c, j) => {
          const cell = `row ${i}, ${cells[j].textContent?.slice(0, 24)}`;
          if (c.left < r.left - 0.5 || c.right > r.right + 0.5)
            faults.push(`${cell}: outside its row`);
          if (c.top < r.top - 0.5 || c.bottom > r.bottom + 0.5)
            faults.push(`${cell}: outside its row`);
          if (cells[j].scrollWidth > cells[j].clientWidth + 1)
            faults.push(`${cell}: what it holds is wider than it`);
          for (let k = j + 1; k < boxes.length; k++) {
            const o = boxes[k];
            const wide = Math.min(c.right, o.right) - Math.max(c.left, o.left);
            const high = Math.min(c.bottom, o.bottom) - Math.max(c.top, o.top);
            if (wide > 0.5 && high > 0.5)
              faults.push(`${cell}: overlaps ${cells[k].textContent?.slice(0, 24)}`);
          }
        });
        const lead = row.querySelector("td.lead");
        if (lead && lead.getBoundingClientRect().width < r.width / 3)
          faults.push(`row ${i}: its lead squeezed to ${lead.getBoundingClientRect().width}px`);
        if (!actions) return;
        const buttons = [...row.querySelectorAll(".row-actions button")].filter(shown);
        if (buttons.length === 0) faults.push(`row ${i}: no action shown`);
        for (const button of buttons) {
          const b = button.getBoundingClientRect();
          const name = `row ${i}, ${button.getAttribute("aria-label") ?? button.textContent}`;
          if (b.left < 0 || b.right > innerWidth || b.left < edge.left || b.right > edge.right)
            faults.push(`${name}: out of view`);
          if (b.width < least || b.height < least)
            faults.push(`${name}: ${b.width}x${b.height}, under a finger's size`);
        }
      });
      return { rows: rows.length, faults };
    },
    { actions, least: FINGER },
  );
}

for (const width of [360, 390]) {
  test.describe(`People and Audit on a phone ${width}px wide`, () => {
    test.use({ isMobile: true, hasTouch: true, viewport: { width, height: 800 } });

    for (const [path, actions] of [
      ["/admin/people", true],
      ["/admin/audit", false],
    ] as const)
      test(`${path} draws each row as a list's row, inside its card, its cells apart${actions ? ", its action in view and a finger's size" : ""}`, async ({
        page,
      }) => {
        await signInAs(page, ALICE);
        await page.goto("/");
        // something audited, so that the audit trail has a row of the world's own
        await page.evaluate(() =>
          fetch("/api/me/settings", {
            method: "PATCH",
            headers: { "content-type": "application/json" },
            body: "{}",
          }),
        );
        await plantLongRows(page);
        await page.goto(path);
        const scroll = page.locator(".card .table-scroll");
        await expect(scroll.locator("tbody tr").nth(1)).toBeVisible();
        await page.waitForLoadState("networkidle");
        const table = scroll.locator("table");
        expect(await table.evaluate((t) => getComputedStyle(t).display)).not.toBe("table");
        const found = await rowFaults(scroll, actions);
        expect(found.rows).toBeGreaterThan(1); // positive control: the rows were measured
        expect(found.faults).toEqual([]);
        if (actions) {
          // a limit not set is left out of its row's line, and there is one to leave out
          const unset = scroll.locator("td.unset");
          expect(await unset.count()).toBeGreaterThan(0);
          const shown = await unset.evaluateAll(
            (tds) => tds.filter((td) => td.getBoundingClientRect().width > 0).length,
          );
          expect(shown).toBe(0);
        }
        const widths = await page.evaluate(() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth,
        }));
        expect(widths.scroll).toBeLessThanOrEqual(widths.client);

        // a planted twin: the lead let shrink to nothing beside the break after it, which bites
        // where nothing else shares the lead's line, as on People
        if (actions) {
          const squeeze = await page.addStyleTag({
            content: ".card.flush table.stacked td.lead { min-width: 0 }",
          });
          expect((await rowFaults(scroll, actions)).faults.join("\n")).toContain("squeezed");
          await squeeze.evaluate((tag) => (tag as Element).remove());
          expect((await rowFaults(scroll, actions)).faults).toEqual([]); // the plant is gone
        }

        // the planted twin: a row's actions kept to the box's right edge over a table that is not
        // reflowed, which hides what is under them; the check sees it
        await page.addStyleTag({
          content: ".row-actions { position: sticky; right: 0; background: var(--surface); }",
        });
        await table.evaluate((t) => t.classList.remove("stacked"));
        expect(await table.evaluate((t) => getComputedStyle(t).display)).toBe("table"); // took
        const planted = await rowFaults(scroll, actions);
        expect(planted.faults.join("\n")).toContain("the box scrolls sideways");
        if (actions) expect(planted.faults.join("\n")).toContain("overlaps");
      });

    test("a row's More actions opens its menu inside the window, over the rows, and Escape closes it back to the button", async ({
      page,
    }) => {
      await signInAs(page, ALICE);
      await page.goto("/admin/people");
      // the last row's, whose menu has the least room under it
      const rows = page.locator(".card tbody tr");
      await expect(rows.nth(1)).toBeVisible();
      const last = rows.last();
      const more = last.locator(".row-actions").getByRole("button", { name: /./ });
      await expect(more).toHaveCount(1); // one button, the menu's, on a phone
      await more.click();
      const menu = page.locator('[data-component="PersonMenu"]');
      await expect(menu).toBeVisible();
      const at = await box(menu);
      expect(at.x).toBeGreaterThanOrEqual(0);
      expect(at.x + at.width).toBeLessThanOrEqual(width);
      for (const name of [m.usersLimits, m.usersRemove]) {
        const item = menu.getByRole("button", { name, exact: true });
        const b = await box(item);
        expect(b.height).toBeGreaterThanOrEqual(FINGER);
        // nothing clips it or lies over it
        const hit = await page.evaluate(
          ([x, y]) => document.elementFromPoint(x, y)?.closest("button")?.textContent,
          [b.x + b.width / 2, b.y + b.height / 2],
        );
        expect(hit).toBe(name);
      }
      await page.keyboard.press("Escape");
      await expect(menu).toBeHidden();
      await expect(more).toBeFocused();
    });
  });
}

test.describe("People on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("is a table, each row's Limits and Remove shown and no More actions", async ({ page }) => {
    await signInAs(page, ALICE);
    await page.goto("/admin/people");
    const bob = page.getByRole("row", { name: /bob@example.com/ });
    await expect(bob.getByRole("button", { name: m.usersLimits, exact: true })).toBeVisible();
    await expect(bob.getByRole("button", { name: m.usersRemove, exact: true })).toBeVisible();
    await expect(bob.getByRole("button", { name: m.usersMoreActionsFor(BOB) })).toBeHidden();
    expect(await page.locator(".card table").evaluate((t) => getComputedStyle(t).display)).toBe(
      "table",
    );
  });
});

test.describe("the Versions page on a phone", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 360, height: 800 } });

  test("keeps its tables, which scroll sideways inside their card, never the page", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/admin/claude");
    const scroll = page.locator(".card .table-scroll").filter({ has: page.locator("tbody td") });
    await expect(scroll.first()).toBeVisible();
    const first = scroll.first();
    expect(await first.locator("table").evaluate((t) => getComputedStyle(t).display)).toBe("table");
    // the measurement the lists are held to sees a table wider than its box: a cell planted
    // wider than the window
    await first
      .locator("tbody td")
      .first()
      .evaluate((td) => {
        td.textContent = "x".repeat(200);
      });
    expect(await first.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    const card = await box(
      first.locator("xpath=ancestor::*[contains(concat(' ', @class, ' '), ' card ')][1]"),
    );
    expect(card.x + card.width).toBeLessThanOrEqual(360);
    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  });
});

test("on a desktop the Limits dialog is a large one, still in the middle", async ({ page }) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1280, height: 800 });
  await OPENERS.LimitsDialog(page);
  const limits = await box(page.locator('dialog[open][data-component="LimitsDialog"]'));
  await page.keyboard.press("Escape");
  await OPENERS.RemoveDialog(page);
  const plain = await box(page.locator('dialog[open][data-component="RemoveDialog"]'));
  expect(limits.width).toBeGreaterThan(plain.width + 100);
  expect(Math.abs(limits.x - (1280 - limits.x - limits.width))).toBeLessThanOrEqual(1);
});
