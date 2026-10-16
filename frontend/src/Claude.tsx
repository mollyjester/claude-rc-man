import { type FormEvent, useEffect, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";

export const CONNECTION_STATES = ["not_connected", "connected", "expiring", "expired"] as const;
export type ConnectionState = (typeof CONNECTION_STATES)[number];

export type Connection = {
  connection: ConnectionState;
  account: {
    email: string | null;
    org_name: string | null;
    subscription_type: string | null;
  } | null;
  login_expires_at: string | null;
  // A sign-out waits for the last running server to stop; no server starts meanwhile.
  signing_out: boolean;
  // The sign-in went away on its own, as when a refresh collided with another: the account is
  // then not connected.
  signed_out_unexpectedly?: boolean;
};

function when(iso: string) {
  return new Date(iso).toLocaleString();
}

function Connect({ onConnected }: { onConnected: (c: Connection) => void }) {
  const [email, setEmail] = useState("");
  const [url, setUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const fields = useFieldErrors(error);

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
        <form noValidate onSubmit={start} ref={fields.form}>
          <label>
            {m.connectEmail}
            <input
              type="text"
              name="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              {...fields.input("email")}
            />
          </label>
          {fields.message("email")}
          <button type="submit" className="primary">
            {m.connectGo}
          </button>
        </form>
      ) : (
        <form noValidate onSubmit={finish} ref={fields.form}>
          <p>{m.connectStep}</p>
          <p>
            <a href={url} target="_blank" rel="noopener noreferrer">
              {m.connectLink}
            </a>
          </p>
          <label>
            {m.connectCode}
            <input
              type="text"
              name="code"
              autoComplete="off"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              {...fields.input("code")}
            />
          </label>
          {fields.message("code")}
          <button type="submit" className="primary">
            {m.connectSubmit}
          </button>
        </form>
      )}
      <ProblemView
        error={error}
        labels={{ email: m.connectEmail, code: m.connectCode }}
        inline={url === null ? ["email"] : ["code"]}
      />
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
  const signingOut = connection.signing_out;
  let message: string;
  if (signingOut) message = m.claudeSigningOut;
  else if (state === "not_connected")
    message = connection.signed_out_unexpectedly
      ? m.claudeSignedOutUnexpectedly
      : m.claudeNotConnected;
  else if (state === "expired") message = m.claudeExpired;
  else if (state === "expiring") message = m.claudeExpiring(expires ? when(expires) : "");
  else message = m.claudeConnectedAs(connection.account?.email ?? "");
  const offer =
    state === "not_connected"
      ? m.connect
      : state === "connected" && !signingOut
        ? null
        : m.reconnect;

  return (
    <section aria-label={m.claudeTitle} className={`claude claude-${state}`}>
      <p>{message}</p>
      {state === "connected" && !signingOut && expires && (
        <p className="muted">{m.claudeExpires(when(expires))}</p>
      )}
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
      {state !== "not_connected" && !signingOut && !connecting && (
        <ClaudeSignOut onSignedOut={onChange} />
      )}
      {signingOut && !connecting && <SignOutNow onSignedOut={onChange} />}
    </section>
  );
}

// Signing out of Claude: running servers keep the account until the last one
// stops, unless the person asks for them to be stopped first.
function ClaudeSignOut({ onSignedOut }: { onSignedOut: (c: Connection) => void }) {
  const [keep, setKeep] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  async function signOut(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      onSignedOut(await api.post<Connection>("/api/claude/logout", { keep_sessions: keep }));
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <form noValidate onSubmit={signOut} className="sign-out">
      <label>
        <input
          type="checkbox"
          name="keep_sessions"
          checked={keep}
          onChange={(e) => setKeep(e.target.checked)}
        />
        {m.keepSessions}
      </label>
      <button type="submit" className="danger">
        {m.claudeSignOut}
      </button>
      <ProblemView error={error} labels={{ keep_sessions: m.keepSessions }} />
    </form>
  );
}

// A sign-out that waits for running servers is never stuck: it can be finished at
// once, stopping them.
function SignOutNow({ onSignedOut }: { onSignedOut: (c: Connection) => void }) {
  const [error, setError] = useState<ApiError | null>(null);

  async function signOutNow(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      onSignedOut(await api.post<Connection>("/api/claude/logout", { keep_sessions: false }));
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <form noValidate onSubmit={signOutNow} className="sign-out">
      <button type="submit" className="danger">
        {m.claudeSignOutNow}
      </button>
      <ProblemView error={error} />
    </form>
  );
}

// The Claude page: the account's state is read once, as the page opens.
export function ClaudePage() {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    api
      .get<Connection>("/api/claude")
      .then(setConnection)
      .catch((e) => setError(asApiError(e)));
  }, []);
  return (
    <section>
      <h1>{m.claudeTitle}</h1>
      <ProblemView error={error} />
      <ClaudeConnection connection={connection} onChange={setConnection} />
    </section>
  );
}
