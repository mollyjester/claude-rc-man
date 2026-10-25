import { useId, useState } from "react";

import { type ApiError, api } from "./api";
import { Caret } from "./Caret";
import { MessageBar } from "./MessageBar";
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

type Action =
  | "stop"
  | "resume"
  | "startAgain"
  | "forget"
  | "showScreen"
  | "trustYes"
  | "trustNo"
  | "enableRc"
  | "notNow";

// What each state of a server allows; the server refuses anything else.
export const ACTIONS: Record<string, Action[]> = {
  starting: ["stop"],
  trust_prompt: ["trustYes", "trustNo", "stop"],
  remote_control_prompt: ["enableRc", "notNow", "stop"],
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
  remote_control_prompt: "waiting",
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
  remote_control_prompt: "enableRc",
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
  enableRc: ["/api/servers/confirm", { answer: "y" }],
  notNow: ["/api/servers/confirm", { answer: "n" }],
};

export const LABELS: Record<Action, string> = {
  stop: m.stop,
  resume: m.resume,
  startAgain: m.startAgain,
  forget: m.forget,
  showScreen: m.showScreen,
  trustYes: m.trustYes,
  trustNo: m.trustNo,
  enableRc: m.enableRc,
  notNow: m.notNow,
};

// What a workspace is, as the page lists it: its server, or why that could not be read.
export type Workspace = {
  id: number;
  name: string;
  // The name claude.ai and the Claude app give its server, its container's hostname.
  device: string;
  state: "creating" | "ready" | "failed" | "deleting";
  error: string | null;
  // The address it is cloned from, null for an empty one; and the steps taken while it is
  // made, one to a line, null once it is ready.
  source: string | null;
  // The GitHub page of the repository it is cloned from, as the server derives it; null
  // where it has none.
  source_page: string | null;
  log: string | null;
  server: Server | null;
  server_error: { code: string; title: string } | null;
  // The Claude Code version its server started with, null while it has none running.
  claude_version: string | null;
  // Its rebuild: the phase one is in (preparing, switching, promoting), null with none under
  // way; the steps of the last one; and why the last one did not finish, null when it did.
  rebuild: Rebuild;
  // Whether a rebuild may be asked for now, as the server judges it.
  rebuildable: boolean;
  // The ports it forwards, each with its label and its address, as the server gives them: none
  // while forwarding is off, nor for a workspace that is not ready.
  ports: ForwardedPort[];
};

export type ForwardedPort = { port: number; label: string | null; url: string };

export type Rebuild = { state: string | null; log: string | null; error: string | null };

// What the server reads while a rebuild replaces a workspace's container: not its server.
export const REBUILDING = "workspace_rebuilding";

// The dot a workspace carries: its server's kind, a problem where the workspace
// failed or its server could not be read, and none otherwise, nor while a rebuild replaces
// its container.
export function dotFor(workspace: Workspace): Kind | null {
  if (workspace.server_error?.code === REBUILDING && workspace.state !== "failed") return null;
  if (workspace.state === "failed" || workspace.server_error) return "problem";
  return workspace.server ? kindOf(workspace.server.state) : null;
}

// The kind of pill a workspace's own state is drawn as.
export const WORKSPACE_KIND: Record<string, Kind> = {
  creating: "waiting",
  ready: "running",
  failed: "problem",
  deleting: "stopped",
};

// A workspace's own state as a pill, in the words of m.workspaceStates.
export function WorkspaceBadge({ state }: { state: string }) {
  return (
    <span className={`badge badge-${WORKSPACE_KIND[state] ?? "problem"}`}>
      {m.workspaceStates[state] ?? state}
    </span>
  );
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
      <div className="server-head">
        <StateBadge state={server.state} />
        {claudeVersion && <span className="muted">{m.serverClaudeVersion(claudeVersion)}</span>}
      </div>
      <p>{m.nextStep[server.state] ?? m.nextStepUnknown}</p>
      {server.state === "trust_prompt" && (
        <MessageBar kind="info">
          <p>
            <strong>{m.trustTitle}</strong>
          </p>
          <p>{m.trustBody(workspace.name)}</p>
        </MessageBar>
      )}
      {server.state === "remote_control_prompt" && (
        <MessageBar kind="info">
          <p>
            <strong>{m.remoteControlTitle}</strong>
          </p>
          <p>{m.remoteControlBody}</p>
        </MessageBar>
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
