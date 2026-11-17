// The Terminal tab's screen: xterm.js, with its fit add-on, behind the few calls the tab
// makes, so that the tests put a stand-in in its place (src/test/fakeXterm.ts) — jsdom has no
// canvas and no layout to draw in or measure.
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import type { Size } from "./terminalSocket";
import type { TerminalTheme } from "./terminalTheme";

export interface XtermView {
  // What the shell printed, as bytes.
  write(data: Uint8Array): void;
  // What is typed, as text, and what xterm.js hands on as bytes held in a string.
  onData(listener: (data: string) => void): void;
  onBinary(listener: (data: string) => void): void;
  // Fits the screen to its element, where the element has a size; the size it then has.
  fit(): Size;
  setTheme(theme: TerminalTheme): void;
  // Whether what is typed is taken at all.
  setInputEnabled(enabled: boolean): void;
  // Input as if typed, handed on to onData's listeners at once, and dropped while input is not
  // taken; focus stays where it is.
  input(data: string): void;
  // Whether the shell has asked for application cursor keys (DECCKM), with which the arrows,
  // Home and End send other sequences.
  applicationCursorKeys(): boolean;
  // Text pasted, as xterm.js's own paste handler sends it (bracketed where the shell asked),
  // handed on to onData's listeners at once, and dropped while input is not taken.
  paste(text: string): void;
  // Clears the screen, for a new shell.
  reset(): void;
  focus(): void;
  dispose(): void;
}

export function createXtermView(element: HTMLElement, theme: TerminalTheme): XtermView {
  const terminal = new Terminal({
    theme,
    fontFamily: getComputedStyle(element).fontFamily,
    // a cursor that blinks is motion, which a person may have asked their system for less of
    cursorBlink: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    // every colour drawn, from the theme or from what the shell asks, meets WCAG AA on its
    // background; xterm.js adjusts the foreground where it would not
    minimumContrastRatio: 4.5,
  });
  const fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(element);
  return {
    write: (data) => terminal.write(data),
    onData: (listener) => void terminal.onData(listener),
    onBinary: (listener) => void terminal.onBinary(listener),
    fit: () => {
      // nothing to fit to while the element is hidden: the size stays as it was
      if (fitAddon.proposeDimensions()) fitAddon.fit();
      return { cols: terminal.cols, rows: terminal.rows };
    },
    setTheme: (next) => {
      terminal.options.theme = next;
    },
    setInputEnabled: (enabled) => {
      terminal.options.disableStdin = !enabled;
    },
    input: (data) => terminal.input(data, true),
    applicationCursorKeys: () => terminal.modes.applicationCursorKeysMode,
    paste: (text) => terminal.paste(text),
    reset: () => terminal.reset(),
    focus: () => terminal.focus(),
    dispose: () => terminal.dispose(),
  };
}
