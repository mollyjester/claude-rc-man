// The Terminal tab's special keys: what a key, with Shift, Ctrl or Alt, sends to the shell. The
// sequences are xterm.js's own, as its keyboard sends them
// (node_modules/@xterm/xterm/src/common/input/Keyboard.ts): a modified key's parameter is
// 1 + (Shift 1 | Alt 2 | Ctrl 4); Ctrl with a letter is its control character; Alt is an ESC
// before the key. As in xterm.js, Alt alone is ignored on Insert, Page Up and Page Down, and
// Tab takes Shift only.
// Where xterm.js sends nothing for a combination (Shift or Ctrl with Insert, Shift with Page Up
// or Down, which scroll its own screen), the key's modified form follows the same pattern as
// Delete's.

export type Modifier = "shift" | "ctrl" | "alt";
export const MODIFIERS: readonly Modifier[] = ["shift", "ctrl", "alt"];

export type KeyName =
  | "up"
  | "down"
  | "left"
  | "right"
  | "home"
  | "end"
  | "tab"
  | "esc"
  | "enter"
  | "space"
  | "backspace"
  | "insert"
  | "delete"
  | "pgup"
  | "pgdn"
  | `f${1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12}`;

// A key: a named one, or a character.
export type Key = { name: KeyName } | { char: string };

const ESC = "\x1b";

// The final letter of the arrows, Home and End, in CSI and SS3 forms.
const CURSOR: Partial<Record<KeyName, string>> = {
  up: "A",
  down: "B",
  right: "C",
  left: "D",
  home: "H",
  end: "F",
};
// The number of the keys sent as CSI <n> ~.
const TILDE: Partial<Record<KeyName, number>> = {
  insert: 2,
  delete: 3,
  pgup: 5,
  pgdn: 6,
  f5: 15,
  f6: 17,
  f7: 18,
  f8: 19,
  f9: 20,
  f10: 21,
  f11: 23,
  f12: 24,
};
// F1 to F4, SS3 P to S.
const PF: Partial<Record<KeyName, string>> = { f1: "P", f2: "Q", f3: "R", f4: "S" };

// The names a person may type for a key, in lower case.
const NAMES: Record<string, KeyName> = {
  up: "up",
  down: "down",
  left: "left",
  right: "right",
  home: "home",
  end: "end",
  tab: "tab",
  esc: "esc",
  escape: "esc",
  enter: "enter",
  space: "space",
  backspace: "backspace",
  insert: "insert",
  ins: "insert",
  delete: "delete",
  del: "delete",
  pgup: "pgup",
  pageup: "pgup",
  pgdn: "pgdn",
  pagedown: "pgdn",
  ...Object.fromEntries(
    Array.from({ length: 12 }, (_, i) => [`f${i + 1}`, `f${i + 1}` as KeyName]),
  ),
};

const parameter = (held: ReadonlySet<Modifier>) =>
  1 + (held.has("shift") ? 1 : 0) + (held.has("alt") ? 2 : 0) + (held.has("ctrl") ? 4 : 0);

// A character with Ctrl, as xterm.js sends it: @ to _ and the letters are their control
// characters, space and 2 NUL, 3 to 7 ESC to US, 8 and ? DEL; anything else is itself.
function control(char: string): string {
  const code = char.codePointAt(0) ?? 0;
  if (char === " " || char === "2") return "\x00";
  if (char === "?" || char === "8") return "\x7f";
  if (code >= 0x33 && code <= 0x37) return String.fromCharCode(code - 0x33 + 27);
  if (code >= 0x40 && code <= 0x7e) return String.fromCharCode(code & 0x1f);
  return char;
}

// What a key sends with the modifiers held.
export function encode(key: Key, mods: Iterable<Modifier>, applicationCursor: boolean): string {
  const held = new Set(mods);
  const alt = held.has("alt") ? ESC : "";
  if ("char" in key) {
    let char = held.has("shift") ? key.char.toUpperCase() : key.char;
    if (held.has("ctrl")) char = control(char);
    return alt + char;
  }
  const { name } = key;
  // Insert, Page Up and Page Down ignore Alt alone (Keyboard.ts's cases 45, 33 and 34)
  if (["insert", "pgup", "pgdn"].includes(name) && held.size === 1) held.delete("alt");
  const modified = held.size > 0;
  const cursor = CURSOR[name];
  if (cursor !== undefined) {
    if (modified) return `${ESC}[1;${parameter(held)}${cursor}`;
    return applicationCursor ? `${ESC}O${cursor}` : `${ESC}[${cursor}`;
  }
  const tilde = TILDE[name];
  if (tilde !== undefined)
    return modified ? `${ESC}[${tilde};${parameter(held)}~` : `${ESC}[${tilde}~`;
  const pf = PF[name];
  if (pf !== undefined) return modified ? `${ESC}[1;${parameter(held)}${pf}` : `${ESC}O${pf}`;
  switch (name) {
    case "tab":
      return held.has("shift") ? `${ESC}[Z` : "\t";
    case "esc":
      return alt + ESC;
    case "enter":
      return `${alt}\r`;
    case "backspace":
      return alt + (held.has("ctrl") ? "\b" : "\x7f");
    default:
      return encode({ char: " " }, held, applicationCursor);
  }
}

// What the keys field sends: `(ctrl|alt|shift "+")* key`, in any case, where the key is a name
// of NAMES or exactly one character, with the modifiers held on the panel too; anything else is
// text, sent as it is, the held modifiers going with its first character only.
export function sent(field: string, held: Iterable<Modifier>, applicationCursor: boolean): string {
  const mods = new Set(held);
  const parsed = /^((?:(?:ctrl|alt|shift)\+)*)(.+)$/is.exec(field);
  if (parsed) {
    const named = NAMES[parsed[2].toLowerCase()];
    const chars = [...parsed[2]];
    if (named !== undefined || chars.length === 1) {
      const own = parsed[1].toLowerCase().split("+").filter(Boolean) as Modifier[];
      const key: Key = named !== undefined ? { name: named } : { char: parsed[2] };
      return encode(key, [...mods, ...own], applicationCursor);
    }
  }
  return withHeld(field, mods);
}

// Text with the held modifiers on its first character.
function withHeld(text: string, held: ReadonlySet<Modifier>): string {
  if (held.size === 0 || text === "") return text;
  const [first, ...rest] = [...text];
  return encode({ char: first }, held, false) + rest.join("");
}

// What the device's own keyboard typed, as xterm.js hands it on, with the modifiers held on the
// panel: a key xterm.js sends a sequence for, sent again with them, or text with no ESC in it,
// with them on its first character. Anything else, such as a paste, a focus or a mouse report,
// is null: it is sent as it is, and the modifiers stay held.
export function typedWithHeld(
  data: string,
  held: Iterable<Modifier>,
  applicationCursor: boolean,
): string | null {
  const mods = new Set(held);
  const key = typedKey(data);
  if (key !== null) return encode(key, mods, applicationCursor);
  if (data.includes(ESC)) return null;
  return withHeld(data, mods);
}

// The key a sequence xterm.js sends unmodified stands for, if any.
function typedKey(data: string): Key | null {
  if (data === ESC) return { name: "esc" };
  if (data === "\t") return { name: "tab" };
  if (data === "\r") return { name: "enter" };
  if (data === "\x7f") return { name: "backspace" };
  if (!data.startsWith(ESC)) return null;
  // the sequence after its ESC
  const after = data.slice(1);
  const cursor = /^([[O])([ABCDHF])$/.exec(after);
  if (cursor) return { name: keyOf(CURSOR, cursor[2]) };
  const pf = /^O([PQRS])$/.exec(after);
  if (pf) return { name: keyOf(PF, pf[1]) };
  const tilde = /^\[(\d+)~$/.exec(after);
  if (tilde) {
    const found = (Object.keys(TILDE) as KeyName[]).find((n) => TILDE[n] === Number(tilde[1]));
    if (found) return { name: found };
  }
  return null;
}

const keyOf = <T>(table: Partial<Record<KeyName, T>>, value: T) =>
  (Object.keys(table) as KeyName[]).find((n) => table[n] === value) as KeyName;
