import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const STYLES = readFileSync(resolve(__dirname, "styles.css"), "utf-8");

// Colours that stay the same in both schemes, and why.
const SAME_IN_DARK: Record<string, string> = {
  "--screen-bg": "a server's screen is shown as a terminal shows it, dark in both",
  "--screen-fg": "a server's screen is shown as a terminal shows it, dark in both",
};

const COLOUR = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/;
// Keywords a colour may be written as that name no colour of the stylesheet's own.
const NOT_A_COLOUR = new Set(["transparent", "currentcolor", "inherit", "initial", "unset", "revert", "revert-layer", "none"]);
// The properties whose value can be a colour by name, and custom properties. The
// engine reads words such as background and menu as system colours, so a word
// counts only where a colour is meant.
const TAKES_A_COLOUR =
  /^(?:--[\w-]+|color|background(?:-[\w-]+)?|border(?:-[\w-]+)?|outline(?:-[\w-]+)?|box-shadow|text-shadow|fill|stroke|caret-color|accent-color|text-decoration(?:-color)?|column-rule(?:-color)?)$/;

// Whether a word is a colour, as the browser's own colour parser reads it:
// the named colours come from the engine, not from a list kept here.
function isColourName(word: string) {
  if (NOT_A_COLOUR.has(word.toLowerCase())) return false;
  const style = document.createElement("div").style;
  style.color = word;
  return style.color !== "";
}

// The named colours a declaration's value uses, var() fallbacks included.
function colourNames(declaration: string): string[] {
  const property = declaration.slice(0, declaration.indexOf(":")).trim();
  if (!TAKES_A_COLOUR.test(property)) return [];
  const value = declaration
    .slice(declaration.indexOf(":") + 1)
    .replace(/--[\w-]+/g, "")
    .replace(/"[^"]*"|'[^']*'/g, "")
    .replace(/[\w-]+\(/g, "(");
  return [...value.matchAll(/[a-zA-Z]+(?:-[a-zA-Z]+)*/g)].map((w) => w[0]).filter(isColourName);
}

const hasColour = (declaration: string) => COLOUR.test(declaration) || colourNames(declaration).length > 0;

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
    if (hasColour(declaration) && !declaration.startsWith("--")) found.push(`colour outside a variable: ${declaration}`);
  }
  const darkSet = new Set(dark(css));
  for (const name of light(css)) {
    const colour = declarations(css).some((d) => d.startsWith(`${name}:`) && hasColour(d));
    if (colour && !darkSet.has(name) && !(name in SAME_IN_DARK)) found.push(`no dark colour: ${name}`);
  }
  const declared = new Set([...light(css), ...darkSet]);
  for (const [, name] of css.matchAll(/var\(\s*(--[\w-]+)/g)) {
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
  const undeclaredWithFallback = `${STYLES}\n.planted { color: var(--nowhere, var(--fg)); }`;
  expect(violations(undeclaredWithFallback)).toEqual(["not declared: --nowhere"]);

  expect(light("")).toEqual([]);
});

test("named colours count as colours, wherever they are written", () => {
  // positive and negative controls for the engine's colour parser
  expect(isColourName("gray")).toBe(true);
  expect(isColourName("solid")).toBe(false);
  expect(isColourName("transparent")).toBe(false);

  const named = `${STYLES}\n.planted { color: gray; }`;
  expect(violations(named)).toEqual(["colour outside a variable: color: gray"]);

  const fallback = `${STYLES}\n.planted { color: var(--muted, gray); }`;
  expect(violations(fallback)).toEqual(["colour outside a variable: color: var(--muted, gray)"]);

  const inDark = STYLES.replace(/(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/, "$1\n    color: gray;");
  expect(inDark).not.toEqual(STYLES); // the plant took effect
  expect(violations(inDark)).toEqual(["colour outside a variable: color: gray"]);

  const lightOnly = STYLES.replace(/(:root\s*\{)/, "$1\n  --planted: gray;");
  expect(violations(lightOnly)).toEqual(["no dark colour: --planted"]);
});

test("a colour's name in a property that takes no colour is not a colour", () => {
  // positive control: the engine reads these words as colours where one is meant
  for (const word of ["background", "menu", "highlight"]) expect(isColourName(word)).toBe(true);
  expect(isColourName("revert-layer")).toBe(false);

  for (const planted of [".planted { transition: background 0.2s; }", ".planted { grid-area: menu; }", ".planted { animation-name: highlight; }"]) {
    expect(violations(`${STYLES}\n${planted}`)).toEqual([]);
  }
  expect(violations(`${STYLES}\n.planted { background: gray; }`)).toEqual(["colour outside a variable: background: gray"]);
  expect(violations(`${STYLES}\n.planted { border-left: 1px solid gray; }`)).toEqual([
    "colour outside a variable: border-left: 1px solid gray",
  ]);
});
