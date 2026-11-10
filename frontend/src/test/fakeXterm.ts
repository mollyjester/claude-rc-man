// A stand-in for src/xtermView.ts, which src/test/setup.ts puts in its place for every test:
// jsdom has no canvas and no layout for xterm.js to draw in or measure. Each view made is
// recorded with what was written to it, and a test types into it as a person would.
import type { Size } from "../terminalSocket";
import type { TerminalTheme } from "../terminalTheme";
import type { XtermView } from "../xtermView";

export class FakeView implements XtermView {
  // Every view made since the test began, in order.
  static made: FakeView[] = [];

  written = "";
  // The size a fit gives; a test changes it to stand for the screen changing size.
  size: Size = { cols: 80, rows: 24 };
  resets = 0;
  inputEnabled = true;
  disposed = false;
  private dataListeners: ((data: string) => void)[] = [];

  constructor() {
    FakeView.made.push(this);
  }

  write(data: Uint8Array) {
    this.written += new TextDecoder().decode(data);
  }
  onData(listener: (data: string) => void) {
    this.dataListeners.push(listener);
  }
  onBinary(_listener: (data: string) => void) {}
  fit(): Size {
    return { ...this.size };
  }
  setTheme() {}
  setInputEnabled(enabled: boolean) {
    this.inputEnabled = enabled;
  }
  reset() {
    this.resets += 1;
    this.written = "";
  }
  focus() {}
  dispose() {
    this.disposed = true;
  }

  // A person typing: what xterm.js hands on as text.
  type(text: string) {
    for (const listener of this.dataListeners) listener(text);
  }
}

export function createXtermView(_element: HTMLElement, _theme: TerminalTheme): XtermView {
  return new FakeView();
}

export const views = () => FakeView.made;
