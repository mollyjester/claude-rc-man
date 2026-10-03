import { useId, useState } from "react";

import { type ApiError, api } from "./api";
import { Caret } from "./Caret";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

export type Server = {
  path: string;
  state: string;
  environment_url: string | null;
  session_name: string | null;
  exit_code: number | null;
  screen?: string[];
};

type Action = "stop" | "resume" | "startAgain" | "forget" | "showScreen" | "trustYes" | "trustNo";

// The actions that start Claude, which the server refuses while the account cannot.
const STARTS = ["resume", "startAgain"] as const satisfies readonly Action[];
type StartAction = (typeof STARTS)[number];
const startsClaude = (action: Action): action is StartAction =>
  (STARTS as readonly Action[]).includes(action);

// Why nothing can start Claude, said in place of each control that would: Start
// in a folder with no server, and a server's own actions that start it again.
export type StartBlocked = Record<"start" | StartAction, string>;

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

// The servers in a folder: its own, and every one in a folder inside it.
export function serversIn(path: string, servers: Server[]): Server[] {
  return servers.filter((s) => s.path === path || s.path.startsWith(`${path}/`));
}

// The dot a folder carries: the most urgent kind among its own server and,
// while includeBelow, every server inside it; none where there is no server.
export function dotFor(path: string, servers: Server[], includeBelow: boolean): Kind | null {
  const counted = includeBelow ? serversIn(path, servers) : servers.filter((s) => s.path === path);
  const kinds = new Set(counted.map((s) => kindOf(s.state)));
  return KIND_ORDER.find((kind) => kinds.has(kind)) ?? null;
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

// A server and the actions its state allows; startReason, when there is one,
// is why those that start Claude are not offered.
export function ServerCard({
  server,
  startReason,
  onChange,
}: {
  server: Server;
  startReason: StartBlocked | null;
  onChange: () => void;
}) {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [showScreen, setShowScreen] = useState(false);
  const screenId = useId();
  const allowed = ACTIONS[server.state] ?? ["showScreen"];
  // The actions that would start Claude, when the account cannot: they are not offered.
  const blocked = startReason ? allowed.filter(startsClaude) : [];
  const actions = allowed.filter((action) => !(blocked as Action[]).includes(action));
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
      <p>
        {startReason && blocked.length > 0
          ? blocked.map((action) => startReason[action]).join(" ")
          : (m.nextStep[server.state] ?? m.nextStepUnknown)}
      </p>
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
        <pre
          id={screenId}
          className="screen"
          aria-label={m.screenTitle(server.path)}
          hidden={!showScreen}
        >
          {server.screen.join("\n")}
        </pre>
      )}
      <ProblemView error={error} />
    </article>
  );
}
