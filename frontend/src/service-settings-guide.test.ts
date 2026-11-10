import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { m } from "./messages";
import { anchors, missingNames as missing, names, unknownNames } from "./test/guideNames";

const DOCS = resolve(__dirname, "../../docs");
const GUIDE = resolve(DOCS, "service-settings.md");

// The names the guide must give in bold, as the page shows them: where the page is, each card,
// and each setting it holds.
const NAMED = [
  m.navAdmin,
  m.navServiceSettings,
  m.monitorSettingsTitle,
  m.monitorUsageInterval,
  m.monitorDiskInterval,
  m.monitorHost,
  m.monitorPeople,
  m.monitorContainers,
  m.forwardingTitle,
  m.forwardingEnabled,
  m.forwardingIdle,
  m.forwardingAbsolute,
  m.save,
];
const missingNames = (markdown: string) => missing(NAMED, markdown);
// The cards, by title: the guide has a section named as each.
const CARDS = [m.monitorSettingsTitle, m.forwardingTitle];
const anchorOf = (title: string) => anchors(`## ${title}`)[0];

test("every name the Service settings guide gives in bold is text the page shows, it names the page, its cards and their settings with a section for each card, and every link to its sections names one it has", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(names(guide).length).toBeGreaterThan(0); // positive control: the guide was read
  expect(unknownNames(guide)).toEqual([]);
  expect(missingNames(guide)).toEqual([]);
  const found = anchors(guide);
  for (const title of CARDS) expect(found).toContain(anchorOf(title));
  const pages = helpPages();
  // positive control: the guides that point at it are read (monitor.md, configuration.md, audit.md)
  expect(pages.join("\n").match(/service-settings\.md#/g)?.length ?? 0).toBeGreaterThanOrEqual(3);
  expect(brokenLinks(pages, guide)).toEqual([]);
});

// The sections of the guide that links in the help pages name and the guide does not have.
const brokenLinks = (pages: string[], guide: string) => {
  const found = anchors(guide);
  return pages
    .flatMap((page) => [...page.matchAll(/service-settings\.md#([a-z0-9-]+)/g)].map((l) => l[1]))
    .filter((anchor) => !found.includes(anchor));
};
const helpPages = () =>
  readdirSync(DOCS)
    .filter((file) => file.endsWith(".md"))
    .map((file) => readFileSync(resolve(DOCS, file), "utf-8"));

test("the checks fail on a name the page does not have, a guide that leaves one out, and a link to a section it does not have", () => {
  const right = NAMED.map((name) => `Choose **${name}**.`).join("\n");
  expect(unknownNames(right)).toEqual([]);
  expect(missingNames(right)).toEqual([]);

  const planted = `${right}\nChoose **Frobnicate**.`;
  expect(names(planted)).toContain("Frobnicate"); // the plant took effect
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);

  const without = right.replace(`**${m.forwardingIdle}**`, m.forwardingIdle);
  expect(without).not.toEqual(right); // the plant took effect
  expect(missingNames(without)).toEqual([m.forwardingIdle]);
  expect(missingNames("")).toEqual(NAMED);
  // built from parts, so that the repository's own reference check does not take it for a link
  const nope = ["service-settings.md", "nope"].join("#");
  const linked = [...helpPages(), ["See [it](", nope, ")."].join("")];
  expect(linked.at(-1)).toContain(nope); // the plant took effect
  expect(brokenLinks(linked, readFileSync(GUIDE, "utf-8"))).toEqual(["nope"]);
});
