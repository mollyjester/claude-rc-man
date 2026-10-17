import { useEffect, useRef, useState } from "react";

import { type ApiError, api, problemFrom, UNREACHABLE } from "./api";
import { browser } from "./browser";
import { noteFailure, PAGE_ERROR } from "./clientErrors";
import { m } from "./messages";
import { ProblemView } from "./ProblemView";
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
// or not ready, or its person signed out or is no longer allowed in.
const REFUSED_AGAIN = new Set(["workspace_gone", "signed_out", "not_allowed"]);

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
// sign-in has ended; so does a close after which rcman would refuse another. The screen is
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
    made.onData((data) => send(encoder.encode(data)));
    made.onBinary((data) => send(bytesOf(data)));
    const refit = fit.current;
    const observer = new ResizeObserver(() => refit());
    observer.observe(element);
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const recolour = () => made.setTheme(readTheme());
    scheme.addEventListener("change", recolour);
    return () => {
      observer.disconnect();
      scheme.removeEventListener("change", recolour);
      view.current = null;
      setShell(null);
      made.dispose();
    };
  }, [xterm, placed]);

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

  return (
    <div className="terminal">
      <div className="terminal-bar">
        <p role="status">
          {phase.kind === "connecting" && m.terminalConnecting}
          {phase.kind === "unloaded" && m.terminalNotLoaded}
          {phase.kind === "connected" && m.terminalConnected}
          {phase.kind === "closed" && phase.said}
        </p>
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
      {phase.kind === "closed" && <ProblemView error={phase.problem} />}
      <div className="terminal-screen" ref={screen} />
    </div>
  );
}
