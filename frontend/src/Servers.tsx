import { useId, useState } from "react";

import { type ApiError, api } from "./api";
import { Caret } from "./Caret";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

export type Server = {
  workspace: number;
  state: string;
  environment_url: string | null;
  session_name: string | null;
  exit_code: number | null;
  screen?: string[];
};

type Action = "stop" | "resume" | "startAgain" | "forget" | "showScreen" | "trustYes" | "trustNo";

// What each state of a server allows; the server refuses anything else.
export const ACTIONS: Record<string, Action[]> = {
  starting: ["stop"],
  trust_prompt: ["trustYes", "trustNo", "stop"],
  connected: ["stop"],
  ready: ["stop"],
  stopped: ["resume", "forget"],
  trust_declined: ["startAgain", "forget"],
  logged_out: ["startAgain", "forget"],
  exited: ["resume", "forget", "showScreen"],
  reconnected: ["showScreen", "stop"],
  unrecognised: ["showScreen", "stop"],
};

// States in which the server is running and its environment on claude.ai takes sessions.
export const LIVE = ["connected", "ready"];

// The kinds of state a badge or a workspace's dot shows, most urgent first.
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
  // Its process runs, and Claude says it has reconnected.
  reconnected: "running",
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
  reconnected: "showScreen",
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

// What a workspace is, as the page lists it: its server, or why that could not be read.
export type Workspace = {
  id: number;
  name: string;
  state: "creating" | "ready" | "failed" | "deleting";
  error: string | null;
  // The address it is cloned from, null for an empty one; and the steps taken while it is
  // made, one to a line, null once it is ready.
  source: string | null;
  log: string | null;
  server: Server | null;
  server_error: { code: string; title: string } | null;
  // The Claude Code version its server started with, null while it has none running.
  claude_version: string | null;
};

// The dot a workspace carries: its server's kind, a problem where the workspace
// failed or its server could not be read, and none otherwise.
export function dotFor(workspace: Workspace): Kind | null {
  if (workspace.state === "failed" || workspace.server_error) return "problem";
  return workspace.server ? kindOf(workspace.server.state) : null;
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`badge badge-${kindOf(state)}`}>{m.states[state] ?? state}</span>;
}

// A dot whose shape and colour both say its kind, with the kind in words as its
// title, and as a hidden description for whatever names it with
// aria-describedby: hidden, so that it is read once, with that name.
export function StatusDot({ kind, id }: { kind: Kind; id: string }) {
  return (
    <>
      <span className={`dot dot-${kind}`} title={m.kinds[kind]} aria-hidden="true" />
      <span id={id} hidden>
        {m.kinds[kind]}
      </span>
    </>
  );
}

// A workspace's server and the actions its state allows.
export function ServerCard({
  workspace,
  server,
  claudeVersion = null,
  onChange,
}: {
  workspace: { id: number; name: string };
  server: Server;
  claudeVersion?: string | null;
  onChange: () => void;
}) {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [showScreen, setShowScreen] = useState(false);
  const screenId = useId();
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
      await api.post(path, { workspace: workspace.id, ...extra });
      onChange();
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article aria-label={workspace.name} className="server">
      <p>
        <StateBadge state={server.state} />
      </p>
      <p>{m.nextStep[server.state] ?? m.nextStepUnknown}</p>
      {claudeVersion && <p>{m.serverClaudeVersion(claudeVersion)}</p>}
      {server.state === "trust_prompt" && (
        <div className="question">
          <p>
            <strong>{m.trustTitle}</strong>
          </p>
          <p>{m.trustBody(workspace.name)}</p>
        </div>
      )}
      {server.session_name && (
        <p>
          {m.sessionName}: <code>{server.session_name}</code>
        </p>
      )}
      {server.exit_code !== null && server.state === "exited" && (
        <p>{m.exitCode(server.exit_code)}</p>
      )}
      <div className="actions">
        {server.environment_url && LIVE.includes(server.state) && (
          <a
            href={server.environment_url}
            target="_blank"
            rel="noopener noreferrer"
            className={primary === "openSession" ? "button primary" : "button"}
          >
            {m.openSession}
          </a>
        )}
        {actions.map((action) => (
          <button
            key={action}
            type="button"
            className={action === primary ? "primary" : "secondary"}
            disabled={busy}
            // Show screen discloses the screen below: a fixed name, with its state in aria-expanded
            aria-expanded={action === "showScreen" ? showScreen : undefined}
            aria-controls={action === "showScreen" ? screenId : undefined}
            onClick={() => act(action)}
          >
            {LABELS[action]}
            {action === "showScreen" && <Caret />}
          </button>
        ))}
      </div>
      {server.screen && (
        <figure id={screenId} aria-label={m.screenTitle(workspace.name)} hidden={!showScreen}>
          <pre className="screen">{server.screen.join("\n")}</pre>
        </figure>
      )}
      <ProblemView error={error} />
    </article>
  );
}
