import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";
import { THEME } from "./terminalTheme";
import { uncommented } from "./test/css";

const STYLES = readFileSync(resolve(__dirname, "styles.css"), "utf-8");

// Colours that stay the same in both schemes, and why.
const SAME_IN_DARK: Record<string, string> = {
  "--screen-bg": "a server's screen is shown as a terminal shows it, dark in both",
  "--screen-fg": "a server's screen is shown as a terminal shows it, dark in both",
};

const COLOUR = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/;
// Keywords a colour may be written as that name no colour of the stylesheet's own.
const NOT_A_COLOUR = new Set([
  "transparent",
  "currentcolor",
  "inherit",
  "initial",
  "unset",
  "revert",
  "revert-layer",
  "none",
]);
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

const hasColour = (declaration: string) =>
  COLOUR.test(declaration) || colourNames(declaration).length > 0;

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
const light = (css: string) =>
  properties(uncommented(css).match(/(?:^|\})\s*:root\s*\{([^}]*)\}/)?.[1]);
const dark = (css: string) =>
  properties(
    uncommented(css).match(/prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/)?.[1],
  );

// What breaks the rule: colours written outside a custom property, light
// colours with no dark counterpart, and properties used but never declared.
function violations(css: string): string[] {
  const found: string[] = [];
  for (const declaration of declarations(css)) {
    if (hasColour(declaration) && !declaration.startsWith("--"))
      found.push(`colour outside a variable: ${declaration}`);
  }
  const darkSet = new Set(dark(css));
  for (const name of light(css)) {
    const colour = declarations(css).some((d) => d.startsWith(`${name}:`) && hasColour(d));
    if (colour && !darkSet.has(name) && !(name in SAME_IN_DARK))
      found.push(`no dark colour: ${name}`);
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

// The terminal's colours that xterm.js reads (src/terminalTheme.ts) missing from a scheme.
function terminalColoursMissing(css: string): string[] {
  const lightSet = new Set(light(css));
  const darkSet = new Set(dark(css));
  return Object.values(THEME).flatMap((name) => [
    ...(lightSet.has(name) ? [] : [`not in light: ${name}`]),
    ...(darkSet.has(name) ? [] : [`not in dark: ${name}`]),
  ]);
}

test("every colour the terminal reads is a custom property with a light and a dark value", () => {
  expect(Object.values(THEME).length).toBeGreaterThan(0);
  expect(terminalColoursMissing(STYLES)).toEqual([]);
});

test("the terminal's colour check fails on one missing from a scheme", () => {
  const withoutDark = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{[^}]*?)--terminal-bg:[^;]*;/,
    "$1",
  );
  expect(withoutDark).not.toEqual(STYLES); // the plant took effect
  expect(terminalColoursMissing(withoutDark)).toEqual(["not in dark: --terminal-bg"]);
  expect(terminalColoursMissing("")).toHaveLength(2 * Object.values(THEME).length);
});

test("every colour kept the same in dark is still declared", () => {
  for (const name of Object.keys(SAME_IN_DARK)) expect(light(STYLES)).toContain(name);
});

test("the check fails on a colour outside a variable, a missing dark colour, or an undeclared one", () => {
  const literal = `${STYLES}\n.planted { color: #123456; }`;
  expect(literal).toContain("#123456");
  expect(violations(literal)).toEqual(["colour outside a variable: color: #123456"]);

  const withoutDark = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{[^}]*?)--fg:[^;]*;/,
    "$1",
  );
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

  const inDark = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/,
    "$1\n    color: gray;",
  );
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
  const read = (text: string) =>
    [...text.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()] as const);
  const found = new Map(read(block(/(?:^|\})\s*:root\s*\{([^}]*)\}/)));
  if (scheme === "dark")
    for (const [name, value] of read(
      block(/prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/),
    ))
      found.set(name, value);
  return found;
}

// A value with every var() replaced by what it holds in the scheme.
function resolved(value: string, vars: Map<string, string>, depth = 0): string {
  if (depth > 10) return value;
  return value.replace(/var\(\s*(--[\w-]+)\s*\)/g, (_, name: string) =>
    resolved(vars.get(name) ?? `unset ${name}`, vars, depth + 1),
  );
}

// The declarations of the rules whose selector list holds exactly this selector.
function rule(css: string, selector: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const [, selectors, body] of uncommented(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!selectors.split(",").some((s) => s.trim() === selector)) continue;
    for (const [, property, value] of body.matchAll(/([\w-]+)\s*:\s*([^;]+);?/g))
      found.set(property, value.trim());
  }
  return found;
}

// How a button with the given class looks in a scheme: the plain button's rule, then the class's.
function looks(css: string, selector: string, scheme: Scheme) {
  const vars = variables(css, scheme);
  const declared = new Map([...rule(css, "button"), ...rule(css, selector)]);
  const value = (property: string) => resolved(declared.get(property) ?? "", vars);
  return {
    own: rule(css, selector).size,
    background: value("background"),
    color: value("color"),
    border: value("border-color"),
  };
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
    if (
      !danger.own ||
      danger.background === primary.background ||
      danger.background === plain.background
    )
      found.push(`${scheme}: a danger button looks like another`);
    if (
      !outline.own ||
      outline.color === secondary.color ||
      outline.color === plain.color ||
      outline.border !== outline.color
    )
      found.push(`${scheme}: a danger outline looks like a secondary button`);
    if (outline.color !== danger.background)
      found.push(`${scheme}: a danger outline is not in the danger colour`);
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
  const likePrimary = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/,
    "$1\n    --danger: var(--accent);",
  );
  expect(likePrimary).not.toEqual(STYLES);
  expect(buttonFaults(likePrimary)).toEqual(["dark: a danger button looks like another"]);
});

test("a colour's name in a property that takes no colour is not a colour", () => {
  // positive control: the engine reads these words as colours where one is meant
  for (const word of ["background", "menu", "highlight"]) expect(isColourName(word)).toBe(true);
  expect(isColourName("revert-layer")).toBe(false);

  for (const planted of [
    ".planted { transition: background 0.2s; }",
    ".planted { grid-area: menu; }",
    ".planted { animation-name: highlight; }",
  ]) {
    expect(violations(`${STYLES}\n${planted}`)).toEqual([]);
  }
  expect(violations(`${STYLES}\n.planted { background: gray; }`)).toEqual([
    "colour outside a variable: background: gray",
  ]);
  expect(violations(`${STYLES}\n.planted { border-left: 1px solid gray; }`)).toEqual([
    "colour outside a variable: border-left: 1px solid gray",
  ]);
});

// A rule's declarations in the stylesheet, by its exact selector list, read as CSS reads it:
// any run of white space, a line break included, is one space.
function ruleOf(selector: string, css = STYLES): string {
  const found = [...uncommented(css).matchAll(/([^{}]+)\{([^}]*)\}/g)].find(
    ([, sel]) => sel.trim().replace(/\s+/g, " ") === selector,
  );
  return found ? found[2] : "";
}

test("a rule is found by its selector list wherever the list breaks its lines", () => {
  expect(ruleOf("a, b", "a, b { color: red; }")).toMatch(/color/); // positive control
  expect(ruleOf("a, b", "a,\nb { color: red; }")).toMatch(/color/);
  expect(ruleOf("a, b", "a,\n  b\n{ color: red; }")).toMatch(/color/);
});

test("what git said, why a workspace could not be made, and the steps made, wrap, where a server's screen scrolls", () => {
  expect(ruleOf(".screen")).toMatch(/overflow-x:\s*auto/); // positive control
  expect(ruleOf(".git-said")).toMatch(/white-space:\s*pre-wrap/);
  expect(ruleOf(".workspace-error")).toMatch(/white-space:\s*pre-wrap/);
  expect(ruleOf(".workspace-log")).toMatch(/white-space:\s*pre-wrap/);
});

test("a repository's page under a workspace's name wraps anywhere, so a long one never widens the panel", () => {
  expect(ruleOf(".workspace-log")).toMatch(/overflow-wrap:\s*anywhere/); // positive control
  expect(ruleOf(".source-page")).toMatch(/overflow-wrap:\s*anywhere/);
});

test("a commit message's box stands apart from its label", () => {
  expect(ruleOf(".panel-section label > textarea")).toMatch(/margin-top:\s*[0-9.]*[1-9]/);
});

// The value of each custom property in a scheme: the light block's, with the dark
// block's over it for a dark scheme.
function schemeValues(css: string, scheme: "light" | "dark"): Record<string, string> {
  const block = (pattern: RegExp) => uncommented(css).match(pattern)?.[1] ?? "";
  const values = (text: string) =>
    Object.fromEntries(
      [...text.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
    );
  const lightValues = values(block(/(?:^|\})\s*:root\s*\{([^}]*)\}/));
  return scheme === "light"
    ? lightValues
    : {
        ...lightValues,
        ...values(block(/prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/)),
      };
}

// Blocks drawn on a background the page's own colour in a scheme, with no border
// to show where they are: they cannot be told from the page.
function unseenBlocks(css: string): string[] {
  const found: string[] = [];
  for (const [, selector, body] of uncommented(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const name = selector.trim();
    if (name === "body" || name === ":root") continue;
    const background = body.match(/background(?:-color)?\s*:\s*var\(\s*(--[\w-]+)\s*\)/)?.[1];
    if (
      !background ||
      /(?:^|;)\s*border(?:-width)?\s*:\s*[^;]*\b(?:[1-9]\d*(?:\.\d+)?|0?\.\d*[1-9])(?:px|rem|em)\b/.test(
        body,
      )
    )
      continue;
    for (const scheme of ["light", "dark"] as const) {
      const values = schemeValues(css, scheme);
      if (values[background] === values["--bg"])
        found.push(`${name}: the page's own background in ${scheme}`);
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
  const inBlocks = STYLES.replace(
    "--bg: #ffffff;",
    "--bg: #ffffff;\n  --planted-bg: #abcdef;",
  ).replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/,
    `$1\n    --planted-bg: ${darkBg};`,
  );
  expect(schemeValues(inBlocks, "dark")["--planted-bg"]).toBe(darkBg); // the plant took effect
  expect(unseenBlocks(`${inBlocks}\n.planted { background: var(--planted-bg); }`)).toEqual([
    ".planted: the page's own background in dark",
  ]);
  expect(
    unseenBlocks(
      `${inBlocks}\n.planted { background: var(--planted-bg); border: 1px solid var(--border); }`,
    ),
  ).toEqual([]);
});

test("a password field is laid out as a text field is", () => {
  const style = document.createElement("style");
  style.textContent = uncommented(STYLES);
  document.head.append(style);
  const form = document.createElement("form");
  form.innerHTML = '<input type="text"><input type="password">';
  document.body.append(form);
  try {
    const [text, password] = [...form.querySelectorAll("input")].map((input) =>
      getComputedStyle(input),
    );
    expect(text.display).toBe("block"); // positive control: the stylesheet applies
    expect([password.display, password.minWidth, password.maxWidth]).toEqual([
      text.display,
      text.minWidth,
      text.maxWidth,
    ]);
  } finally {
    style.remove();
    form.remove();
  }
});

test("a form's text field takes the form's font, and its width, up to a limit", () => {
  const style = document.createElement("style");
  style.textContent = uncommented(STYLES);
  document.head.append(style);
  const form = document.createElement("form");
  form.innerHTML = '<label>Address<input type="text" name="url"></label>';
  document.body.append(form);
  try {
    const field = getComputedStyle(form.querySelector("input")!);
    expect(field.display).toBe("block"); // positive control: the stylesheet applies
    // jsdom gives a field the font around it, as a browser does not: the rule says so itself
    expect(ruleOf('form input[type="text"], form input[type="password"]')).toMatch(
      /(?:^|;)\s*font-family:\s*inherit/,
    );
    expect([field.width, field.boxSizing]).toEqual(["100%", "border-box"]);
    expect(field.maxWidth).toMatch(/^\d+(?:\.\d+)?(?:px|rem)$/); // a length, not the whole width
  } finally {
    style.remove();
    form.remove();
  }
});

// What is wrong with the colours of the changes' letters: a change whose letter
// has no rule of its own, or one whose colour is not a custom property that
// holds a colour in both schemes.
function letterFaults(css: string): string[] {
  const found: string[] = [];
  for (const code of Object.keys(m.changeLetters)) {
    const colour = ruleOf(`.change-${code}`, css)
      .match(/(?:^|;)\s*color:\s*([^;]+)/)?.[1]
      .trim();
    if (!colour) {
      found.push(`${code}: no colour`);
      continue;
    }
    if (!/^var\(\s*--[\w-]+\s*\)$/.test(colour)) found.push(`${code}: not a custom property`);
    for (const scheme of ["light", "dark"] as const) {
      if (!/^#[0-9a-fA-F]{3,8}$/.test(resolved(colour, variables(css, scheme))))
        found.push(`${code}: no colour in ${scheme}`);
    }
  }
  return found;
}

test("each change's letter is drawn in a colour of its own rule, in both schemes", () => {
  expect(Object.keys(m.changeLetters).length).toBeGreaterThan(0); // non-vacuity
  expect(letterFaults(STYLES)).toEqual([]);
});

test("the letter check fails on a change with no rule, or a colour outside a custom property", () => {
  const code = Object.keys(m.changeLetters)[0];
  const withoutRule = STYLES.replace(new RegExp(`\\.change-${code}\\s*\\{[^}]*\\}`), "");
  expect(withoutRule).not.toEqual(STYLES); // the plant took effect
  expect(letterFaults(withoutRule)).toEqual([`${code}: no colour`]);
  const literal = `${withoutRule}\n.change-${code} { color: inherit; }`;
  expect(letterFaults(literal)).toEqual([
    `${code}: not a custom property`,
    `${code}: no colour in light`,
    `${code}: no colour in dark`,
  ]);
  const undeclared = `${withoutRule}\n.change-${code} { color: var(--nowhere); }`;
  expect(letterFaults(undeclared)).toEqual([
    `${code}: no colour in light`,
    `${code}: no colour in dark`,
  ]);
});

// What a media query holds, found by matching its braces: its inner stylesheet, or "".
function mediaOf(query: string, css = STYLES): string {
  const text = uncommented(css);
  const start = text.indexOf(`@media ${query}`);
  if (start < 0) return "";
  const open = text.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    if (text[i] === "}" && --depth === 0) return text.slice(open + 1, i);
  }
  return "";
}

// The width below which the page lays itself out for a phone.
const PHONE = "(max-width: 48rem)";

test("the navigation wraps, and a table, a help page and a wide word never widen the page", () => {
  expect(ruleOf("header nav")).toMatch(/flex-wrap:\s*wrap/);
  expect(ruleOf(".table-scroll")).toMatch(/overflow-x:\s*auto/);
  expect(ruleOf(".table-scroll")).toMatch(/max-width:\s*100%/);
  // help: one column on a phone, the text in a box that may shrink
  expect(ruleOf(".docs article")).toMatch(/min-width:\s*0/);
  expect(ruleOf(".docs", mediaOf(PHONE))).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  expect(ruleOf(".docs pre")).toMatch(/overflow-x:\s*auto/);
});

test("the check on a media query reads one, and finds none that is not there", () => {
  expect(mediaOf(PHONE)).toContain(".dashboard"); // positive control: the phone layout is read
  expect(mediaOf("(max-width: 1px)")).toBe("");
});

test("on a phone the list and the selected workspace's panel take turns, with the way back over the panel", () => {
  const phone = mediaOf(PHONE);
  expect(ruleOf(".dashboard.showing > .list", phone)).toMatch(/display:\s*none/);
  expect(ruleOf(".dashboard:not(.showing) > .panel-side", phone)).toMatch(/display:\s*none/);
  expect(ruleOf(".panel-bar", phone)).toMatch(/display:\s*flex/);
  // beside each other, the list is there, so the way back is not
  expect(ruleOf(".panel-bar")).toMatch(/display:\s*none/);
});

test("the terminal shows where focus is, though xterm.js takes the outline off its own element", () => {
  expect(ruleOf(".xterm.focus, .xterm:focus")).toMatch(/outline:\s*none/); // positive: why
  expect(ruleOf(".terminal-screen:focus-within")).toMatch(/outline:\s*2px solid var\(--focus\)/);
});

test("a skip link is out of sight until it has focus, then shown", () => {
  expect(ruleOf(".skip-link")).toMatch(/position:\s*absolute/);
  expect(ruleOf(".skip-link")).toMatch(/(?:left|top|inset-inline-start):\s*-/);
  expect(ruleOf(".skip-link:focus")).toMatch(/(?:left|top):\s*[0-9.]/);
});

test("a field's refusal, a failed result and a date are drawn as they should be", () => {
  expect(ruleOf(".field-error")).toMatch(/color:\s*var\(--problem\)/);
  expect(ruleOf("td.result-failed")).toMatch(/color:\s*var\(--problem\)/);
  expect(ruleOf("td.when")).toMatch(/white-space:\s*nowrap/);
  expect(ruleOf("td.wrap")).toMatch(/overflow-wrap:\s*anywhere/);
});

test("a field takes the page's colours in both schemes, and keeps a border", () => {
  const rule = ruleOf('input:not([type="checkbox"]), textarea, select');
  expect(rule).toMatch(/background:\s*var\(--surface\)/);
  expect(rule).toMatch(/color:\s*var\(--fg\)/);
  expect(rule).toMatch(/border:\s*1px solid var\(--border\)/);
  expect(ruleOf('input[aria-invalid="true"], textarea[aria-invalid="true"]')).toMatch(
    /border-color:\s*var\(--problem\)/,
  );
});

test("what is pressed is at least a pointer's minimum size, a checkbox's label included", () => {
  const minimum = (rule: string) => {
    const found = rule.match(/min-height:\s*([\d.]+)rem/)?.[1];
    return found ? parseFloat(found) * 16 : 0;
  };
  for (const selector of [
    'label:has(> input[type="checkbox"])',
    "button.name",
    "button.disclosure",
    "button, a.button",
  ]) {
    expect(minimum(ruleOf(selector)), selector).toBeGreaterThanOrEqual(24);
  }
});

test("a workspace's long name is cut short with an ellipsis, its dot staying beside it", () => {
  expect(ruleOf(".workspaces li")).toMatch(/display:\s*flex/);
  expect(ruleOf(".workspaces button.name")).toMatch(/text-overflow:\s*ellipsis/);
  expect(ruleOf(".workspaces button.name")).toMatch(/min-width:\s*0/);
  expect(ruleOf(".workspaces .dot")).toMatch(/flex:\s*none/);
});

// --- touch screens and phones -----------------------------------------------------------

// The least a target is high and wide on a touch screen, in px: WCAG 2.2's 2.5.5.
const TOUCH = 44;
const COARSE = "(pointer: coarse)";

// A length in px: rem at the browser's 16px, px as it is, and max() as its largest.
function px(value: string | undefined): number {
  if (!value) return 0;
  const lengths = [...value.matchAll(/([\d.]+)(rem|px)/g)].map(([, n, unit]) =>
    unit === "rem" ? parseFloat(n) * 16 : parseFloat(n),
  );
  return lengths.length ? Math.max(...lengths) : 0;
}

// The rules of a stylesheet outside any media query, each selector with its declarations.
function plainRules(css: string): [string, Map<string, string>][] {
  let text = uncommented(css);
  for (;;) {
    const at = text.indexOf("@media");
    if (at < 0) break;
    const open = text.indexOf("{", at);
    let depth = 0;
    let end = open;
    for (let i = open; i < text.length; i++) {
      if (text[i] === "{") depth++;
      if (text[i] === "}" && --depth === 0) {
        end = i;
        break;
      }
    }
    text = text.slice(0, at) + text.slice(end + 1);
  }
  return [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].flatMap(([, selectors, body]) =>
    selectors.split(",").map((selector) => {
      const found = new Map<string, string>();
      for (const [, property, value] of body.matchAll(/([\w-]+)\s*:\s*([^;]+);?/g))
        found.set(property, value.trim());
      return [selector.trim().replace(/\s+/g, " "), found] as [string, Map<string, string>];
    }),
  );
}

// What sets a minimum height outside the touch screen's rules that is not a target, and why.
const NOT_PRESSED: Record<string, string> = {
  ".sync": "the ahead and behind counts beside Push, which say and do nothing",
};

// The targets a touch screen's rules must make at least TOUCH high, and those also as wide.
const TALL = [
  "button",
  "a.button",
  "summary",
  "select",
  'input:not([type="checkbox"])',
  'label:has(> input[type="checkbox"])',
  "nav a",
  ".menu li a",
  ".skip-link",
  "p.source-page a",
];
const WIDE = ["button", "a.button", "nav a"];

// What is wrong with the targets on a touch screen: a target named above, or one whose own rule
// sets a smaller minimum height, which the touch screen's rules do not raise to TOUCH.
function touchFaults(css: string): string[] {
  const coarse = new Map<string, Map<string, string>>();
  for (const [selector, declared] of plainRules(mediaOf(COARSE, css)))
    coarse.set(selector, new Map([...(coarse.get(selector) ?? []), ...declared]));
  const found: string[] = [];
  const sized = plainRules(css)
    .filter(([, declared]) => declared.has("min-height"))
    .map(([selector]) => selector)
    .filter((selector) => !(selector in NOT_PRESSED));
  for (const selector of new Set([...TALL, ...sized])) {
    if (px(coarse.get(selector)?.get("min-height")) < TOUCH)
      found.push(`${selector}: under ${TOUCH}px high on a touch screen`);
  }
  for (const selector of WIDE) {
    if (px(coarse.get(selector)?.get("min-width")) < TOUCH)
      found.push(`${selector}: under ${TOUCH}px wide on a touch screen`);
  }
  return found;
}

test("on a touch screen every target is at least 44px, at every width", () => {
  expect(mediaOf(COARSE)).not.toBe(""); // positive control: the rules are read
  expect(touchFaults(STYLES)).toEqual([]);
});

test("the touch check fails on a target left small, one sized elsewhere, and finds each exemption", () => {
  const withoutButton = STYLES.replace(
    "  button,\n  a.button,\n  summary,",
    "  a.button,\n  summary,",
  );
  expect(withoutButton).not.toEqual(STYLES); // the plant took effect
  expect(touchFaults(withoutButton)).toContain(`button: under ${TOUCH}px high on a touch screen`);
  const sizedElsewhere = `${STYLES}\n.planted-target { min-height: 1.5rem; }`;
  expect(touchFaults(sizedElsewhere)).toEqual([
    `.planted-target: under ${TOUCH}px high on a touch screen`,
  ]);
  // an exemption names a rule that is still there
  for (const selector of Object.keys(NOT_PRESSED))
    expect(
      plainRules(STYLES).some(([s, declared]) => s === selector && declared.has("min-height")),
    ).toBe(true);
  expect(px("max(1rem, 18px)")).toBe(18);
});

// What is wrong with the size of the text in fields: one under 16px, which iOS zooms in on, or a
// rule that resets it with the font shorthand.
const FIELD = /(?:^|[\s>+~(,])(?:input|textarea|select)\b(?!\[type="checkbox"\])/;
function fieldFontFaults(css: string): string[] {
  const found: string[] = [];
  const rules = plainRules(css).filter(([selector]) => FIELD.test(selector));
  const base = rules.find(([selector]) => selector === 'input:not([type="checkbox"])');
  if (px(base?.[1].get("font-size")) < 16) found.push("fields: under 16px");
  for (const [selector, declared] of rules) {
    if (declared.has("font")) found.push(`${selector}: the font shorthand resets the size`);
    if (declared.has("font-size") && px(declared.get("font-size")) < 16)
      found.push(`${selector}: under 16px`);
  }
  return found;
}

test("the text in a field is at least 16px, so that a phone does not zoom in on it", () => {
  expect(plainRules(STYLES).filter(([selector]) => FIELD.test(selector)).length).toBeGreaterThan(1); // non-vacuity
  expect(fieldFontFaults(STYLES)).toEqual([]);
});

test("the field check fails on a smaller size, or the font shorthand, and passes a checkbox", () => {
  expect(fieldFontFaults(`${STYLES}\n.planted input { font-size: 0.8rem; }`)).toEqual([
    ".planted input: under 16px",
  ]);
  expect(fieldFontFaults(`${STYLES}\n.planted textarea { font: inherit; }`)).toEqual([
    ".planted textarea: the font shorthand resets the size",
  ]);
  expect(
    fieldFontFaults(`${STYLES}\n.planted input[type="checkbox"] { font-size: 0.8rem; }`),
  ).toEqual([]);
});

test("the meta tag lets the page be zoomed, and has the keyboard shrink the page's viewport", () => {
  const html = readFileSync(resolve(__dirname, "..", "index.html"), "utf-8");
  const viewport = html.match(/<meta\s+name="viewport"\s+content="([^"]*)"/)?.[1];
  expect(viewport).toMatch(/width=device-width/); // positive control: the tag is read
  expect(viewport).not.toMatch(/user-scalable\s*=\s*(?:no|0)|maximum-scale/);
  // where the browser honours it, an on-screen keyboard resizes the page, dvh with it, so that
  // the terminal's screen (.terminal-screen in src/styles.css) fits above the keyboard
  expect(viewport).toMatch(/(?:^|,)\s*interactive-widget\s*=\s*resizes-content\s*(?:,|$)/);
});

test("on a phone a dialog fills the screen, and on a wider one it sits in the middle", () => {
  const phone = ruleOf("dialog.dialog", mediaOf(PHONE));
  expect(phone).toMatch(/width:\s*100%/);
  expect(phone).toMatch(/height:\s*100dvh/);
  expect(phone).toMatch(/max-width:\s*none/);
  expect(phone).toMatch(/max-height:\s*none/);
  expect(phone).toMatch(/margin:\s*0/);
  // wider: the browser's own placement of a modal, in the middle, kept by margin: auto
  expect(ruleOf("dialog.dialog")).toMatch(/margin:\s*auto/);
  expect(ruleOf("dialog.dialog::backdrop")).toMatch(/background:\s*var\(--backdrop\)/);
});

test("the terminal's height follows the room the phone's keyboard leaves", () => {
  expect(ruleOf(".terminal-screen")).toMatch(/height:[^;]*\d+dvh/);
});
