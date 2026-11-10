import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";

const GUIDE = resolve(__dirname, "../../docs/users.md");

// The names the guide must give in bold, as the page shows them: where the page is, and the
// controls and columns it describes.
const NAMED = [
  m.navAdmin,
  m.usersTitle,
  m.usersAddAdmin,
  m.usersRemove,
  m.usersMemory,
  m.usersCpu,
  m.usersProcesses,
  m.usersLimits,
  m.usersMoreActions,
  m.save,
  m.usersNoLimit,
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

// The words about limits that say something in numbers, which would go stale: the server's
// refusals give the minimums.
const LIMITS_WORDS = [
  ...Object.values(m.limitsHints),
  m.limitsHeldToPerson,
  m.limitsMemoryHard,
  m.limitsBelowUse,
  m.limitsNotLimited,
];
const numbered = (words: string[]) => words.filter((text) => /[0-9]/.test(text));

test("every name the people guide gives in bold is text the interface shows, it names the page, its columns and the controls that add, remove and limit, and the words about limits say nothing in numbers", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(names(guide).length).toBeGreaterThan(0); // positive control: the guide was read
  expect(unknownNames(guide)).toEqual([]);
  expect(missingNames(guide)).toEqual([]);
  expect(LIMITS_WORDS.every((text) => text.length > 0)).toBe(true);
  expect(numbered(LIMITS_WORDS)).toEqual([]);
});

test("the checks fail on a guide that names something the page does not show, or leaves out a name, and on words about limits in numbers", () => {
  const planted = "On **People**, press **Frobnicate**.";
  expect(names(planted)).toEqual([m.usersTitle, "Frobnicate"]);
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);
  expect(unknownNames("On **People**, press **Remove**.")).toEqual([]);
  expect(missingNames("**People**")).toEqual(NAMED.filter((name) => name !== m.usersTitle));
  expect(missingNames(NAMED.map((name) => `**${name}**`).join(" "))).toEqual([]);
  expect(numbered([...LIMITS_WORDS, "Planted: at least 6 MB."])).toEqual([
    "Planted: at least 6 MB.",
  ]);
});
