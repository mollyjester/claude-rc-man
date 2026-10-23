import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { SETTINGS_ANCHOR } from "./Monitor";
import { m } from "./messages";

const GUIDE = resolve(__dirname, "../../docs/monitor.md");

// The names the guide must give in bold, as the page shows them: where the page is, its parts,
// its two rounds, and the settings and what they hold.
const NAMED = [
  m.navAdmin,
  m.navMonitor,
  m.monitorHost,
  m.monitorPeople,
  m.monitorContainers,
  m.monitorUsage,
  m.monitorDisk,
  m.monitorSettingsTitle,
  m.monitorUsageInterval,
  m.monitorDiskInterval,
  m.monitorNotKnown,
  m.save,
];

// Every piece of text in the message catalogue that is not worked out from values.
function catalogue(value: unknown = m): string[] {
  if (typeof value === "string") return [value];
  if (value && typeof value === "object") return Object.values(value).flatMap((v) => catalogue(v));
  return [];
}

// Each name in bold in the guide, its line breaks as spaces.
const names = (markdown: string) =>
  [...markdown.matchAll(/\*\*([^*]+)\*\*/g)].map((b) => b[1].replace(/\s+/g, " "));

// The bold names the guide uses that the page does not show.
function unknownNames(markdown: string): string[] {
  const texts = new Set(catalogue());
  return names(markdown).filter((name) => !texts.has(name));
}

// The names the guide should give in bold and does not.
const missingNames = (markdown: string) => NAMED.filter((name) => !names(markdown).includes(name));

// A heading's anchor as a help page's link names it: lower case, words joined by hyphens.
const anchors = (markdown: string) =>
  [...markdown.matchAll(/^#+ (.+)$/gm)].map((h) =>
    h[1]
      .toLowerCase()
      .replace(/[^a-z0-9 -]/g, "")
      .replace(/ /g, "-"),
  );

test("every name the Monitor guide gives in bold is text the page shows", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(names(guide).length).toBeGreaterThan(0); // positive control: the guide was read
  expect(unknownNames(guide)).toEqual([]);
});

test("the guide names the page, its parts, its rounds and its settings", () => {
  expect(missingNames(readFileSync(GUIDE, "utf-8"))).toEqual([]);
});

test("the guide has a section named as the page's settings card, which the page links to", () => {
  expect(SETTINGS_ANCHOR).toBe(anchors(`## ${m.monitorSettingsTitle}`)[0]);
  expect(anchors(readFileSync(GUIDE, "utf-8"))).toContain(SETTINGS_ANCHOR);
});

test("the checks fail on a name the page does not have, and on a guide that leaves one out", () => {
  const right = NAMED.map((name) => `Choose **${name}**.`).join("\n");
  expect(unknownNames(right)).toEqual([]);
  expect(missingNames(right)).toEqual([]);

  const planted = `${right}\nChoose **Frobnicate**.`;
  expect(names(planted)).toContain("Frobnicate"); // the plant took effect
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);

  const without = right.replace(`**${m.monitorDiskInterval}**`, m.monitorDiskInterval);
  expect(without).not.toEqual(right); // the plant took effect
  expect(missingNames(without)).toEqual([m.monitorDiskInterval]);
  expect(missingNames("")).toEqual(NAMED);
});
