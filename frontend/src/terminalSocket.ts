// The Terminal tab's protocol over its WebSocket, as backend/rcman/routers/terminal.py
// speaks it: the route and its query, the frames, the sizes, the close codes and the
// reasons a closed frame gives. tests/test_terminal.py holds PROTOCOL to the backend's.
// This module imports nothing, so that the test can load it by itself.
export const PROTOCOL = {
  path: "/api/terminal",
  query: "workspace",
  resize: "resize",
  exit: "exit",
  closed: "closed",
  problem: "problem",
  size_min: 1,
  size_max: 65535,
  close_normal: 1000,
  close_unsupported: 1003,
  close_policy: 1008,
  close_closing: 1012,
  close_problem: 4000,
  reasons: [
    "terminal_replaced",
    "workspace_gone",
    "workspace_rebuilding",
    "signed_out",
    "not_allowed",
    "service_closing",
  ],
} as const;

export type Size = { cols: number; rows: number };

// What a text frame from rcman says. One the page does not know is "unknown", and changes
// nothing.
export type Frame =
  | { type: "exit"; code: number | null }
  | { type: "closed"; reason: string }
  | { type: "problem"; problem: Record<string, unknown> }
  | { type: "unknown" };

// A whole number of columns or rows within what a resize may give.
function bounded(value: number): number {
  const whole = Number.isFinite(value) ? Math.floor(value) : PROTOCOL.size_min;
  return Math.min(PROTOCOL.size_max, Math.max(PROTOCOL.size_min, whole));
}

// The text frame that gives the terminal's size: the first sent, and one on every fit.
export function resizeFrame({ cols, rows }: Size): string {
  return JSON.stringify({ type: PROTOCOL.resize, cols: bounded(cols), rows: bounded(rows) });
}

// A text frame from rcman, read.
export function readFrame(text: string): Frame {
  let frame: unknown;
  try {
    frame = JSON.parse(text);
  } catch {
    return { type: "unknown" };
  }
  if (frame === null || typeof frame !== "object") return { type: "unknown" };
  const { type, code, reason, problem } = frame as Record<string, unknown>;
  if (type === PROTOCOL.exit) return { type: "exit", code: typeof code === "number" ? code : null };
  if (type === PROTOCOL.closed && typeof reason === "string") return { type: "closed", reason };
  if (type === PROTOCOL.problem && problem !== null && typeof problem === "object")
    return { type: "problem", problem: problem as Record<string, unknown> };
  return { type: "unknown" };
}
