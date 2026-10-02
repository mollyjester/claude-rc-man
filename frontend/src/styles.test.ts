import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const STYLES = readFileSync(resolve(__dirname, "styles.css"), "utf-8");

// Colours that stay the same in both schemes, and why.
const SAME_IN_DARK: Record<string, string> = {
  "--screen-bg": "a server's screen is shown as a terminal shows it, dark in both",
  "--screen-fg": "a server's screen is shown as a terminal shows it, dark in both",
};

const COLOUR = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/;

const uncommented = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

// Every declaration in the stylesheet: what ends in ; or }.
function declarations(css: string): string[] {
  return [...uncommented(css).matchAll(/([^;{}]*)([;{}])/g)]
    .filter((match) => match[2] !== "{")
    .map((match) => match[1].trim())
    .filter((declaration) => declaration.includes(":"));
}

// The custom properties a block declares.
function properties(block: string | undefined): string[] {
  return [...(block ?? "").matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]);
}

// The custom properties of the first :root block, and of the one for a dark scheme.
const light = (css: string) => properties(uncommented(css).match(/(?:^|\})\s*:root\s*\{([^}]*)\}/)?.[1]);
const dark = (css: string) =>
  properties(uncommented(css).match(/prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/)?.[1]);

// What breaks the rule: colours written outside a custom property, light
// colours with no dark counterpart, and properties used but never declared.
function violations(css: string): string[] {
  const found: string[] = [];
  for (const declaration of declarations(css)) {
    if (COLOUR.test(declaration) && !declaration.startsWith("--")) found.push(`colour outside a variable: ${declaration}`);
  }
  const darkSet = new Set(dark(css));
  for (const name of light(css)) {
    const colour = declarations(css).some((d) => d.startsWith(`${name}:`) && COLOUR.test(d));
    if (colour && !darkSet.has(name) && !(name in SAME_IN_DARK)) found.push(`no dark colour: ${name}`);
  }
  const declared = new Set([...light(css), ...darkSet]);
  for (const [, name] of css.matchAll(/var\((--[\w-]+)\)/g)) {
    if (!declared.has(name)) found.push(`not declared: ${name}`);
  }
  return found;
}

test("colours are written only as custom properties, each with a dark counterpart", () => {
  expect(light(STYLES).length).toBeGreaterThan(0);
  expect(dark(STYLES).length).toBeGreaterThan(0);
  expect(violations(STYLES)).toEqual([]);
});

test("every colour kept the same in dark is still declared", () => {
  for (const name of Object.keys(SAME_IN_DARK)) expect(light(STYLES)).toContain(name);
});

test("the check fails on a colour outside a variable, a missing dark colour, or an undeclared one", () => {
  const literal = `${STYLES}\n.planted { color: #123456; }`;
  expect(literal).toContain("#123456");
  expect(violations(literal)).toEqual(["colour outside a variable: color: #123456"]);

  const withoutDark = STYLES.replace(/(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{[^}]*?)--fg:[^;]*;/, "$1");
  expect(dark(withoutDark)).not.toContain("--fg");
  expect(violations(withoutDark)).toEqual(["no dark colour: --fg"]);

  const undeclared = `${STYLES}\n.planted { color: var(--nowhere); }`;
  expect(violations(undeclared)).toEqual(["not declared: --nowhere"]);

  expect(light("")).toEqual([]);
});
