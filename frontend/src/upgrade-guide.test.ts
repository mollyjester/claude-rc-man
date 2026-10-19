import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";
import { GUIDE_ROLLBACK, GUIDE_WEB_UPGRADE } from "./Upgrade";

const GUIDE = resolve(__dirname, "../../docs/installation.md");
const SECTION = "## Upgrading from the web interface";

// The section of the guide under a heading of its level, to the next heading of the same level.
function section(markdown: string, heading: string): string {
  const start = markdown.indexOf(`\n${heading}\n`);
  if (start < 0) return "";
  const end = markdown.indexOf("\n## ", start + heading.length + 2);
  return markdown.slice(start, end < 0 ? undefined : end);
}

// The names the section must give in bold, as the page shows them: where the page is, and its
// controls.
const NAMED = [m.navAdmin, m.navRcman, m.upgradeRcman, m.upgradeReload];

const names = (markdown: string) =>
  [...markdown.matchAll(/\*\*([^*]+)\*\*/g)].map((b) => b[1].replace(/\s+/g, " "));
const missingNames = (markdown: string) => NAMED.filter((name) => !names(markdown).includes(name));

// A heading's anchor as a help page's link names it: lower case, words joined by hyphens.
const anchors = (markdown: string) =>
  [...markdown.matchAll(/^#+ (.+)$/gm)].map((h) =>
    h[1]
      .toLowerCase()
      .replace(/[^a-z0-9 -]/g, "")
      .replace(/ /g, "-"),
  );
// The anchors the page's links to the guide name that the guide has no heading for.
const missingAnchors = (markdown: string, links: string[]) =>
  links.filter((link) => {
    const [path, anchor] = link.split("#");
    return path !== "/help/installation" || !anchors(markdown).includes(anchor);
  });

test("the guide's section on upgrading from the web interface names the page and its controls", () => {
  const found = section(readFileSync(GUIDE, "utf-8"), SECTION);
  expect(found.length).toBeGreaterThan(0); // positive control: the section was read
  expect(missingNames(found)).toEqual([]);
});

test("the page's links to the guide lead to headings it has", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(anchors(guide)).toContain("upgrading-from-the-web-interface"); // positive control
  expect(missingAnchors(guide, [GUIDE_WEB_UPGRADE, GUIDE_ROLLBACK])).toEqual([]);
});

test("the checks fail on a section that leaves a control out, and on a link to no heading", () => {
  const right = NAMED.map((name) => `Choose **${name}**.`).join("\n");
  expect(missingNames(right)).toEqual([]);
  const without = right.replace(`**${m.upgradeReload}**`, m.upgradeReload);
  expect(without).not.toEqual(right); // the plant took effect
  expect(missingNames(without)).toEqual([m.upgradeReload]);
  const guide = "# Installing\n\n## Upgrading and rolling back\n";
  expect(missingAnchors(guide, ["/help/installation#upgrading-and-rolling-back"])).toEqual([]);
  expect(missingAnchors(guide, ["/help/installation#no-such-part"])).toEqual([
    "/help/installation#no-such-part",
  ]);
  expect(missingAnchors(guide, ["/help/claude-version#upgrading-and-rolling-back"])).toHaveLength(
    1,
  );
});
