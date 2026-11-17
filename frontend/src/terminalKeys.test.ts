import { type Modifier, sent } from "./terminalKeys";

// What the Terminal tab's keys field sends: the field's text, the modifiers held on the panel,
// whether the shell has asked for application cursor keys (DECCKM), and the bytes xterm.js
// sends for that key (node_modules/@xterm/xterm/src/common/input/Keyboard.ts).
const ROWS: [string, readonly Modifier[], boolean, string][] = [
  ["up", [], false, "\x1b[A"],
  ["up", [], true, "\x1bOA"],
  ["ctrl+up", [], false, "\x1b[1;5A"],
  ["shift+tab", [], false, "\x1b[Z"],
  ["esc", [], false, "\x1b"],
  ["alt+x", [], false, "\x1bx"],
  ["ctrl+c", [], false, "\x03"],
  ["ctrl+[", [], false, "\x1b"],
  ["c", ["ctrl"], false, "\x03"],
  ["cd", ["ctrl"], false, "\x03d"],
  ["héllo ✓", [], false, "héllo ✓"],
  ["home", [], true, "\x1bOH"],
  ["ctrl+del", [], false, "\x1b[3;5~"],
  ["pgup", [], false, "\x1b[5~"],
  ["f1", [], false, "\x1bOP"],
  ["shift+f5", [], false, "\x1b[15;2~"],
];

test("each key and combination is what xterm sends for it", () => {
  for (const [field, held, applicationCursor, bytes] of ROWS)
    expect(sent(field, held, applicationCursor), `${field} ${held} ${applicationCursor}`).toBe(
      bytes,
    );
});
