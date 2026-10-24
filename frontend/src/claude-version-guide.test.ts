import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";

const GUIDE = resolve(__dirname, "../../docs/claude-version.md");

// The names the guide must give in bold, as the page shows them: where the page is, and the
// controls that roll a version out and that mark a version as failed.
const NAMED = [
  m.navAdmin,
  m.navVersions,
  m.versionsUpgradeGo,
  m.versionsUse,
  m.versionsRollout,
  m.versionsFailedTitle,
  m.versionsMarkFailed,
  m.versionsMarkAnyFailed,
  m.versionsClearFailed,
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

test("every name the Claude Code versions guide gives in bold is text the interface shows", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(names(guide).length).toBeGreaterThan(0); // positive control: the guide was read
  expect(unknownNames(guide)).toEqual([]);
});

test("the guide names the page and the controls that roll a version out and mark one as failed", () => {
  expect(missingNames(readFileSync(GUIDE, "utf-8"))).toEqual([]);
});

test("the checks fail on a name the page does not have, and on a guide that leaves a control out", () => {
  const right = NAMED.map((name) => `Choose **${name}**.`).join("\n");
  expect(unknownNames(right)).toEqual([]);
  expect(missingNames(right)).toEqual([]);

  const planted = `${right}\nChoose **Frobnicate**.`;
  expect(names(planted)).toContain("Frobnicate"); // the plant took effect
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);

  const without = right.replace(`**${m.versionsUse}**`, m.versionsUse);
  expect(without).not.toEqual(right); // the plant took effect
  expect(missingNames(without)).toEqual([m.versionsUse]);
  expect(missingNames("")).toEqual(NAMED);
});
