import { useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";

export type Server = {
  path: string;
  state: string;
  environment_url: string | null;
  session_name: string | null;
  exit_code: number | null;
  screen?: string[];
};

type Action = "stop" | "resume" | "startAgain" | "forget" | "showScreen" | "trustYes" | "trustNo";

// What each state reported by rc-helper allows; the server refuses anything else.
export const ACTIONS: Record<string, Action[]> = {
  starting: ["stop"],
  trust_prompt: ["trustYes", "trustNo", "stop"],
  connected: ["stop"],
  ready: ["stop"],
  stopped: ["resume", "forget"],
  trust_declined: ["startAgain", "forget"],
  logged_out: ["startAgain", "forget"],
  exited: ["resume", "forget", "showScreen"],
  unrecognised: ["showScreen", "stop"],
};

// States in which the server is running and its environment on claude.ai takes sessions.
export const LIVE = ["connected", "ready"];

// The kinds of state a badge or a folder's dot shows, most urgent first.
export const KIND_ORDER = ["problem", "waiting", "running", "stopped"] as const;
export type Kind = (typeof KIND_ORDER)[number];

export const KIND: Record<string, Kind> = {
  starting: "running",
  trust_prompt: "waiting",
  connected: "running",
  ready: "running",
  stopped: "stopped",
  trust_declined: "stopped",
  logged_out: "problem",
  exited: "problem",
  unrecognised: "problem",
};

// A state rcman does not know is a problem until it is taught it.
export const kindOf = (state: string): Kind => KIND[state] ?? "problem";

// The one action each state leads with; "openSession" is the link to claude.ai.
export const PRIMARY: Record<string, Action | "openSession" | null> = {
  starting: null,
  trust_prompt: "trustYes",
  connected: "openSession",
  ready: "openSession",
  stopped: "resume",
  trust_declined: "startAgain",
  logged_out: "startAgain",
  exited: "resume",
  unrecognised: "showScreen",
};

const CALLS: Record<Exclude<Action, "showScreen">, [string, Record<string, string>]> = {
  stop: ["/api/servers/stop", {}],
  resume: ["/api/servers/resume", {}],
  startAgain: ["/api/servers", {}],
  forget: ["/api/servers/forget", {}],
  trustYes: ["/api/servers/confirm", { answer: "y" }],
  trustNo: ["/api/servers/confirm", { answer: "n" }],
};

export const LABELS: Record<Action, string> = {
  stop: m.stop,
  resume: m.resume,
  startAgain: m.startAgain,
  forget: m.forget,
  showScreen: m.showScreen,
  trustYes: m.trustYes,
  trustNo: m.trustNo,
};

// The dot a folder carries: the most urgent kind among its own server and,
// while includeBelow, every server inside it; none where there is no server.
export function dotFor(path: string, servers: Server[], includeBelow: boolean): Kind | null {
  const kinds = new Set(
    servers
      .filter((s) => s.path === path || (includeBelow && s.path.startsWith(`${path}/`)))
      .map((s) => kindOf(s.state)),
  );
  return KIND_ORDER.find((kind) => kinds.has(kind)) ?? null;
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`badge badge-${kindOf(state)}`}>{m.states[state] ?? state}</span>;
}

// A coloured dot, and its meaning in words for whatever names it with aria-describedby.
export function StatusDot({ kind, id }: { kind: Kind; id: string }) {
  return (
    <>
      <span className={`dot dot-${kind}`} aria-hidden="true" />
      <span id={id} className="visually-hidden">
        {m.kinds[kind]}
      </span>
    </>
  );
}

export function ServerCard({ server, onChange }: { server: Server; onChange: () => void }) {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [showScreen, setShowScreen] = useState(false);
  const actions = ACTIONS[server.state] ?? ["showScreen"];
  const primary = PRIMARY[server.state];

  async function act(action: Action) {
    if (action === "showScreen") {
      setShowScreen((shown) => !shown);
      return;
    }
    const [path, extra] = CALLS[action];
    setBusy(true);
    setError(null);
    try {
      await api.post(path, { path: server.path, ...extra });
      onChange();
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article aria-label={server.path} className="server">
      <p>
        <StateBadge state={server.state} />
      </p>
      <p>{m.nextStep[server.state] ?? m.nextStepUnknown}</p>
      {server.state === "trust_prompt" && (
        <div className="question">
          <p>
            <strong>{m.trustTitle}</strong>
          </p>
          <p>{m.trustBody(server.path)}</p>
        </div>
      )}
      {server.session_name && (
        <p>
          {m.sessionName}: <code>{server.session_name}</code>
        </p>
      )}
      {server.environment_url && LIVE.includes(server.state) && (
        <p>
          <a
            href={server.environment_url}
            target="_blank"
            rel="noopener noreferrer"
            className={primary === "openSession" ? "button primary" : undefined}
          >
            {m.openSession}
          </a>
        </p>
      )}
      {server.exit_code !== null && server.state === "exited" && <p>{m.exitCode(server.exit_code)}</p>}
      <div className="actions">
        {actions.map((action) => (
          <button
            key={action}
            type="button"
            className={action === primary ? "primary" : "secondary"}
            disabled={busy}
            onClick={() => act(action)}
          >
            {LABELS[action]}
          </button>
        ))}
      </div>
      {showScreen && server.screen && (
        <pre className="screen" aria-label={m.screenTitle(server.path)}>
          {server.screen.join("\n")}
        </pre>
      )}
      <ProblemView error={error} />
    </article>
  );
}
