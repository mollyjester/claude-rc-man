import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { uncommented } from "./test/css";

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

// --- how buttons look ----------------------------------------------------------------

type Scheme = "light" | "dark";

// The custom properties of a scheme, by name: dark's over light's.
function variables(css: string, scheme: Scheme): Map<string, string> {
  const block = (re: RegExp) => uncommented(css).match(re)?.[1] ?? "";
  const read = (text: string) => [...text.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()] as const);
  const found = new Map(read(block(/(?:^|\})\s*:root\s*\{([^}]*)\}/)));
  if (scheme === "dark") for (const [name, value] of read(block(/prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/))) found.set(name, value);
  return found;
}

// A value with every var() replaced by what it holds in the scheme.
function resolved(value: string, vars: Map<string, string>, depth = 0): string {
  if (depth > 10) return value;
  return value.replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name: string) => resolved(vars.get(name) ?? `unset ${name}`, vars, depth + 1));
}

// The declarations of the rules whose selector list holds exactly this selector.
function rule(css: string, selector: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const [, selectors, body] of uncommented(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!selectors.split(",").some((s) => s.trim() === selector)) continue;
    for (const [, property, value] of body.matchAll(/([\w-]+)\s*:\s*([^;]+);?/g)) found.set(property, value.trim());
  }
  return found;
}

// How a button with the given class looks in a scheme: the plain button's rule, then the class's.
function looks(css: string, selector: string, scheme: Scheme) {
  const vars = variables(css, scheme);
  const declared = new Map([...rule(css, "button"), ...rule(css, selector)]);
  const value = (property: string) => resolved(declared.get(property) ?? "", vars);
  return { own: rule(css, selector).size, background: value("background"), color: value("color"), border: value("border-color") };
}

// What is wrong with how the destructive buttons look: a danger button not
// unlike a primary or a plain one, or a danger outline not unlike a secondary one.
function buttonFaults(css: string): string[] {
  const found: string[] = [];
  for (const scheme of ["light", "dark"] as const) {
    const plain = looks(css, "button", scheme);
    const primary = looks(css, "button.primary", scheme);
    const danger = looks(css, "button.danger", scheme);
    const secondary = looks(css, "button.secondary", scheme);
    const outline = looks(css, "button.secondary-danger", scheme);
    if (!danger.own || danger.background === primary.background || danger.background === plain.background)
      found.push(`${scheme}: a danger button looks like another`);
    if (!outline.own || outline.color === secondary.color || outline.color === plain.color || outline.border !== outline.color)
      found.push(`${scheme}: a danger outline looks like a secondary button`);
    if (outline.color !== danger.background) found.push(`${scheme}: a danger outline is not in the danger colour`);
  }
  return found;
}

test("danger buttons look like no other button, in both schemes", () => {
  expect(looks(STYLES, "button.primary", "dark").background).toMatch(/^#/); // positive control: the reader resolves
  expect(buttonFaults(STYLES)).toEqual([]);
});

test("the check fails on a danger button or a danger outline that is not styled", () => {
  const noDanger = STYLES.replace(/button\.danger\s*\{[^}]*\}/, "");
  expect(noDanger).not.toEqual(STYLES);
  expect(buttonFaults(noDanger)).toContain("light: a danger button looks like another");
  const noOutline = STYLES.replace(/button\.secondary-danger\s*\{[^}]*\}/, "");
  expect(noOutline).not.toEqual(STYLES);
  expect(buttonFaults(noOutline)).toContain("dark: a danger outline looks like a secondary button");
  const likePrimary = STYLES.replace(/(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/, "$1\n    --danger: var(--accent);");
  expect(likePrimary).not.toEqual(STYLES);
  expect(buttonFaults(likePrimary)).toEqual(["dark: a danger button looks like another"]);
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

// A rule's declarations in the stylesheet, by its exact selector.
function ruleOf(selector: string): string {
  const found = [...uncommented(STYLES).matchAll(/([^{}]+)\{([^}]*)\}/g)].find(([, sel]) => sel.trim() === selector);
  return found ? found[2] : "";
}

test("what git said wraps, where a server's screen scrolls", () => {
  expect(ruleOf(".screen")).toMatch(/overflow-x:\s*auto/); // positive control
  expect(ruleOf(".git-said")).toMatch(/white-space:\s*pre-wrap/);
});

test("a commit message's box stands apart from its label", () => {
  expect(ruleOf(".panel-section label > textarea")).toMatch(/margin-top:\s*[0-9.]*[1-9]/);
});

// The value of each custom property in a scheme: the light block's, with the dark
// block's over it for a dark scheme.
function schemeValues(css: string, scheme: "light" | "dark"): Record<string, string> {
  const block = (pattern: RegExp) => uncommented(css).match(pattern)?.[1] ?? "";
  const values = (text: string) => Object.fromEntries([...text.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
  const lightValues = values(block(/(?:^|\})\s*:root\s*\{([^}]*)\}/));
  return scheme === "light" ? lightValues : { ...lightValues, ...values(block(/prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/)) };
}

// Blocks drawn on a background the page's own colour in a scheme, with no border
// to show where they are: they cannot be told from the page.
function unseenBlocks(css: string): string[] {
  const found: string[] = [];
  for (const [, selector, body] of uncommented(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const name = selector.trim();
    if (name === "body" || name === ":root") continue;
    const background = body.match(/background(?:-color)?\s*:\s*var\(\s*(--[\w-]+)\s*\)/)?.[1];
    if (!background || /(?:^|;)\s*border(?:-width)?\s*:\s*[^;]*\b(?:[1-9]\d*(?:\.\d+)?|0?\.\d*[1-9])(?:px|rem|em)\b/.test(body)) continue;
    for (const scheme of ["light", "dark"] as const) {
      const values = schemeValues(css, scheme);
      if (values[background] === values["--bg"]) found.push(`${name}: the page's own background in ${scheme}`);
    }
  }
  return found;
}

test("every block with a background of its own can be told from the page, in both schemes", () => {
  expect(schemeValues(STYLES, "dark")["--bg"]).not.toBe(schemeValues(STYLES, "light")["--bg"]); // positive control
  expect(unseenBlocks(STYLES)).toEqual([]);
});

test("the check finds a block on the page's own background with no border, and lets a border through", () => {
  const darkBg = schemeValues(STYLES, "dark")["--bg"];
  const inBlocks = STYLES.replace("--bg: #ffffff;", "--bg: #ffffff;\n  --planted-bg: #abcdef;").replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/,
    `$1\n    --planted-bg: ${darkBg};`,
  );
  expect(schemeValues(inBlocks, "dark")["--planted-bg"]).toBe(darkBg); // the plant took effect
  expect(unseenBlocks(`${inBlocks}\n.planted { background: var(--planted-bg); }`)).toEqual([".planted: the page's own background in dark"]);
  expect(unseenBlocks(`${inBlocks}\n.planted { background: var(--planted-bg); border: 1px solid var(--border); }`)).toEqual([]);
});

test("a password field is laid out as a text field is", () => {
  const style = document.createElement("style");
  style.textContent = uncommented(STYLES);
  document.head.append(style);
  const form = document.createElement("form");
  form.innerHTML = '<input type="text"><input type="password">';
  document.body.append(form);
  try {
    const [text, password] = [...form.querySelectorAll("input")].map((input) => getComputedStyle(input));
    expect(text.display).toBe("block"); // positive control: the stylesheet applies
    expect([password.display, password.minWidth, password.maxWidth]).toEqual([text.display, text.minWidth, text.maxWidth]);
  } finally {
    style.remove();
    form.remove();
  }
});
