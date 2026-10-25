import { m } from "../messages";

// Every piece of text in the message catalogue that is not worked out from values.
export function catalogue(value: unknown = m): string[] {
  if (typeof value === "string") return [value];
  if (value && typeof value === "object") return Object.values(value).flatMap((v) => catalogue(v));
  return [];
}

// Each name in bold in a guide, its line breaks as spaces.
export const names = (markdown: string) =>
  [...markdown.matchAll(/\*\*([^*]+)\*\*/g)].map((b) => b[1].replace(/\s+/g, " "));

// The bold names a guide uses that the page does not show.
export function unknownNames(markdown: string): string[] {
  const texts = new Set(catalogue());
  return names(markdown).filter((name) => !texts.has(name));
}

// The names a guide should give in bold and does not.
export const missingNames = (named: string[], markdown: string) =>
  named.filter((name) => !names(markdown).includes(name));

// A heading's anchor as a help page's link names it: lower case, words joined by hyphens.
export const anchors = (markdown: string) =>
  [...markdown.matchAll(/^#+ (.+)$/gm)].map((h) =>
    h[1]
      .toLowerCase()
      .replace(/[^a-z0-9 -]/g, "")
      .replace(/ /g, "-"),
  );
