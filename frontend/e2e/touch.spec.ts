import { fileURLToPath } from "node:url";

import { expect, type Locator, type Page, test } from "@playwright/test";

import { ADMIN_PAGES } from "../src/AdminPages";
import { m } from "../src/messages";
import { PAGES } from "../src/Pages";
import { callSites, pageSources } from "../src/test/callSites";
import { GITHUB_ADDRESS, panel, workspaces } from "./nav";
import { ALICE, signInAs } from "./people";

// What the stylesheet and the page's dialogs build on, proved in the browser itself: the
// rules for a touch screen are under (pointer: coarse), and every dialog is the native modal
// <dialog>, which jsdom has not got (src/test/setup.ts puts a stand-in in its place).

const coarse = () => matchMedia("(pointer: coarse)").matches;

// The page's own source folder, which the walk's coverage is held to.
const SRC = fileURLToPath(new URL("../src", import.meta.url));

test.describe("on a touch screen", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 800 } });

  test("the pointer is coarse", async ({ page }) => {
    await page.setContent("<p>touch</p>");
    expect(await page.evaluate(coarse)).toBe(true);
  });
});

test("with a mouse the pointer is not coarse", async ({ page }) => {
  await page.setContent("<p>mouse</p>");
  expect(await page.evaluate(() => matchMedia("(pointer: fine)").matches)).toBe(true); // positive control
  expect(await page.evaluate(coarse)).toBe(false);
});

// A page with a button that opens a modal dialog, and a button outside it.
const DIALOG_PAGE = `
  <button id="outside">outside</button>
  <button id="opener">open</button>
  <dialog id="dialog" aria-label="probe">
    <p>words</p>
    <label>Name <input id="first" type="text"></label>
    <button id="last" type="button">last</button>
  </dialog>
  <script>
    document.getElementById("opener").addEventListener("click", () =>
      document.getElementById("dialog").showModal());
  </script>`;

test("a native modal dialog takes focus, keeps it, closes on Escape and gives it back", async ({
  page,
}) => {
  await page.setContent(DIALOG_PAGE);
  await page.locator("#opener").focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "probe" });
  await expect(dialog).toBeVisible();
  // focus in: its first control that takes focus
  await expect(page.locator("#first")).toBeFocused();
  // what is outside cannot be reached, by a press or by Tab
  expect(await page.evaluate(() => document.getElementById("dialog")!.matches(":modal"))).toBe(
    true,
  );
  await page
    .locator("#outside")
    .click({ force: true, timeout: 2000 })
    .catch(() => {});
  await expect(page.locator("#outside")).not.toBeFocused();
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    const at = await page.evaluate(() => document.activeElement?.id ?? "");
    expect(["first", "last", "dialog", ""]).toContain(at);
    expect(at).not.toBe("outside");
    expect(at).not.toBe("opener");
  }
  // Escape closes it, and focus goes back to what opened it
  await page.locator("#first").focus();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.locator("#opener")).toBeFocused();
});

// --- every page, on a phone and on a touch screen ------------------------------------------

// The least a target is, high and wide, on a touch screen: WCAG 2.2's 2.5.5.
const TOUCH = 44;

// Every page's path, from the route table, a help page's for each page the backend has.
async function everyPath(page: Page): Promise<string[]> {
  const slugs: string[] = await page.evaluate(async () =>
    (await (await fetch("/api/docs")).json()).pages.map((p: { slug: string }) => p.slug),
  );
  expect(slugs.length).toBeGreaterThan(0); // positive control: the help pages are listed
  const paths = [...PAGES, ...ADMIN_PAGES].flatMap((p) =>
    p.path.includes(":slug") ? slugs.map((slug) => p.path.replace(":slug", slug)) : [p.path],
  );
  for (const path of paths)
    expect(path, "a route the walk does not know how to fill").not.toMatch(/:/);
  return paths;
}

// Controls too small for a finger that are right as they are, each with what makes it so: a
// control is let through only where it matches the selector, and, where inSentence is set,
// only where the block it is in holds words besides it. A walk that meets none of one fails,
// so that none is kept once its subject is gone.
const SMALL_ALLOWED: Record<string, { reason: string; selector: string; inSentence: boolean }> = {
  "a link in running text": {
    reason: "WCAG 2.5.5 leaves out a link inside a sentence or a block of text",
    selector: "a[href]:not(.button):not(nav a):not(header a):not(.menu a)",
    inSentence: true,
  },
};

// Boxes that may scroll sideways inside the page, each with why; any other box that does is a
// fault. A walk on a phone that meets none of one that scrolls fails.
const SCROLL_ALLOWED: Record<string, string> = {
  ".table-scroll": "a data table scrolls in its own box, as WCAG 1.4.10 allows",
  ".docs pre": "a help page's command keeps its lines, and scrolls in its own box",
};

type Found = {
  checked: number;
  small: string[];
  allowed: Record<string, number>;
  scrolls: Record<string, number>;
  wide: string[];
  fonts: string[];
};

// What the page shows now that breaks a rule: a control under TOUCH high or wide (a checkbox
// by its label, which is its target); a box that scrolls sideways, the top layer's dialogs and
// pop-ups among them, but for SCROLL_ALLOWED; anything past the window's right edge that no box
// of its own scrolls or clips; and a field whose text is under 16px. Icon-only buttons are
// marked, for their names to be read by the accessibility tree.
async function survey(page: Page): Promise<Found> {
  return page.evaluate(
    ({ touch, allowed, scrollAllowed }) => {
      const shown = (el: Element) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.checkVisibility({ visibilityProperty: true });
      };
      const describe = (el: Element) => {
        const text = (el.textContent ?? "").trim().slice(0, 30);
        const label = el.getAttribute("aria-label") ?? "";
        const classes = el.className ? `.${String(el.className).split(" ").join(".")}` : "";
        return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}${classes} "${label || text}"`;
      };
      // the block a link is in holds words of its own besides the link; a table's cell, which
      // may hold a single word beside it, a status say, only where they are three or more
      const inSentence = (el: Element) => {
        const block = el.closest("p, li, td, th, dd, figcaption");
        if (block === null) return false;
        const others = (block.textContent ?? "").replace(el.textContent ?? "", "");
        const words = others.match(/[A-Za-z]{2,}/g)?.length ?? 0;
        return words >= (block.matches("td, th") ? 3 : 1);
      };
      const found = {
        checked: 0,
        small: [] as string[],
        allowed: Object.fromEntries(Object.keys(allowed).map((k) => [k, 0])),
        scrolls: Object.fromEntries(Object.keys(scrollAllowed).map((k) => [k, 0])),
        wide: [] as string[],
        fonts: [] as string[],
      };
      for (const el of document.querySelectorAll(
        'button, a[href], input:not([type="hidden"]), select, textarea, summary, [role="tab"]',
      )) {
        if (el.closest(".xterm")) continue; // the terminal's own, drawn by xterm.js
        const target = el.matches('input[type="checkbox"]') ? el.closest("label") : el;
        if (target === null) {
          found.small.push(`${describe(el)}: a checkbox with no label to press`);
          continue;
        }
        if (!shown(target)) continue;
        found.checked++;
        const r = target.getBoundingClientRect();
        if (r.height < touch - 0.5 || r.width < touch - 0.5) {
          const exempt = Object.entries(allowed).find(
            ([, rule]) => el.matches(rule.selector) && (!rule.inSentence || inSentence(el)),
          );
          if (exempt) found.allowed[exempt[0]]++;
          else
            found.small.push(`${describe(target)}: ${Math.round(r.width)}×${Math.round(r.height)}`);
        }
        if (el.matches('input:not([type="checkbox"]), textarea, select')) {
          const size = parseFloat(getComputedStyle(el).fontSize);
          if (size < 16) found.fonts.push(`${describe(el)}: ${size}px`);
        }
        if (el.matches("button") && (el as HTMLElement).innerText.trim() === "")
          el.setAttribute("data-icon-only", "");
      }
      const edge = document.documentElement.clientWidth;
      if (document.documentElement.scrollWidth > edge)
        found.wide.push(`the page: ${document.documentElement.scrollWidth}px in ${edge}px`);
      for (const el of document.querySelectorAll("body *")) {
        if (!shown(el) || el.closest(".xterm")) continue;
        // a box of its own that scrolls sideways, as a dialog or a pop-up would
        const overflow = getComputedStyle(el).overflowX;
        if ((overflow === "auto" || overflow === "scroll") && el.scrollWidth > el.clientWidth + 1) {
          const exempt = Object.keys(scrollAllowed).find((name) => el.matches(name));
          if (exempt) found.scrolls[exempt]++;
          else
            found.wide.push(
              `${describe(el)}: scrolls sideways, ${el.scrollWidth}px in ${el.clientWidth}px`,
            );
        }
        // past the window's edge, with nothing of its own that scrolls or clips it
        if (el.getBoundingClientRect().right <= edge + 1) continue;
        let boxed = false;
        for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
          if (getComputedStyle(up).overflowX !== "visible") boxed = true;
        }
        if (!boxed)
          found.wide.push(`${describe(el)}: to ${Math.round(el.getBoundingClientRect().right)}px`);
      }
      return found;
    },
    { touch: TOUCH, allowed: SMALL_ALLOWED, scrollAllowed: SCROLL_ALLOWED },
  );
}

// The marked icon-only buttons with no name the accessibility tree reads out.
async function nameless(page: Page): Promise<string[]> {
  const marked = page.locator("[data-icon-only]");
  const found: string[] = [];
  for (let i = 0; i < (await marked.count()); i++) {
    const snapshot = await marked.nth(i).ariaSnapshot();
    if (!/^- button "[^"]+"/.test(snapshot))
      found.push(await marked.nth(i).evaluate((el) => el.outerHTML.slice(0, 80)));
  }
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("[data-icon-only]"))
      el.removeAttribute("data-icon-only");
  });
  return found;
}

// A workspace cloned from the world's one repository, ready, by id.
async function clonedWorkspace(page: Page, name: string): Promise<number> {
  const id: number = await page.evaluate(
    async ({ url, name }) => {
      const reply = await fetch("/api/git/clone", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url, name }),
      });
      return (await reply.json()).workspace.id;
    },
    { url: GITHUB_ADDRESS, name },
  );
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
      { timeout: 30_000 },
    )
    .toBe("ready");
  return id;
}

// A page's state to look at: where it is, what is opened there first, which says whether it
// opened a dialog or a pop-up, and the component whose dialog or pop-up that is (callSites in
// src/test/callSites.ts), which the walk holds to the sources so that none goes unvisited, and
// to what opened (labelFaults).
type View = {
  name: string;
  path: string;
  open?: (page: Page) => Promise<boolean>;
  dialog?: string;
  popup?: string;
};

// Opens a dialog or a pop-up with its button, which is then open. Where the view says the
// button is not shown at this width, it is checked to be absent instead, and nothing opened.
const opens =
  (button: (page: Page) => Locator, shown = true) =>
  async (page: Page) => {
    const it = button(page);
    if (!shown) {
      await expect(it).toBeHidden();
      return false;
    }
    await it.click();
    await expect(page.locator('dialog[open], [aria-expanded="true"]').first()).toBeVisible();
    return true;
  };

// The components whose dialogs and pop-ups are open now, by the data-component each is marked
// with (src/Dialog.tsx, src/usePopup.ts): an open dialog's own, and a pop-up's panel's, which
// its expanded button's aria-controls names. A button that discloses a part of the page in
// place, which is no pop-up, controls a part with no mark, and is passed over; one whose
// panel is not there, or that names none, is reported.
async function openComponents(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const open: string[] = [];
    for (const dialog of document.querySelectorAll("dialog[open]"))
      open.push(
        dialog.getAttribute("data-component") ?? `unmarked: ${dialog.outerHTML.slice(0, 60)}`,
      );
    for (const button of document.querySelectorAll('[aria-expanded="true"]')) {
      const id = button.getAttribute("aria-controls");
      const panel = id ? document.getElementById(id) : null;
      if (panel === null) open.push(`no panel: what ${button.outerHTML.slice(0, 60)} opens`);
      else if (panel.hasAttribute("data-component"))
        open.push(panel.getAttribute("data-component")!);
    }
    return open.sort();
  });
}

// A view's label against what is open now: what it says it opened, where it opened something,
// and nothing otherwise.
async function labelFaults(page: Page, view: View, opened: boolean): Promise<string[]> {
  const labelled = opened ? [view.dialog, view.popup].filter((c) => c !== undefined).sort() : [];
  const open = await openComponents(page);
  return JSON.stringify(open) === JSON.stringify(labelled)
    ? []
    : [`${view.name}: labelled ${JSON.stringify(labelled)}, but open is ${JSON.stringify(open)}`];
}

// What the Changes section's status says, with something staged to commit and a push that
// can be tried, so that Commit and its menu are offered.
async function withSomethingStaged(page: Page) {
  await page.route("**/api/git/status*", async (route) => {
    const status = await (await route.fetch()).json();
    await route.fulfill({
      json: {
        ...status,
        staged: { files: [{ path: "planted.txt", change: "added" }], more: 0 },
        author: { name: "Alice", email: "alice@example.com" },
        push_has_remote: true,
        push_to_github: false,
      },
    });
  });
}

// Whether the page lays itself out for a phone at a width: the list and the panel take turns.
const PHONE_LAYOUT = 768; // 48rem in src/styles.css

function views(paths: string[], id: number, name: string, width: number): View[] {
  const banner = (page: Page) => page.getByRole("banner");
  const trash = (name: string) => (page: Page) =>
    workspaces(page).getByRole("button", { name: m.deleteWorkspaceNamed(name), exact: true });
  const opened = `/?workspace=${id}`;
  return [
    ...paths.map((path) => ({ name: path, path })),
    {
      name: "New workspace",
      path: "/",
      dialog: "NewWorkspace",
      open: opens((p) => p.getByRole("button", { name: m.newWorkspace, exact: true })),
    },
    {
      name: "the account menu",
      path: "/",
      popup: "AccountMenu",
      open: opens((p) => banner(p).getByRole("button", { name: m.account })),
    },
    {
      name: "the Admin menu",
      path: "/",
      popup: "AdminMenu",
      open: opens((p) => banner(p).getByRole("button", { name: m.navAdmin })),
    },
    {
      name: "deleting a workspace",
      path: "/",
      dialog: "DeleteWorkspaceDialog",
      open: opens(trash(name)),
    },
    {
      name: "a workspace that cannot be deleted",
      path: "/",
      dialog: "CannotDeleteDialog",
      open: async (page) => {
        await page.route("**/api/workspaces", async (route) => {
          const answer = await (await route.fetch()).json();
          answer.workspaces.push({
            ...answer.workspaces[0],
            id: 999_999,
            name: "alice-being-made",
            state: "creating",
            source: null,
            source_page: null,
            server: null,
          });
          await route.fulfill({ json: answer });
        });
        await page.reload();
        const opened = await opens(trash("alice-being-made"))(page);
        await page.unroute("**/api/workspaces");
        return opened;
      },
    },
    { name: "a workspace, Overview", path: opened },
    {
      name: "a workspace, Terminal",
      path: `${opened}&tab=terminal`,
      open: async (page) => {
        await expect(page.getByText(m.terminalConnected)).toBeVisible();
        return false;
      },
    },
    {
      name: "the switch to another workspace",
      path: opened,
      popup: "PanelBar",
      // over the panel only where the list is not beside it
      open: opens((p) => p.getByRole("button", { name: m.switchWorkspace }), width <= PHONE_LAYOUT),
    },
    {
      name: "the branch lookup",
      path: opened,
      popup: "Lookup",
      open: opens((p) => p.getByRole("button", { name: m.onBranch("main") })),
    },
    {
      name: "a new branch",
      path: opened,
      popup: "NewBranch",
      open: opens((p) => p.getByRole("button", { name: m.newBranch })),
    },
    {
      name: "Commit's menu",
      path: opened,
      popup: "CommitMenu",
      open: async (page) => {
        await withSomethingStaged(page);
        await page.reload();
        const opened = await opens((p) => p.getByRole("button", { name: m.commitMenu }))(page);
        await page.unroute("**/api/git/status*");
        return opened;
      },
    },
    {
      name: "removing the GitHub token",
      path: "/settings",
      dialog: "RemoveTokenDialog",
      open: async (page) => {
        await page.route("**/api/github", (route) => route.fulfill({ json: { token: true } }));
        await page.reload();
        const opened = await opens((p) => p.getByRole("button", { name: m.githubTokenRemove }))(
          page,
        );
        await page.unroute("**/api/github");
        return opened;
      },
    },
    {
      name: "a person's limits",
      path: "/admin/people",
      dialog: "LimitsDialog",
      open: opens((p) =>
        p
          .getByRole("row", { name: /bob@example.com/ })
          .getByRole("button", { name: m.usersLimits }),
      ),
    },
    {
      name: "removing a person",
      path: "/admin/people",
      dialog: "RemoveDialog",
      open: opens((p) =>
        p
          .getByRole("row", { name: /bob@example.com/ })
          .getByRole("button", { name: m.usersRemove }),
      ),
    },
  ];
}

// The components the walk's views open, against those the sources have.
function walkCovers(views: View[]) {
  const { faults, ...sites } = callSites(pageSources(SRC));
  expect(faults).toEqual([]); // each component named for sure
  const dialogs = [...new Set(views.flatMap((v) => (v.dialog ? [v.dialog] : [])))].sort();
  const popups = [...new Set(views.flatMap((v) => (v.popup ? [v.popup] : [])))].sort();
  return { expected: sites, walked: { dialogs, popups } };
}

test("the walk opens every dialog and pop-up the sources have, and the check sees one left out", () => {
  const all = views(["/"], 1, "x", 390);
  const { expected, walked } = walkCovers(all);
  expect(expected.dialogs).toContain("LimitsDialog"); // positive control: the sources were read
  expect(walked).toEqual(expected);
  const without = walkCovers(all.filter((v) => v.popup !== "CommitMenu"));
  expect(without.walked.popups).not.toContain("CommitMenu"); // the plant took effect
  expect(without.walked).not.toEqual(without.expected);
});

// Each width: two phones', and a tablet's, which is wide enough for the list beside the panel.
for (const [width, phone] of [
  [320, true],
  [390, true],
  [1024, false],
] as const) {
  test.describe(`on a touch screen ${width}px wide`, () => {
    test.use({ isMobile: phone, hasTouch: true, viewport: { width, height: 800 } });

    test("no view is wider than the window, every control is big enough for a finger, and every icon-only button is named", async ({
      page,
    }) => {
      test.slow(); // every page, and each dialog and menu, at one width
      await signInAs(page, ALICE);
      await page.goto("/");
      expect(await page.evaluate(coarse)).toBe(true); // positive control: the rules for touch apply
      const name = `alice-touch-${width}`;
      const all = views(await everyPath(page), await clonedWorkspace(page, name), name, width);
      const faults: string[] = [];
      const allowed: Record<string, number> = {};
      const scrolls: Record<string, number> = {};
      let checked = 0;
      let opened = 0;
      for (const view of all) {
        await page.goto(view.path);
        await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
        const didOpen = (await view.open?.(page)) ?? false;
        if (didOpen) opened += await page.locator('dialog[open], [aria-expanded="true"]').count();
        faults.push(...(await labelFaults(page, view, didOpen)));
        await page.waitForTimeout(100); // a pop-up's or a dialog's first frame
        const found = await survey(page);
        checked += found.checked;
        for (const [name, count] of Object.entries(found.allowed))
          allowed[name] = (allowed[name] ?? 0) + count;
        for (const [name, count] of Object.entries(found.scrolls))
          scrolls[name] = (scrolls[name] ?? 0) + count;
        faults.push(
          ...[
            ...found.small,
            ...found.wide,
            ...found.fonts,
            ...(await nameless(page)).map((b) => `a button with no name: ${b}`),
          ].map((fault) => `${view.name}: ${fault}`),
        );
      }
      expect(checked).toBeGreaterThan(0); // non-vacuity
      expect(opened).toBeGreaterThan(0); // the dialogs and pop-ups were looked at open
      expect(faults).toEqual([]);
      // each exemption still has something to exempt
      for (const name of Object.keys(SMALL_ALLOWED)) expect(allowed[name], name).toBeGreaterThan(0);
      // and a box let scroll is met scrolling, on a phone, where the window is narrow enough
      if (phone)
        for (const name of Object.keys(SCROLL_ALLOWED))
          expect(scrolls[name], name).toBeGreaterThan(0);
    });
  });
}

test.describe("the walk's checks", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 800 } });

  test("fail on a planted small button and link, a button with no name, a field's small text and a box past the edge", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const clean = await survey(page);
    expect(clean.checked).toBeGreaterThan(0);
    expect([...clean.small, ...clean.wide, ...clean.fonts]).toEqual([]);
    expect(await nameless(page)).toEqual([]);
    await page.evaluate(() => {
      const main = document.querySelector("main")!;
      main.insertAdjacentHTML(
        "beforeend",
        `<button id="planted-small" style="all: unset; width: 20px; height: 20px">s</button>
         <a id="planted-link" href="#planted" style="font-size: 10px">x</a>
         <p>Words around <a id="planted-in-text" href="#planted">a link</a> in a sentence.</p>
         <table><tbody><tr><td><a id="planted-in-cell" href="#planted">cell</a></td>
           <td>Ready <a id="planted-by-status" href="#planted">log</a></td>
           <td>makes a workspace (see <a id="planted-cell-sentence" href="#planted">it</a>)</td></tr></tbody></table>
         <button id="planted-nameless" type="button"><svg aria-hidden="true" width="16" height="16"></svg></button>
         <input id="planted-field" type="text" style="font-size: 12px">
         <div id="planted-wide" style="width: 2000px; height: 10px"></div>`,
      );
    });
    const planted = await survey(page);
    expect(planted.small.some((s) => s.startsWith("button#planted-small"))).toBe(true);
    // a small link on its own, and one in a table's cell, alone or beside a word, are not
    // running text
    expect(planted.small.some((s) => s.startsWith("a#planted-link"))).toBe(true);
    expect(planted.small.some((s) => s.startsWith("a#planted-in-cell"))).toBe(true);
    expect(planted.small.some((s) => s.startsWith("a#planted-by-status"))).toBe(true);
    // positive control: one in a cell that holds a sentence is
    expect(planted.small.some((s) => s.startsWith("a#planted-cell-sentence"))).toBe(false);
    expect(planted.small.some((s) => s.startsWith("a#planted-in-text"))).toBe(false);
    expect(planted.allowed["a link in running text"]).toBeGreaterThan(0);
    expect(planted.fonts).toHaveLength(1);
    expect(planted.wide.some((s) => s.startsWith("the page"))).toBe(true);
    expect(planted.wide.some((s) => s.startsWith("div#planted-wide"))).toBe(true);
    expect(await nameless(page)).toHaveLength(1);
  });

  test("fail on a plain box that scrolls sideways, but not on a help page's command, and count only the allowed boxes that scroll", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const clean = await survey(page);
    expect(clean.wide).toEqual([]); // positive control: clean before the plant
    const long = "x".repeat(400);
    await page.evaluate((long) => {
      document.querySelector("main")!.insertAdjacentHTML(
        "beforeend",
        `<pre id="planted-pre" style="overflow-x: auto">${long}</pre>
           <div class="docs"><pre id="planted-command" style="overflow-x: auto">${long}</pre>
             <pre id="planted-short" style="overflow-x: auto">short</pre></div>`,
      );
    }, long);
    const found = await survey(page);
    expect(
      found.wide.some((s) => s.startsWith("pre#planted-pre") && s.includes("scrolls sideways")),
    ).toBe(true);
    expect(found.wide.some((s) => s.startsWith("pre#planted-command"))).toBe(false);
    // the short command is there but does not scroll: it is not counted
    expect(found.scrolls[".docs pre"]).toBe(clean.scrolls[".docs pre"] + 1);
  });

  test("fail on a view whose label is not what its button opens", async ({ page }) => {
    await signInAs(page, ALICE);
    const all = views(["/"], 1, "x", 390);
    for (const [name, wrong] of [
      ["the account menu", { popup: "AdminMenu" }],
      ["New workspace", { dialog: "DeleteWorkspaceDialog" }],
    ] as const) {
      const view = all.find((v) => v.name === name)!;
      await page.goto(view.path);
      await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
      const opened = await view.open!(page);
      expect(await labelFaults(page, view, opened)).toEqual([]); // positive control: right as it is
      const mislabelled = { name: view.name, path: view.path, ...wrong };
      expect(await labelFaults(page, mislabelled, opened)).toHaveLength(1);
    }
  });

  test("fail on a box too wide inside an open dialog, which never widens the page", async ({
    page,
  }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    await page.getByRole("button", { name: m.newWorkspace, exact: true }).click();
    const dialog = page.getByRole("dialog", { name: m.newWorkspace });
    await expect(dialog).toBeVisible();
    expect((await survey(page)).wide).toEqual([]); // positive control: clean while open
    await dialog.evaluate((el) =>
      el
        .querySelector("form")!
        .insertAdjacentHTML(
          "beforeend",
          '<div id="planted-wide" style="width: 2000px; height: 10px"></div>',
        ),
    );
    const found = await survey(page);
    expect(found.wide.some((s) => s.startsWith("dialog") && s.includes("scrolls sideways"))).toBe(
      true,
    );
    expect(found.wide.some((s) => s.startsWith("the page"))).toBe(false); // the top layer's own
  });
});

test.describe("on a phone", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 800 } });

  test("the list leads to a workspace, back to the list, and on to another", async ({ page }) => {
    await signInAs(page, ALICE);
    await page.goto("/");
    for (const name of ["alice-phone-a", "alice-phone-b"]) {
      expect(
        await page.evaluate(
          async (name) =>
            (
              await fetch("/api/workspaces", {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({ name }),
              })
            ).status,
          name,
        ),
      ).toBeLessThan(300);
    }
    await page.reload();
    const list = workspaces(page);
    await list.getByRole("button", { name: "alice-phone-a", exact: true }).click();
    const a = panel(page, "alice-phone-a");
    await expect(a.getByRole("heading", { name: "alice-phone-a" })).toBeVisible();
    await expect(list).toBeHidden(); // the panel in the list's place
    // on to the other, from the panel
    await page.getByRole("button", { name: m.switchWorkspace }).click();
    await page.getByRole("button", { name: "alice-phone-b", exact: true }).click();
    const b = panel(page, "alice-phone-b");
    await expect(b.getByRole("heading", { name: "alice-phone-b" })).toBeVisible();
    await expect(a).toHaveCount(0);
    // and back to the list
    await page.getByRole("link", { name: m.backToWorkspaces }).click();
    await expect(list).toBeVisible();
    await expect(b).toHaveCount(0);
    expect(new URL(page.url()).searchParams.get("workspace")).toBeNull();
    await expect(page.getByRole("link", { name: m.backToWorkspaces })).toHaveCount(0);
  });
});
