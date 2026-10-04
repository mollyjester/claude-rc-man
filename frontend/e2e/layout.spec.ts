import { expect, type Page, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel, summary, tree } from "./nav";
import { ALICE, signInAs } from "./people";

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
  test(`at ${width}px wide, a server's screen scrolls inside its card instead of widening the page`, async ({
    page,
  }) => {
    await signInAs(page, ALICE);
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
    const screen = card.getByRole("figure", { name: m.screenTitle(server.path) }).locator("pre");
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

test("Your servers lines its paths up with its heading, each badge on its path's line", async ({
  page,
}) => {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 1200, height: 800 });
  // the longest badge there is, beside a path
  const server = {
    path: "projects/alice-alpha",
    state: "trust_prompt",
    environment_url: null,
    session_name: null,
    exit_code: null,
  };
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
  await signInAs(page, ALICE);
  await page.setViewportSize({ width: 780, height: 800 });
  const server = {
    path: "projects/alice-alpha",
    state: "trust_prompt",
    environment_url: null,
    session_name: null,
    exit_code: null,
  };
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
  await signInAs(page, ALICE);
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
  const list = page
    .getByRole("region", { name: m.changesTitle })
    .getByRole("group", { name: m.notStagedTitle });
  const plain = list.getByText("plain.txt", { exact: true });
  const odd = list.getByText("caf�.txt", { exact: true });
  await expect(plain).toBeVisible();
  await expect(odd).toBeVisible();
  const [plainBox, oddBox] = await Promise.all([plain.boundingBox(), odd.boundingBox()]);
  expect(Math.abs(plainBox!.x - oddBox!.x)).toBeLessThanOrEqual(1);
});

// A branch name longer than a narrow pop-up is wide.
const LONG_BRANCH = "issue-38/real-claude-auth-status-with-a-really-long-name";

// A repository whose Changes section has everything the bar and Commit offer:
// an upstream, something to push, something staged and an author.
async function repositoryOpened(page: Page, width: number) {
  await signInAs(page, ALICE);
  await page.setViewportSize({ width, height: 900 });
  const path = "projects/alice-repo";
  const status = {
    path,
    branch: { name: "main", commit: "0123456789", upstream: "origin/main", ahead: 2, behind: 1 },
    merging: false,
    staged: { files: [{ path: "staged.txt", change: "added" }], more: 0 },
    not_staged: { files: [{ path: "changed.txt", change: "modified" }], more: 0 },
    author: { name: "Alice", email: ALICE },
    push_has_remote: true,
    push_to_github: false,
    github_token: null,
    push_ahead: 2,
  };
  const branches = {
    path,
    local: {
      branches: [
        { name: "main", current: true, upstream: "origin/main" },
        { name: LONG_BRANCH, current: false, upstream: `origin/${LONG_BRANCH}` },
      ],
      more: 0,
    },
    remote: {
      branches: [
        { name: "origin/main", local: "main", taken_by: "main" },
        { name: `origin/${LONG_BRANCH}`, local: LONG_BRANCH, taken_by: LONG_BRANCH },
      ],
      more: 0,
    },
  };
  await page.route("**/api/git/status*", (route) => route.fulfill({ json: status }));
  await page.route("**/api/git/branches*", (route) => route.fulfill({ json: branches }));
  await page.goto(`/?folder=${path}`);
  const changes = page.getByRole("region", { name: m.changesTitle });
  await expect(changes.getByRole("button", { name: m.push, exact: true })).toBeVisible();
  return changes;
}

// 1280px leaves the panel room for the bar beside the heading; 360px does not.
for (const [width, beside] of [
  [1280, true],
  [360, false],
] as const) {
  test(`at ${width}px wide, the branch bar is ${beside ? "beside" : "under"} the Changes heading`, async ({
    page,
  }) => {
    const changes = await repositoryOpened(page, width);
    // measured in one go: the Claude account loading above moves both alike
    const { heading, bar } = await changes.evaluate((section, newBranch) => {
      const box = (el: Element) => {
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      };
      return {
        heading: box(section.querySelector("h3")!),
        bar: box(section.querySelector(`button[aria-label="${newBranch}"]`)!),
      };
    }, m.newBranch);
    expect(heading.height).toBeGreaterThan(0); // positive control: both are there
    expect(bar.height).toBeGreaterThan(0);
    if (beside) {
      expect(bar.y).toBeLessThan(heading.y + heading.height); // on the heading's line
      expect(bar.x).toBeGreaterThan(heading.x + heading.width);
    } else {
      expect(bar.y).toBeGreaterThanOrEqual(heading.y + heading.height - 1); // below it
    }
    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  });

  test(`at ${width}px wide, each of the Changes pop-ups lies inside the section`, async ({
    page,
  }) => {
    const changes = await repositoryOpened(page, width);
    const openers = [
      changes.getByRole("button", { name: m.onBranch("main") }),
      changes.getByRole("button", { name: m.newBranch }),
      changes.getByRole("button", { name: m.commitMenu }),
    ];
    // The bar's pop-ups are held inside the section across, at any width; they
    // overlay what is below them, so on a short section they may reach past its
    // foot, and their bottom edge is not checked. Commit's menu opens upward
    // from the foot of the section, so it is held inside on all four sides.
    for (const opener of openers) {
      await opener.click();
      await expect(opener).toHaveAttribute("aria-expanded", "true");
      const id = (await opener.getAttribute("aria-controls"))!;
      // measured in one go, so that nothing loading meanwhile moves one and not the other
      const { section, popup } = await changes.evaluate((element, popupId) => {
        const box = (el: Element) => {
          const r = el.getBoundingClientRect();
          return { x: r.x, y: r.y, width: r.width, height: r.height };
        };
        return { section: box(element), popup: box(document.getElementById(popupId)!) };
      }, id);
      expect(popup.width).toBeGreaterThan(0); // positive control: it is shown
      expect(popup.x).toBeGreaterThanOrEqual(section.x - 1);
      expect(popup.x + popup.width).toBeLessThanOrEqual(section.x + section.width + 1);
      expect(popup.y).toBeGreaterThanOrEqual(section.y - 1);
      if (opener === openers[2])
        expect(popup.y + popup.height).toBeLessThanOrEqual(section.y + section.height + 1);
      await page.keyboard.press("Escape");
      await expect(opener).toHaveAttribute("aria-expanded", "false");
    }
  });
}

test("at 360px wide, the lookup shows each branch's name whole", async ({ page }) => {
  const changes = await repositoryOpened(page, 360);
  await changes.getByRole("button", { name: m.onBranch("main") }).click();
  const local = changes.getByRole("list", { name: m.localBranches });
  const remote = changes.getByRole("list", { name: m.remoteBranches });
  for (const name of [LONG_BRANCH, `origin/${LONG_BRANCH}`]) {
    const shown = (name === LONG_BRANCH ? local : remote).getByText(name, { exact: true });
    await expect(shown).toBeVisible();
    const clipped = await shown.evaluate(
      (el) => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1,
    );
    expect(clipped, name).toBe(false);
    // positive control: the name is longer than the pop-up is wide, so it had to wrap
    const [box, popup] = await Promise.all([
      shown.boundingBox(),
      changes.locator(".popover").boundingBox(),
    ]);
    expect(box!.height).toBeGreaterThan(25);
    expect(box!.x + box!.width).toBeLessThanOrEqual(popup!.x + popup!.width);
  }
});

test("at 360px wide, each list's action keeps to its heading's line", async ({ page }) => {
  const changes = await repositoryOpened(page, 360);
  for (const [title, action] of [
    [m.stagedTitle, m.unstageSelected],
    [m.notStagedTitle, m.stageSelected],
  ]) {
    const list = changes.getByRole("group", { name: title, exact: true });
    const { heading, button, words } = await list.evaluate((group, name) => {
      const box = (el: Element) => {
        const r = el.getBoundingClientRect();
        return { y: r.y, height: r.height, right: r.right };
      };
      const found = [...group.querySelectorAll("button")].find((b) => b.textContent === name)!;
      const toggle = group.querySelector("h4 button")!;
      return {
        heading: box(group.querySelector("h4")!),
        button: { ...box(found), left: found.getBoundingClientRect().left },
        // where the heading's own words end, its count among them
        words: Math.max(...[...toggle.children].map((c) => c.getBoundingClientRect().right)),
      };
    }, action);
    expect(heading.height).toBeGreaterThan(0); // positive control: both are there
    const middle = button.y + button.height / 2;
    expect(middle).toBeGreaterThanOrEqual(heading.y);
    expect(middle).toBeLessThanOrEqual(heading.y + heading.height);
    expect(words, title).toBeLessThanOrEqual(button.left); // and does not cover its words
  }
  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
});
