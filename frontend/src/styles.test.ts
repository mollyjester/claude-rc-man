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
  const lightSet = new Set(light(css));
  for (const name of darkSet) if (!lightSet.has(name)) found.push(`only in dark: ${name}`);
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

  const darkOnly = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{)/,
    "$1\n    --planted-dark: #123456;",
  );
  expect(darkOnly).not.toEqual(STYLES); // the plant took effect
  expect(violations(darkOnly)).toEqual(["only in dark: --planted-dark"]);
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
// unlike a primary or a plain one, or a danger outline not unlike a secondary one or not in
// danger's text colour.
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
    // danger's text on a card: the fill's red in light, a lighter red of it in dark
    if (outline.color !== resolved("var(--danger-text)", variables(css, scheme)))
      found.push(`${scheme}: a danger outline is not in the danger colour`);
  }
  return found;
}

test("danger buttons look like no other button, in both schemes", () => {
  expect(looks(STYLES, "button.primary", "dark").background).toMatch(/^#/); // positive control: the reader resolves
  expect(buttonFaults(STYLES)).toEqual([]);
});

test("the check fails on a danger button or a danger outline that is not styled", () => {
  const noDanger = STYLES.replace(/button\.danger,\s*a\.button\.danger\s*\{[^}]*\}/, "");
  expect(noDanger).not.toEqual(STYLES);
  expect(buttonFaults(noDanger)).toContain("light: a danger button looks like another");
  const noOutline = STYLES.replace(
    /button\.secondary-danger,\s*a\.button\.secondary-danger\s*\{[^}]*\}/,
    "",
  );
  expect(noOutline).not.toEqual(STYLES);
  expect(buttonFaults(noOutline)).toContain("dark: a danger outline looks like a secondary button");
  // the dark scheme's own danger fill made the accent's
  const likePrimary = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{[^}]*?--danger:\s*)[^;]+/,
    "$1var(--accent)",
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

test("a repository in a workspace's panel is cut short with an ellipsis, so a long one never widens the panel", () => {
  expect(ruleOf(".workspace-log")).toMatch(/overflow-wrap:\s*anywhere/); // positive control
  expect(ruleOf(".repo-link span")).toMatch(/text-overflow:\s*ellipsis/);
  expect(ruleOf(".repo-link span")).toMatch(/white-space:\s*nowrap/);
  expect(ruleOf("a.repo-link")).toMatch(/min-width:\s*0/);
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

test("a table, a help page and a wide word never widen the page", () => {
  expect(ruleOf(".table-scroll")).toMatch(/overflow-x:\s*auto/);
  expect(ruleOf(".table-scroll")).toMatch(/max-width:\s*100%/);
  // help: one column on a phone, the text in a box that may shrink
  expect(ruleOf(".docs-article")).toMatch(/min-width:\s*0/);
  expect(ruleOf(".docs", mediaOf(PHONE))).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  expect(ruleOf(".docs-article pre")).toMatch(/overflow-x:\s*auto/);
});

test("the sidebar is a column beside the page, and on a phone a drawer the menu button opens", () => {
  // beside the page: the shell's columns, the sidebar's and the page's, which may shrink
  expect(ruleOf(".shell")).toMatch(
    /grid-template-columns:\s*var\(--sidebar\)\s+minmax\(0,\s*1fr\)/,
  );
  expect(ruleOf(".menu-button")).toMatch(/display:\s*none/);
  const phone = mediaOf(PHONE);
  expect(ruleOf(".shell", phone)).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  expect(ruleOf(".menu-button", phone)).toMatch(/display:\s*inline-flex/);
  // the one navigation: out of the way until opened, then over the page
  expect(ruleOf(".sidebar:not(.open)", phone)).toMatch(/display:\s*none/);
  expect(ruleOf(".sidebar", phone)).toMatch(/position:\s*fixed/);
  // the top bar is one row, of a height of its own on each
  expect(ruleOf(".topbar")).toMatch(/flex-wrap:\s*nowrap/);
  expect(ruleOf(".topbar")).toMatch(/height:\s*var\(--topbar\)/);
  expect(ruleOf(".topbar", phone)).toMatch(/height:\s*var\(--topbar-phone\)/);
});

test("the chrome's links are not underlined", () => {
  expect(ruleOf(".topbar a, .sidebar a")).toMatch(/text-decoration:\s*none/);
});

test("the check on a media query reads one, and finds none that is not there", () => {
  expect(mediaOf(PHONE)).toContain(".sidebar"); // positive control: the phone layout is read
  expect(mediaOf("(max-width: 1px)")).toBe("");
});

test("on a phone the list and the selected workspace's panel take turns, with the way back over the panel", () => {
  const phone = mediaOf(PHONE);
  expect(ruleOf(".dashboard.showing > .list", phone)).toMatch(/display:\s*none/);
  expect(ruleOf(".dashboard:not(.showing) > .panel-side", phone)).toMatch(/display:\s*none/);
  expect(ruleOf(".panel-bar-root", phone)).toMatch(/display:\s*contents/);
  expect(ruleOf(".panel-bar", phone)).toMatch(/position:\s*sticky/);
  // beside each other, the list is there, so the way back is not
  expect(ruleOf(".panel-bar-root")).toMatch(/display:\s*none/);
});

test("on a phone what is scrolled to stops below the workspace's bar too, while a panel is shown", () => {
  const phone = mediaOf(PHONE);
  expect(ruleOf("html:has(.dashboard.showing)", phone)).toMatch(
    /scroll-padding-top:\s*calc\(var\(--topbar-phone\) \+ var\(--panel-bar\)\)/,
  );
  expect(ruleOf(".panel-bar", phone)).toMatch(/min-height:\s*var\(--panel-bar\)/);
});

test("a pill and a message bar are edged in their state's stroke, a message bar never a box", () => {
  for (const kind of ["running", "waiting", "problem", "stopped"])
    expect(ruleOf(`.badge-${kind}`)).toMatch(
      new RegExp(`border-color:\\s*var\\(--${kind}-stroke\\)`),
    );
  expect(ruleOf(".message")).toMatch(/border-block:\s*1px solid/);
  expect(ruleOf(".message")).not.toMatch(/border-left:/);
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
  expect(rule).toMatch(/border:\s*1px solid var\(--field-border\)/);
  expect(ruleOf('input[aria-invalid="true"], textarea[aria-invalid="true"]')).toMatch(
    /border-color:\s*var\(--problem\)/,
  );
});

test("what is pressed is at least a pointer's minimum size, a checkbox's label included", () => {
  const minimum = (rule: string) =>
    px(resolved(rule.match(/min-height:\s*([^;]+)/)?.[1] ?? "", variables(STYLES, "light")));
  expect(minimum("min-height: var(--control);")).toBeGreaterThan(0); // positive: a token is read
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
  expect(ruleOf(".name-text")).toMatch(/text-overflow:\s*ellipsis/);
  expect(ruleOf(".name-text")).toMatch(/min-width:\s*0/);
  expect(ruleOf(".workspaces button.name")).toMatch(/min-width:\s*0/);
  expect(ruleOf(".dot-slot")).toMatch(/flex:\s*none/);
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
  ".badge": "a state's pill, which says and does nothing",
  ".shell": "the frame of every page, as high as the window",
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
  ".skip-link",
  "a.repo-link",
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
  const vars = variables(css, "light");
  const length = (value: string | undefined) => px(value && resolved(value, vars));
  for (const selector of new Set([...TALL, ...sized])) {
    if (length(coarse.get(selector)?.get("min-height")) < TOUCH)
      found.push(`${selector}: under ${TOUCH}px high on a touch screen`);
  }
  for (const selector of WIDE) {
    if (length(coarse.get(selector)?.get("min-width")) < TOUCH)
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

test("on a phone a dialog is a sheet from the screen's foot, and on a wider one it sits in the middle", () => {
  const phone = ruleOf("dialog.dialog", mediaOf(PHONE));
  expect(phone).not.toBe(""); // positive control: the phone's rule is read
  expect(phone).toMatch(/width:\s*100%/);
  expect(phone).toMatch(/max-width:\s*none/);
  // from the foot: no room under it, the rest above
  expect(phone).toMatch(/margin:\s*auto 0 0(?:;|$)/);
  expect(phone).toMatch(/max-height:[^;]*\d+dvh/);
  // its top corners rounded, its foot square against the screen's edge
  expect(phone).toMatch(/border-radius:\s*var\(--radius-card\) var\(--radius-card\) 0 0/);
  // wider: the browser's own placement of a modal, in the middle, kept by margin: auto
  expect(ruleOf("dialog.dialog")).toMatch(/margin:\s*auto/);
  expect(ruleOf("dialog.dialog")).toMatch(/max-height:[^;]*\d+dvh/);
  expect(ruleOf("dialog.dialog::backdrop")).toMatch(/background:\s*var\(--backdrop\)/);
  // a large dialog, wider on a desktop, is a sheet as wide as any other on a phone
  expect(ruleOf("dialog.dialog.large")).toMatch(/width:\s*\d/);
  expect(ruleOf("dialog.dialog.large", mediaOf(PHONE))).toMatch(/width:\s*100%/);
});

// The cards a dialog is drawn in, as Rebuild's is in the Container card: a rule that sets the
// margin of a card's first or last child, whatever it is, would take the dialog's margin: auto,
// which keeps it in the middle, from it when the dialog is that child.
const DIALOG_HOSTS = [".card", ".panel-section"];

function marginsTakenFromDialogs(css: string): string[] {
  return [...uncommented(css).matchAll(/([^{}]+)\{([^}]*)\}/g)]
    .filter(([, , body]) => /(?:^|;)\s*margin(?:-[a-z-]+)?\s*:/.test(body))
    .flatMap(([, selectors]) => selectors.split(","))
    .map((selector) => selector.trim().replace(/\s+/g, " "))
    .filter((selector) =>
      DIALOG_HOSTS.some((host) =>
        new RegExp(`(?:^|\\s)${host.replace(".", "\\.")} > (?::first-child|:last-child|\\*)`).test(
          selector,
        ),
      ),
    )
    .filter((selector) => !selector.includes(":not(dialog)"));
}

test("a dialog drawn inside a card keeps the margin that puts it in the middle", () => {
  expect(ruleOf(".card > :last-child:not(dialog)")).toMatch(/margin-bottom/); // positive control
  expect(marginsTakenFromDialogs(STYLES)).toEqual([]);
  const planted = `${STYLES}\n.card > :last-child { margin-bottom: 0; }`;
  expect(marginsTakenFromDialogs(planted)).toEqual([".card > :last-child"]);
});

test("a dialog's footer keeps to the foot of the dialog while what is above it scrolls", () => {
  const footer = ruleOf("dialog.dialog .footer");
  expect(footer).toMatch(/position:\s*sticky/);
  expect(footer).toMatch(/bottom:\s*0/);
  // drawn over what scrolls under it
  expect(footer).toMatch(/background:\s*var\(--surface\)/);
  // the dialog itself scrolls, with no padding of its own for the footer to stop short of
  expect(ruleOf("dialog.dialog")).toMatch(/overflow-y:\s*auto/);
  expect(ruleOf("dialog.dialog")).toMatch(/padding:\s*0(?:;|$)/);
});

test("the terminal's height follows the room the phone's keyboard leaves", () => {
  expect(ruleOf(".terminal-screen")).toMatch(/height:[^;]*\d+dvh/);
});

// --- the design system ------------------------------------------------------------------

// The families of the design system's tokens, each declared once, in the light scheme's :root,
// whose values the dark scheme does not change.
const TOKEN_FAMILIES = [
  /^--space-\d+$/,
  /^--radius-(?:control|card|pill)$/,
  /^--text-[\w-]+$/,
  /^--font(?:-mono)?$/,
  /^--control$/,
  /^--touch$/,
  /^--topbar(?:-phone)?$/,
  /^--sidebar$/,
  /^--elevation-[\w-]+$/,
];

test("the design system's spacing, radii, type, sizes and elevations are tokens of :root", () => {
  const names = light(STYLES);
  for (const family of TOKEN_FAMILIES)
    expect(
      names.some((name) => family.test(name)),
      String(family),
    ).toBe(true);
  // the spacing scale is on a 4px grid
  const vars = variables(STYLES, "light");
  const spaces = names.filter((name) => /^--space-\d+$/.test(name));
  expect(spaces.length).toBeGreaterThan(3);
  for (const name of spaces) expect(px(vars.get(name)) % 4, name).toBe(0);
  // sizes are not colours, and so not the dark scheme's to change
  expect(
    dark(STYLES).filter(
      (name) => TOKEN_FAMILIES.some((f) => f.test(name)) && !/elevation/.test(name),
    ),
  ).toEqual([]);
});

// What is wrong with how buttons and links drawn as one are laid out: not in a row with their
// label in the middle and a control's height, or a kind drawn for one and not the other.
function buttonLayoutFaults(css: string): string[] {
  const both = ruleOf("button, a.button", css);
  const found: string[] = [];
  for (const [what, pattern] of [
    ["not in a row", /display:\s*inline-flex/],
    ["not in the middle, top to bottom", /align-items:\s*center/],
    ["not in the middle, side to side", /justify-content:\s*center/],
    ["not a control's height", /min-height:\s*var\(--control\)/],
    ["its border outside its size", /box-sizing:\s*border-box/],
  ] as const)
    if (!pattern.test(both)) found.push(what);
  for (const kind of ["primary", "subtle", "danger", "secondary-danger"])
    if (!/background:/.test(ruleOf(`button.${kind}, a.button.${kind}`, css)))
      found.push(`${kind}: not drawn alike for both`);
  return found;
}

test("a button and a link drawn as one are laid out alike: in a row, their label in the middle", () => {
  expect(ruleOf("button, a.button")).not.toBe(""); // positive control: the rule is read
  expect(buttonLayoutFaults(STYLES)).toEqual([]);
});

test("the button check fails on a link left as a block of its own, and on a kind for buttons only", () => {
  const block = STYLES.replace(
    /(button,\s*a\.button\s*\{[^}]*?)display:\s*inline-flex;/,
    "$1display: inline-block;",
  );
  expect(block).not.toEqual(STYLES); // the plant took effect
  expect(buttonLayoutFaults(block)).toEqual(["not in a row"]);
  const buttonsOnly = STYLES.replace(/button\.subtle,\s*a\.button\.subtle\s*\{/, "button.subtle {");
  expect(buttonsOnly).not.toEqual(STYLES);
  expect(buttonLayoutFaults(buttonsOnly)).toEqual(["subtle: not drawn alike for both"]);
});

// WCAG's relative luminance of a colour written as #rgb, #rrggbb or rgba(), a translucent one
// over what it is drawn on.
function rgb(value: string, under?: number[]): number[] | null {
  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)?.[1];
  if (hex) {
    const full = hex.length === 3 ? [...hex].map((c) => c + c).join("") : hex;
    return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  }
  const parts = value.match(
    /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)$/,
  );
  if (!parts) return null;
  const [r, g, b] = [1, 2, 3].map((i) => parseFloat(parts[i]));
  const alpha = parts[4] === undefined ? 1 : parseFloat(parts[4]);
  const base = under ?? [255, 255, 255];
  return [r, g, b].map((c, i) => c * alpha + base[i] * (1 - alpha));
}
function luminance([r, g, b]: number[]): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}
function contrast(a: number[], b: number[]): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// What text may be drawn on where a rule gives it no background of its own: the page, a card,
// and the sidebar.
const CANVASES = ["--bg", "--surface", "--nav-bg"];

// Every rule of the stylesheet, in a media query or not, with its declarations.
function allRules(css: string): [string[], Map<string, string>][] {
  return [...uncommented(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selectors, body]) => {
    const found = new Map<string, string>();
    for (const [, property, value] of body.matchAll(/([\w-]+)\s*:\s*([^;]+);?/g))
      found.set(property, value.trim());
    return [selectors.split(",").map((s) => s.trim().replace(/\s+/g, " ")), found];
  });
}

// The colours text takes from around it where a rule gives a background and no colour.
const INHERITED = ["--fg", "--muted", "--accent-text"];

// Backgrounds that hold no text, and why: a rule all of whose selectors are here pairs its
// background with no colour. Each must still be a rule with a background.
const NO_TEXT: Record<string, string> = {
  ".dot-running": "a status dot, a shape of its colour",
  ".dot-problem": "a status dot, a shape of its colour",
  "dialog.dialog::backdrop": "the page dimmed behind a dialog",
  ".scrim:not([hidden])": "the page dimmed beside the drawer, which takes a press",
  ".sidebar a.active::before": "the accent's bar beside the page where you are",
  '.list-row[aria-current="true"]::before': "the accent's bar beside the selected row",
  ".list-row.selected::before": "the accent's bar beside the selected row",
  '.docs-toc nav a[aria-current="page"]::before':
    "the accent's bar beside the help page you are on",
};

// The pairs of colour tokens text is drawn in, read from the stylesheet: a rule's colour on its
// own background, a colour on no background of its rule's own on each canvas, and a background
// with no colour of its rule's own under each colour text takes from around it.
function textPairs(css: string): [string, string][] {
  const token = (value: string | undefined) => value?.match(/^var\(\s*(--[\w-]+)\s*\)$/)?.[1];
  const pairs = new Map<string, [string, string]>();
  for (const [selectors, declared] of allRules(css)) {
    const fg = token(declared.get("color"));
    const bg = token(declared.get("background") ?? declared.get("background-color"));
    if (!fg && (!bg || selectors.every((s) => s in NO_TEXT))) continue;
    for (const colour of fg ? [fg] : INHERITED)
      for (const on of bg ? [bg] : CANVASES) pairs.set(`${colour} on ${on}`, [colour, on]);
  }
  return [...pairs.values()];
}

// The pairs whose contrast is under WCAG AA's for text, in each scheme.
function contrastFaults(css: string): string[] {
  const found: string[] = [];
  for (const scheme of ["light", "dark"] as const) {
    const vars = variables(css, scheme);
    const surface = rgb(resolved("var(--surface)", vars)) ?? undefined;
    for (const [fg, bg] of textPairs(css)) {
      const back = rgb(resolved(`var(${bg})`, vars), surface ?? undefined);
      const fore = back && rgb(resolved(`var(${fg})`, vars), back);
      if (!back || !fore) {
        found.push(`${scheme}: ${fg} on ${bg} is not a colour`);
        continue;
      }
      const ratio = contrast(fore, back);
      if (ratio < 4.5) found.push(`${scheme}: ${fg} on ${bg} is ${ratio.toFixed(2)}:1`);
    }
  }
  return found;
}

test("contrast is worked out as WCAG has it", () => {
  expect(contrast([0, 0, 0], [255, 255, 255])).toBeCloseTo(21, 5);
  expect(contrast(rgb("#767676")!, rgb("#ffffff")!)).toBeCloseTo(4.54, 2);
  expect(rgb("rgba(0, 0, 0, 0.5)", [255, 255, 255])).toEqual([127.5, 127.5, 127.5]);
  expect(rgb("#fff")).toEqual([255, 255, 255]);
});

test("every colour text is drawn in meets WCAG AA's contrast on what it is drawn on, in both schemes", () => {
  const pairs = textPairs(STYLES);
  expect(pairs.length).toBeGreaterThan(10); // non-vacuity: the pairs are read
  expect(pairs).toContainEqual(["--accent-fg", "--accent"]); // a rule's own background
  expect(pairs).toContainEqual(["--muted", "--surface"]); // and a canvas
  // a danger fill with its text, and danger's text on a card, as an outline's is
  expect(pairs).toContainEqual(["--danger-fg", "--danger"]);
  expect(pairs).toContainEqual(["--danger-text", "--surface"]);
  expect(contrastFaults(STYLES)).toEqual([]);
});

test("in both schemes a danger fill is a saturated red with white on it, as the primary is blue with white", () => {
  for (const scheme of ["light", "dark"] as const) {
    const vars = variables(STYLES, scheme);
    expect(rgb(resolved("var(--danger-fg)", vars)), scheme).toEqual([255, 255, 255]);
    const [r, g, b] = rgb(resolved("var(--danger)", vars))!;
    expect(r, scheme).toBeGreaterThan(150);
    expect(Math.max(g, b), scheme).toBeLessThan(80); // saturated, not a pastel
  }
});

test("every background said to hold no text is still drawn by a rule", () => {
  for (const selector of Object.keys(NO_TEXT))
    expect(
      allRules(STYLES).some(
        ([selectors, declared]) =>
          selectors.includes(selector) &&
          (declared.has("background") || declared.has("background-color")),
      ),
      selector,
    ).toBe(true);
});

test("the contrast check fails on a background with no colour of its own that text around it cannot be read on", () => {
  const planted = `${STYLES}\n.planted { background: var(--border); }`;
  expect(textPairs(planted)).toContainEqual(["--accent-text", "--border"]); // the plant is read
  expect(contrastFaults(planted).some((f) => f.includes("on --border"))).toBe(true);
  expect(contrastFaults(STYLES).some((f) => f.includes("on --border"))).toBe(false);
});

test("the contrast check fails on a planted pair that is too faint, and on one in one scheme only", () => {
  const planted = `${STYLES}\n.planted { color: var(--border); background: var(--surface); }`;
  const faults = contrastFaults(planted);
  expect(faults.some((f) => f.startsWith("light: --border on --surface"))).toBe(true);
  expect(faults.some((f) => f.startsWith("dark: --border on --surface"))).toBe(true);
  const darkOnly = STYLES.replace(
    /(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{[^}]*?)--muted:[^;]*;/,
    "$1--muted: #3a3a3a;",
  );
  expect(darkOnly).not.toEqual(STYLES); // the plant took effect
  const dark = contrastFaults(darkOnly);
  expect(dark.some((f) => f.startsWith("dark: --muted on --surface"))).toBe(true);
  expect(dark.some((f) => f.startsWith("light:"))).toBe(false);
});

// The field rules: at rest, and with focus.
const FIELDS = 'input:not([type="checkbox"]), textarea, select';
const FIELDS_FOCUSED =
  'input:not([type="checkbox"]):focus-visible, textarea:focus-visible, select:focus-visible';

// What is wrong with a field's focus: none drawn, or one that is not told from the field at
// rest at WCAG's 3:1 for what is not text, in a scheme.
function focusFaults(css: string): string[] {
  const focused = ruleOf(FIELDS_FOCUSED, css);
  if (!/border-color:\s*var\(--[\w-]+\)/.test(focused)) return ["no border on focus"];
  const found: string[] = [];
  for (const scheme of ["light", "dark"] as const) {
    const vars = variables(css, scheme);
    const colourOf = (rule: string, property: RegExp) =>
      rgb(resolved(rule.match(property)?.[1] ?? "", vars));
    const field = colourOf(ruleOf(FIELDS, css), /background:\s*(var\([^)]*\))/)!;
    const rest = colourOf(ruleOf(FIELDS, css), /border:\s*1px solid (var\([^)]*\))/)!;
    const ring = colourOf(focused, /border-color:\s*(var\([^)]*\))/)!;
    if (contrast(ring, field) < 3) found.push(`${scheme}: the focus is faint on the field`);
    if (contrast(rest, field) < 3)
      found.push(`${scheme}: the border at rest is faint on the field`);
    if (ring.join() === rest.join()) found.push(`${scheme}: focus looks as rest does`);
  }
  if (!/box-shadow:[^;]*var\(--[\w-]+\)/.test(focused)) found.push("no ring inside the border");
  return found;
}

test("a field shows where focus is with its border, seen at 3:1 in both schemes", () => {
  expect(ruleOf(FIELDS_FOCUSED)).not.toBe(""); // positive control: the rule is read
  // the border is the ring; the outline is kept, transparent, for forced colours to draw
  expect(ruleOf(FIELDS_FOCUSED)).toMatch(/outline:\s*2px solid transparent/);
  expect(ruleOf(FIELDS_FOCUSED)).toMatch(/outline-offset:\s*-2px/);
  expect(focusFaults(STYLES)).toEqual([]);
});

test("the focus check fails on a focus drawn as the field at rest is", () => {
  const same = STYLES.replace(
    /(:focus-visible,\s*select:focus-visible\s*\{[^}]*?border-color:\s*)var\(--[\w-]+\)/,
    "$1var(--field-border)",
  );
  expect(same).not.toEqual(STYLES); // the plant took effect
  expect(focusFaults(same)).toContain("light: focus looks as rest does");
});

test("what is scrolled to, as by a link to a part, is not hidden under the top bar", () => {
  expect(ruleOf("html")).toMatch(/scroll-padding-top:\s*var\(--topbar\)/);
  expect(ruleOf("html", mediaOf(PHONE))).toMatch(/scroll-padding-top:\s*var\(--topbar-phone\)/);
});

test("with forced colours the sidebar still marks the page where you are", () => {
  const forced = mediaOf("(forced-colors: active)");
  expect(ruleOf('nav.sidebar a[aria-current="page"]', forced)).toMatch(/outline:\s*2px solid/);
});

// The rules that draw a hover outside the query for a pointer that can hover, where a touch
// screen would keep them on after a tap.
function ungatedHovers(css: string): string[] {
  return plainRules(css)
    .map(([selector]) => selector)
    .filter((selector) => selector.includes(":hover"));
}

test("hovering is drawn only where the pointer can hover", () => {
  expect(mediaOf("(hover: hover)")).toContain(":hover"); // positive control: the hovers are read
  expect(ungatedHovers(STYLES)).toEqual([]);
});

test("the hover check fails on a hover outside the query", () => {
  expect(ungatedHovers(`${STYLES}\n.planted:hover { color: var(--fg); }`)).toEqual([
    ".planted:hover",
  ]);
});

test("the browser's bar takes the top bar's colour in each scheme", () => {
  const html = readFileSync(resolve(__dirname, "..", "index.html"), "utf-8");
  for (const scheme of ["light", "dark"] as const) {
    const meta = html.match(
      new RegExp(
        `<meta name="theme-color" media="\\(prefers-color-scheme: ${scheme}\\)" content="([^"]+)"`,
      ),
    )?.[1];
    expect(meta, scheme).toBe(schemeValues(STYLES, scheme)["--surface"]);
  }
  expect(ruleOf(".topbar")).toMatch(/background:\s*var\(--surface\)/);
});
