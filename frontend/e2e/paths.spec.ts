import { type Locator, type Page, expect, test } from "@playwright/test";

import { m } from "../src/messages";
import { panel, summary, tree } from "./nav";
import { ALICE, useAs } from "./people";

// What holds, in a real browser: a path in Your servers and in a panel's
// breadcrumb wraps only between its parts, each part being a folder's name with
// the slash after it, so every line but the last ends right after a slash. A
// part is broken inside only when it is wider than a whole line on its own, and
// then it still fits.
const SHORT = "projects/alice-alpha";
const LONG = "projects/clients/acme-corporation/website-redesign-2026/frontend-app";
const TOO_WIDE = "projects/a-folder-whose-name-is-far-too-long-to-fit-on-any-line-of-the-summary-at-all";
const DEEP = `${LONG}/src/components/navigation-menu/dropdown-items`;

const at = (path: string, state: string) => ({ path, state, environment_url: null, session_name: null, exit_code: null });

// Each part of a path as laid out: its text, how many lines its own text takes,
// the top of its first line, its width unbroken against the width of the whole
// line it is laid out in, and its right edge against that line's.
async function parts(container: Locator) {
  return container.locator(".path-part").evaluateAll((elements) =>
    elements.map((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const rects = [...range.getClientRects()].filter((r) => r.width > 0);
      const line = el.closest("li, ol")!;
      const style = getComputedStyle(line);
      const lineBox = line.getBoundingClientRect();
      const lineWidth = lineBox.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const unbroken = el.cloneNode(true) as HTMLElement;
      unbroken.style.cssText = "position: absolute; visibility: hidden; white-space: nowrap; max-width: none";
      document.body.append(unbroken);
      const width = unbroken.getBoundingClientRect().width;
      unbroken.remove();
      return {
        text: el.textContent ?? "",
        lines: new Set(rects.map((r) => Math.round(r.top))).size,
        top: Math.round(Math.min(...rects.map((r) => r.top))),
        widerThanLine: width > lineWidth + 1,
        right: Math.max(...rects.map((r) => r.right)),
        lineRight: lineBox.right,
      };
    }),
  );
}

type Part = Awaited<ReturnType<typeof parts>>[number];

// The parts make up the path, every part but the last ends with a slash, and a
// part is broken inside only when it is wider than the whole line by itself.
function expectBreaksOnlyAfterSlashes(found: Part[], path: string) {
  expect(found.map((p) => p.text).join("")).toBe(path);
  found.forEach((part, i) => {
    if (i < found.length - 1) expect(part.text).toMatch(/\/$/);
    if (!part.widerThanLine) expect(part.lines, part.text).toBe(1);
  });
}

async function show(page: Page, width: number, address: string) {
  await useAs(page, ALICE);
  await page.setViewportSize({ width, height: 900 });
  const servers = [at(SHORT, "trust_prompt"), at(LONG, "ready"), at(TOO_WIDE, "stopped")];
  await page.route("**/api/servers", (route) => route.fulfill({ json: { servers } }));
  await page.goto(address);
  await expect(tree(page).getByRole("button", { name: "projects", exact: true })).toBeVisible();
  await expect(summary(page).getByRole("button", { name: TOO_WIDE })).toBeVisible();
}

for (const width of [600, 780, 900, 1200]) {
  test(`at ${width}px wide, the paths in Your servers break only right after a slash`, async ({ page }) => {
    await show(page, width, "/");
    for (const path of [SHORT, LONG, TOO_WIDE]) {
      expectBreaksOnlyAfterSlashes(await parts(summary(page).getByRole("button", { name: path })), path);
    }

    // positive control: the long path does take more than one line here
    const long = await parts(summary(page).getByRole("button", { name: LONG }));
    expect(new Set(long.map((p) => p.top)).size).toBeGreaterThan(1);

    // positive control: a name wider than any line is broken, and still fits
    const [, name] = await parts(summary(page).getByRole("button", { name: TOO_WIDE }));
    expect(name.widerThanLine).toBe(true);
    expect(name.lines).toBeGreaterThan(1);
    expect(name.right).toBeLessThanOrEqual(name.lineRight + 1);
  });

  test(`at ${width}px wide, a deep folder's breadcrumb breaks only between folders`, async ({ page }) => {
    await show(page, width, `/?folder=${DEEP}`);
    const crumbs = panel(page, DEEP).getByRole("navigation", { name: m.breadcrumb });
    // the breadcrumb's parts are its steps, from Home, each with the slash after it
    const found = await parts(crumbs);
    expectBreaksOnlyAfterSlashes(found, `${m.home}/${DEEP}`);
    expect(new Set(found.map((p) => p.top)).size).toBeGreaterThan(1); // positive control: it wraps
  });
}
