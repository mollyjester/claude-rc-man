import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { m } from "./messages";
import { missingNames as missing, names, unknownNames } from "./test/guideNames";

const GUIDE = resolve(__dirname, "../../docs/monitor.md");

// The names the guide must give in bold, as the page shows them: where the page is, its parts,
// its two rounds, and the page its settings are on.
const NAMED = [
  m.navAdmin,
  m.navMonitor,
  m.monitorHost,
  m.monitorPeople,
  m.monitorContainers,
  m.monitorUsage,
  m.monitorDisk,
  m.monitorNotKnown,
  m.monitorOff,
  m.navServiceSettings,
];
const missingNames = (markdown: string) => missing(NAMED, markdown);

test("every name the Monitor guide gives in bold is text the page shows", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(names(guide).length).toBeGreaterThan(0); // positive control: the guide was read
  expect(unknownNames(guide)).toEqual([]);
});

test("the guide names the page, its parts, its rounds and where its settings are", () => {
  expect(missingNames(readFileSync(GUIDE, "utf-8"))).toEqual([]);
});

test("the checks fail on a name the page does not have, and on a guide that leaves one out", () => {
  const right = NAMED.map((name) => `Choose **${name}**.`).join("\n");
  expect(unknownNames(right)).toEqual([]);
  expect(missingNames(right)).toEqual([]);

  const planted = `${right}\nChoose **Frobnicate**.`;
  expect(names(planted)).toContain("Frobnicate"); // the plant took effect
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);

  const without = right.replace(`**${m.monitorDisk}**`, m.monitorDisk);
  expect(without).not.toEqual(right); // the plant took effect
  expect(missingNames(without)).toEqual([m.monitorDisk]);
  expect(missingNames("")).toEqual(NAMED);
});
