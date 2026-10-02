import { type FormEvent, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";
import type { StartAction } from "./Servers";

export const CONNECTION_STATES = ["not_connected", "connected", "expiring", "expired"] as const;
export type ConnectionState = (typeof CONNECTION_STATES)[number];

// Why nothing can start Claude, said in place of each control that would: Start
// in a folder with no server, and a server's own actions that start it again.
export type StartBlocked = Record<"start" | StartAction, string>;

// Why no server can start while the account is in each state; null where one
// can. rc-helper starts one with an expired sign-in, and the account's section
// already warns about it.
export const START_REASON: Record<ConnectionState, StartBlocked | null> = {
  not_connected: m.notConnected,
  connected: null,
  expiring: null,
  expired: null,
};

export type Connection = {
  connection: ConnectionState;
  account: { email: string | null; org_name: string | null; subscription_type: string | null } | null;
  login_expires_at: string | null;
};

function when(iso: string) {
  return new Date(iso).toLocaleString();
}

function Connect({ onConnected }: { onConnected: (c: Connection) => void }) {
  const [email, setEmail] = useState("");
  const [url, setUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<ApiError | null>(null);

  async function start(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const reply = await api.post<{ url: string }>("/api/claude/login", email ? { email } : {});
      setUrl(reply.url);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  async function finish(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      onConnected(await api.post<Connection>("/api/claude/code", { code }));
    } catch (e) {
      setError(asApiError(e));
      setUrl(null); // a code works once and the sign-in has ended: start again
      setCode("");
    }
  }

  return (
    <div className="connect">
      {url === null ? (
        <form noValidate onSubmit={start}>
          <label>
            {m.connectEmail}
            <input type="text" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <button type="submit">{m.connectGo}</button>
        </form>
      ) : (
        <form noValidate onSubmit={finish}>
          <p>{m.connectStep}</p>
          <p>
            <a href={url} target="_blank" rel="noopener noreferrer">
              {m.connectLink}
            </a>
          </p>
          <label>
            {m.connectCode}
            <input type="text" autoComplete="off" value={code} onChange={(e) => setCode(e.target.value)} />
          </label>
          <button type="submit">{m.connectSubmit}</button>
        </form>
      )}
      <ProblemView error={error} />
    </div>
  );
}

export function ClaudeConnection({
  connection,
  onChange,
}: {
  connection: Connection | null;
  onChange: (c: Connection) => void;
}) {
  const [connecting, setConnecting] = useState(false);
  if (!connection) return null;
  const state = connection.connection;
  const expires = connection.login_expires_at;
  let message: string;
  if (state === "not_connected") message = m.claudeNotConnected;
  else if (state === "expired") message = m.claudeExpired;
  else if (state === "expiring") message = m.claudeExpiring(expires ? when(expires) : "");
  else message = m.claudeConnectedAs(connection.account?.email ?? "");
  const offer = state === "not_connected" ? m.connect : state === "connected" ? null : m.reconnect;

  return (
    <section aria-label={m.claudeTitle} className={`claude claude-${state}`}>
      <p>{message}</p>
      {state === "connected" && expires && <p className="muted">{m.claudeExpires(when(expires))}</p>}
      {offer && !connecting && (
        <button type="button" onClick={() => setConnecting(true)}>
          {offer}
        </button>
      )}
      {connecting && (
        <Connect
          onConnected={(c) => {
            setConnecting(false);
            onChange(c);
          }}
        />
      )}
    </section>
  );
}
