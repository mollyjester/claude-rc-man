// The Terminal tab's colours: each of xterm.js's theme colours is a custom property of
// src/styles.css, with a light and a dark value, read from the page as it is drawn now.
// src/styles.test.ts holds every property named here to the stylesheet.
import type { ITheme } from "@xterm/xterm";

export type TerminalTheme = ITheme;

// Each colour of xterm.js's theme, and the custom property that holds it.
export const THEME = {
  background: "--terminal-bg",
  foreground: "--terminal-fg",
  cursor: "--terminal-cursor",
  cursorAccent: "--terminal-cursor-accent",
  selectionBackground: "--terminal-selection",
  scrollbarSliderBackground: "--terminal-scrollbar",
  scrollbarSliderHoverBackground: "--terminal-scrollbar-hover",
  scrollbarSliderActiveBackground: "--terminal-scrollbar-active",
  black: "--terminal-black",
  red: "--terminal-red",
  green: "--terminal-green",
  yellow: "--terminal-yellow",
  blue: "--terminal-blue",
  magenta: "--terminal-magenta",
  cyan: "--terminal-cyan",
  white: "--terminal-white",
  brightBlack: "--terminal-bright-black",
  brightRed: "--terminal-bright-red",
  brightGreen: "--terminal-bright-green",
  brightYellow: "--terminal-bright-yellow",
  brightBlue: "--terminal-bright-blue",
  brightMagenta: "--terminal-bright-magenta",
  brightCyan: "--terminal-bright-cyan",
  brightWhite: "--terminal-bright-white",
} as const satisfies Partial<Record<keyof ITheme, `--terminal-${string}`>>;

// The theme as the page's colour scheme draws it now; a property that is not set is left
// to xterm.js's own.
export function readTheme(): TerminalTheme {
  const style = getComputedStyle(document.documentElement);
  const theme: TerminalTheme = {};
  for (const [colour, property] of Object.entries(THEME)) {
    const value = style.getPropertyValue(property).trim();
    if (value) theme[colour as keyof typeof THEME] = value;
  }
  return theme;
}
