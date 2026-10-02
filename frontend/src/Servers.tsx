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
const LIVE = ["connected", "ready"];

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

function ServerCard({ server, onChange }: { server: Server; onChange: () => void }) {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [showScreen, setShowScreen] = useState(false);
  const actions = ACTIONS[server.state] ?? ["showScreen"];

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
    <article aria-label={server.path} className={`server state-${server.state}`}>
      <h3>{server.path}</h3>
      <p className="state">{m.states[server.state] ?? server.state}</p>
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
          <a href={server.environment_url} target="_blank" rel="noopener noreferrer">
            {m.openSession}
          </a>
        </p>
      )}
      {server.exit_code !== null && server.state === "exited" && <p>{m.exitCode(server.exit_code)}</p>}
      <div className="actions">
        {actions.map((action) => (
          <button key={action} type="button" disabled={busy} onClick={() => act(action)}>
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

export function ServerList({ servers, onChange }: { servers: Server[]; onChange: () => void }) {
  return (
    <section aria-label={m.serversTitle}>
      <h2>{m.serversTitle}</h2>
      {servers.length === 0 && <p>{m.serversEmpty}</p>}
      {servers.map((server) => (
        <ServerCard key={server.path} server={server} onChange={onChange} />
      ))}
    </section>
  );
}

type Entry = { name: string; path: string; has_children: boolean };

function FolderNode({
  entry,
  running,
  onStart,
}: {
  entry: Entry;
  running: Set<string>;
  onStart: (path: string) => void;
}) {
  const [children, setChildren] = useState<Entry[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  async function toggle() {
    if (children) {
      setChildren(null);
      return;
    }
    try {
      setChildren((await api.get<{ entries: Entry[] }>("/api/folders", { path: entry.path })).entries);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <li>
      {entry.has_children ? (
        <button
          type="button"
          className="toggle"
          aria-label={children ? m.collapse(entry.name) : m.expand(entry.name)}
          onClick={toggle}
        >
          {children ? "▾" : "▸"}
        </button>
      ) : (
        <span className="toggle" />
      )}
      <span className="name">{entry.name}</span>
      {!running.has(entry.path) && (
        <button type="button" aria-label={m.startIn(entry.path)} onClick={() => onStart(entry.path)}>
          {m.start}
        </button>
      )}
      <ProblemView error={error} />
      {children && (
        <ul>
          {children.length === 0 && <li>{m.foldersEmpty}</li>}
          {children.map((child) => (
            <FolderNode key={child.path} entry={child} running={running} onStart={onStart} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function FolderTree({
  entries,
  running,
  onStart,
}: {
  entries: Entry[];
  running: Set<string>;
  onStart: (path: string) => void;
}) {
  return (
    <section aria-label={m.foldersTitle}>
      <h2>{m.foldersTitle}</h2>
      {entries.length === 0 && <p>{m.foldersEmpty}</p>}
      <ul className="tree">
        {entries.map((entry) => (
          <FolderNode key={entry.path} entry={entry} running={running} onStart={onStart} />
        ))}
      </ul>
    </section>
  );
}
