import { useCallback, useEffect, useRef, useState } from "react";

import { type ApiError, api, problemFrom, UNREACHABLE } from "./api";
import { browser } from "./browser";
import { noteFailure, PAGE_ERROR } from "./clientErrors";
import { m } from "./messages";
import { ProblemView } from "./ProblemView";
import { TerminalKeys } from "./TerminalKeys";
import { encode, type Key, type Modifier, sent, typedWithHeld } from "./terminalKeys";
import { PROTOCOL, readFrame, resizeFrame, type Size } from "./terminalSocket";
import { readTheme } from "./terminalTheme";
import type { XtermView } from "./xtermView";

type XtermModule = typeof import("./xtermView");
// xterm.js, loaded once the first Terminal tab is shown, so that no other page waits for it;
// a load that failed is tried again by the next Terminal tab. It fails for good where the
// page was loaded before an upgrade and names a chunk the new build does not have, so the
// tab then offers to reload the page.
let loading: Promise<XtermModule> | null = null;
function loadXterm(): Promise<XtermModule> {
  loading ??= import("./xtermView").catch((cause) => {
    loading = null;
    throw cause;
  });
  return loading;
}

// Where the terminal's connection stands: being made, xterm.js not loaded, carrying the
// shell, or closed, with
// what closed it, for a workspace refused the problem rcman gave, and whether rcman would
// open another (`again`), which Reconnect is offered for.
type Phase =
  | { kind: "connecting" }
  | { kind: "unloaded" }
  | { kind: "connected" }
  | { kind: "closed"; said: string; problem: ApiError | null; again: boolean };
type Closed = Extract<Phase, { kind: "closed" }>;

// The reasons after which rcman would refuse another terminal on the workspace: it is gone
// stopped, gone or not ready, or its person signed out or is no longer allowed in.
const REFUSED_AGAIN = new Set(["workspace_gone", "workspace_stopped", "signed_out", "not_allowed"]);

const CONNECTING: Phase = { kind: "connecting" };
const encoder = new TextEncoder();

// Bytes xterm.js hands on in a string, one to each character, as some mouse reports are.
const bytesOf = (text: string) => Uint8Array.from(text, (c) => c.charCodeAt(0) & 0xff);

// What a frame rcman sent before closing says ended the terminal, if it said.
function saidBy(text: string): Closed | null {
  const frame = readFrame(text);
  if (frame.type === "exit")
    return { kind: "closed", said: m.terminalExited(frame.code), problem: null, again: true };
  if (frame.type === "closed") {
    const said = m.terminalClosed[frame.reason] ?? m.terminalClosedOther(frame.reason);
    return { kind: "closed", said, problem: null, again: !REFUSED_AGAIN.has(frame.reason) };
  }
  if (frame.type === "problem") {
    // the workspace is not the person's, or not ready: the page's list of it is out of date
    const status = typeof frame.problem.status === "number" ? frame.problem.status : 0;
    return {
      kind: "closed",
      said: m.terminalNotOpened,
      problem: problemFrom(frame.problem, status),
      again: false,
    };
  }
  return null;
}

// What a close that came with no frame says: rcman stopping, or the connection lost.
function closedWith(code: number): Closed {
  const said = code === PROTOCOL.close_closing ? m.terminalClosed.service_closing : m.terminalLost;
  return { kind: "closed", said, problem: null, again: true };
}

// The Terminal tab of a ready workspace: a shell in it, over the WebSocket api.openTerminal
// opens, drawn by xterm.js (src/xtermView.ts). It connects as it is mounted and closes as it
// is unmounted; what closes it otherwise is said, with Reconnect where rcman would open
// another, and nothing reconnects by itself. A socket that never opened is noted, and
// onFailure asks the page to refresh, which shows whether rcman is not reached or the
// sign-in has ended; so does a close after which rcman would refuse another. While connected,
// the bar offers the special keys (src/TerminalKeys.tsx), whose Shift, Ctrl and Alt go with the
// next key typed on the device's own keyboard too. The screen is
// fitted when `shown` and whenever its box changes size, and its colours read again when
// the colour scheme changes.
export function Terminal({
  workspace,
  shown,
  onFailure,
}: {
  workspace: number;
  shown: boolean;
  onFailure: () => void;
}) {
  const screen = useRef<HTMLDivElement>(null);
  const [xterm, setXterm] = useState<XtermModule | null>(null);
  // The screen, once xterm.js is loaded and it is made.
  const [shell, setShell] = useState<XtermView | null>(null);
  const view = useRef<XtermView | null>(null);
  // The socket carrying the shell, once its first resize is sent.
  const carrying = useRef<WebSocket | null>(null);
  const sentSize = useRef<Size | null>(null);
  const failed = useRef(onFailure);
  failed.current = onFailure;
  const [phase, setPhase] = useState<Phase>(CONNECTING);
  const [attempt, setAttempt] = useState(0);
  // Shift, Ctrl and Alt held on the special keys for the next key, from them or from the
  // device's own keyboard; the ref is what is held now, for what is typed before a render.
  const [held, setHeld] = useState<readonly Modifier[]>([]);
  const holding = useRef<readonly Modifier[]>([]);
  const hold = useCallback((next: readonly Modifier[]) => {
    holding.current = next;
    setHeld(next);
  }, []);

  // Fits the screen and sends its size where it is new.
  const fit = useRef(() => {
    const size = view.current?.fit();
    const socket = carrying.current;
    if (!size || !socket) return;
    const sent = sentSize.current;
    if (sent && sent.cols === size.cols && sent.rows === size.rows) return;
    sentSize.current = size;
    socket.send(resizeFrame(size));
  });

  // Whether the tab has been shown since xterm.js loaded: the screen is made only where it
  // can be measured, and kept from then on.
  const [placed, setPlaced] = useState(false);
  useEffect(() => {
    if (xterm && shown) setPlaced(true);
  }, [xterm, shown]);

  // xterm.js, loaded as the tab is mounted; a load that fails is reported as the page's own
  // fault.
  useEffect(() => {
    let gone = false;
    loadXterm().then(
      (loaded) => {
        if (!gone) setXterm(loaded);
      },
      (cause) => {
        if (gone) return;
        noteFailure({ kind: PAGE_ERROR, cause });
        setPhase({ kind: "unloaded" });
      },
    );
    return () => {
      gone = true;
    };
  }, []);

  // The screen, for as long as the tab is on the page.
  useEffect(() => {
    if (!xterm || !placed) return;
    // biome-ignore lint/style/noNonNullAssertion: the element is rendered before its effect runs
    const element = screen.current!;
    const made = xterm.createXtermView(element, readTheme());
    view.current = made;
    setShell(made);
    const send = (bytes: Uint8Array<ArrayBuffer>) => carrying.current?.send(bytes);
    // What is typed goes with what is held where it is a key or plain text, which lets it go;
    // what xterm.js sends of its own goes as it is: a paste, which the screen takes from
    // xterm.js in the capture phase and hands to its paste() itself, so that what that sends,
    // at once, is known (`pasting`), and a focus or mouse report. An arrow xterm.js sends for
    // the mouse wheel on the alternate screen, where no program asked for the mouse, cannot be
    // told from a typed one, and takes what is held.
    let pasting = false;
    const onPaste = (event: ClipboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
      // as xterm.js's own handler, nothing where the event carries no clipboard
      if (!event.clipboardData) return;
      pasting = true;
      try {
        made.paste(event.clipboardData.getData("text/plain"));
      } finally {
        pasting = false;
      }
    };
    element.addEventListener("paste", onPaste, { capture: true });
    made.onData((data) => {
      let typed = data;
      if (holding.current.length > 0 && !pasting) {
        const withHeld = typedWithHeld(data, holding.current, made.applicationCursorKeys());
        if (withHeld !== null) {
          typed = withHeld;
          hold([]);
        }
      }
      send(encoder.encode(typed));
    });
    made.onBinary((data) => send(bytesOf(data)));
    const refit = fit.current;
    const observer = new ResizeObserver(() => refit());
    observer.observe(element);
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const recolour = () => made.setTheme(readTheme());
    scheme.addEventListener("change", recolour);
    return () => {
      element.removeEventListener("paste", onPaste, { capture: true });
      observer.disconnect();
      scheme.removeEventListener("change", recolour);
      view.current = null;
      setShell(null);
      made.dispose();
    };
  }, [xterm, placed, hold]);

  // The socket: one for each attempt, closed when the next is made or the tab goes.
  useEffect(() => {
    void attempt; // each Reconnect is an attempt of its own, with a socket of its own
    if (!shell) return;
    // Until it closes, or the next is made: what a socket says after that is not heard.
    let live = true;
    const socket = api.openTerminal(workspace);
    const started = performance.now();
    let opened = false;
    let said: Closed | null = null;
    socket.addEventListener("open", () => {
      if (!live) return;
      opened = true;
      carrying.current = socket;
      sentSize.current = null;
      fit.current(); // the size first, before anything typed
      shell.setInputEnabled(true);
      setPhase({ kind: "connected" });
    });
    socket.addEventListener("message", (event: MessageEvent) => {
      if (!live) return;
      if (typeof event.data === "string") said = saidBy(event.data) ?? said;
      else shell.write(new Uint8Array(event.data as ArrayBuffer));
    });
    socket.addEventListener("close", (event: CloseEvent) => {
      if (!live) return;
      live = false;
      carrying.current = null;
      shell.setInputEnabled(false);
      if (!opened) {
        noteFailure({
          kind: UNREACHABLE,
          method: "GET",
          path: PROTOCOL.path,
          elapsedMs: performance.now() - started,
          cause: new Error(`The terminal's WebSocket closed with ${event.code} before it opened`),
        });
        setPhase({ kind: "closed", said: m.terminalNotOpened, problem: null, again: true });
        failed.current();
        return;
      }
      const ended = said ?? closedWith(event.code);
      setPhase(ended);
      if (!ended.again) failed.current(); // what the page shows of the workspace has changed
    });
    shell.setInputEnabled(false);
    return () => {
      live = false;
      if (carrying.current === socket) carrying.current = null;
      socket.close(PROTOCOL.close_normal);
    };
  }, [shell, workspace, attempt]);

  // What is held is let go once the terminal's connection ends.
  useEffect(() => {
    if (phase.kind !== "connected") hold([]);
  }, [phase.kind, hold]);

  // A special key, or the keys field's text, with what is held, which is let go first, so that
  // what the screen then hands on is not given it again. It goes through the screen's input, so
  // that it is dropped where typing would be.
  function input(data: (applicationCursor: boolean) => string) {
    const shown = view.current;
    if (!shown) return;
    const bytes = data(shown.applicationCursorKeys());
    hold([]);
    if (bytes !== "") shown.input(bytes);
  }
  const pressKey = (key: Key) => input((cursor) => encode(key, holding.current, cursor));
  // Sent, the pop-up closes and focus goes to the screen, where typing resumes.
  function sendText(text: string) {
    input((cursor) => sent(text, holding.current, cursor));
    view.current?.focus();
  }

  // Shown again, the screen may have another size than when it was hidden.
  useEffect(() => {
    if (shown) fit.current();
  }, [shown]);

  // The button goes as it is pressed, so focus goes to the screen, where typing resumes.
  function reconnect() {
    view.current?.reset();
    view.current?.focus();
    setPhase(CONNECTING);
    setAttempt((n) => n + 1);
  }

  // How the terminal stands, as a dot of the status dots' shapes beside the words.
  const kind =
    phase.kind === "connected"
      ? "running"
      : phase.kind === "connecting"
        ? "waiting"
        : phase.kind === "unloaded" || (phase.kind === "closed" && phase.problem)
          ? "problem"
          : "stopped";
  // The card is .terminal-card, never .terminal: xterm.js puts the class terminal on its own
  // element, and rcman's rules never name a class xterm.js uses.
  return (
    <div className="terminal-card card">
      <div className="terminal-bar">
        <span className={`dot dot-${kind}`} aria-hidden="true" />
        <p role="status">
          {phase.kind === "connecting" && m.terminalConnecting}
          {phase.kind === "unloaded" && m.terminalNotLoaded}
          {phase.kind === "connected" && m.terminalConnected}
          {phase.kind === "closed" && phase.said}
        </p>
        {phase.kind === "connected" && (
          <TerminalKeys held={held} onHold={hold} onKey={pressKey} onSend={sendText} />
        )}
        {phase.kind === "unloaded" && (
          // the page loaded afresh names the build rcman serves now
          <button type="button" className="primary" onClick={() => browser.reload()}>
            {m.reloadPage}
          </button>
        )}
        {phase.kind === "closed" && phase.again && (
          <button type="button" className="primary" onClick={reconnect}>
            {m.terminalReconnect}
          </button>
        )}
      </div>
      {phase.kind === "closed" && phase.problem && (
        <div className="terminal-problem">
          <ProblemView error={phase.problem} />
        </div>
      )}
      <div className="terminal-screen" ref={screen} />
    </div>
  );
}

// The Terminal tab of a workspace that has no terminal to open, stopped or being started: the
// terminal's bar alone, saying why, its dot not running, and no screen.
export function TerminalNotice({ said }: { said: string }) {
  return (
    <div className="terminal-card card">
      <div className="terminal-bar">
        <span className="dot dot-stopped" aria-hidden="true" />
        <p role="status">{said}</p>
      </div>
    </div>
  );
}
