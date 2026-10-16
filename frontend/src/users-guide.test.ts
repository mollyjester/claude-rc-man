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

test("every name the people guide gives in bold is text the interface shows", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(names(guide).length).toBeGreaterThan(0); // positive control: the guide was read
  expect(unknownNames(guide)).toEqual([]);
});

test("the guide names the page, its columns and the controls that add, remove and limit", () => {
  expect(missingNames(readFileSync(GUIDE, "utf-8"))).toEqual([]);
});

test("the checks fail on a guide that names something the page does not show, or leaves out a name", () => {
  const planted = "On **People**, press **Frobnicate**.";
  expect(names(planted)).toEqual([m.usersTitle, "Frobnicate"]);
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);
  expect(unknownNames("On **People**, press **Remove**.")).toEqual([]);
  expect(missingNames("**People**")).toEqual(NAMED.filter((name) => name !== m.usersTitle));
  expect(missingNames(NAMED.map((name) => `**${name}**`).join(" "))).toEqual([]);
});

test("the words about limits say nothing in numbers, which would go stale: the server's refusals give the minimums", () => {
  const words = [
    ...Object.values(m.limitsHints),
    m.limitsHeldToPerson,
    m.limitsMemoryHard,
    m.limitsBelowUse,
    m.limitsNotLimited,
  ];
  expect(words.every((text) => text.length > 0)).toBe(true);
  for (const text of words) expect(text).not.toMatch(/[0-9]/);
});
