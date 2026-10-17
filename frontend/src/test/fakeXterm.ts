// A stand-in for src/xtermView.ts, which src/test/setup.ts puts in its place for every test:
// jsdom has no canvas and no layout for xterm.js to draw in or measure. Each view made is
// recorded, with what it was given, and a test types into it as a person would.
import type { Size } from "../terminalSocket";
import type { TerminalTheme } from "../terminalTheme";
import type { XtermView } from "../xtermView";

export class FakeView implements XtermView {
  // Every view made since the test began, in order.
  static made: FakeView[] = [];

  written = "";
  themes: TerminalTheme[] = [];
  // The size a fit gives; a test changes it to stand for the screen changing size.
  size: Size = { cols: 80, rows: 24 };
  fits = 0;
  resets = 0;
  focuses = 0;
  inputEnabled = true;
  disposed = false;
  private dataListeners: ((data: string) => void)[] = [];
  private binaryListeners: ((data: string) => void)[] = [];

  constructor(
    public element: HTMLElement,
    theme: TerminalTheme,
  ) {
    this.themes.push(theme);
    FakeView.made.push(this);
  }

  write(data: Uint8Array) {
    this.written += new TextDecoder().decode(data);
  }
  onData(listener: (data: string) => void) {
    this.dataListeners.push(listener);
  }
  onBinary(listener: (data: string) => void) {
    this.binaryListeners.push(listener);
  }
  fit(): Size {
    this.fits += 1;
    return { ...this.size };
  }
  setTheme(theme: TerminalTheme) {
    this.themes.push(theme);
  }
  setInputEnabled(enabled: boolean) {
    this.inputEnabled = enabled;
  }
  reset() {
    this.resets += 1;
    this.written = "";
  }
  focus() {
    this.focuses += 1;
  }
  dispose() {
    this.disposed = true;
  }

  // A person typing: what xterm.js hands on as text.
  type(text: string) {
    for (const listener of this.dataListeners) listener(text);
  }
  // What xterm.js hands on as bytes held in a string, as some mouse reports are.
  typeBinary(text: string) {
    for (const listener of this.binaryListeners) listener(text);
  }
}

export function createXtermView(element: HTMLElement, theme: TerminalTheme): XtermView {
  return new FakeView(element, theme);
}

export const views = () => FakeView.made;
