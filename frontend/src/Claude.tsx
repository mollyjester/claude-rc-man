import { type FormEvent, type ReactNode, useEffect, useId, useState } from "react";

import { type ApiError, api } from "./api";
import { MessageBar, type MessageKind } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import type { Kind } from "./Servers";

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
  const help = useId();

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
              {...fields.input("email", help)}
            />
          </label>
          {fields.message("email")}
          <p id={help} className="help muted">
            {m.connectEmailHelp}
          </p>
          <div className="actions">
            <button type="submit" className="primary">
              {m.connectGo}
            </button>
          </div>
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
          <div className="actions">
            <button type="submit" className="primary">
              {m.connectSubmit}
            </button>
          </div>
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

// What the sheet says of the account's state: its pill, and the message bar it needs, if any.
function standing(connection: Connection): {
  state: string;
  kind: Kind;
  bar: { kind: MessageKind; text: string } | null;
} {
  const { connection: state, login_expires_at: expires } = connection;
  if (connection.signing_out)
    return {
      state: "signing_out",
      kind: "waiting",
      bar: { kind: "warning", text: m.claudeSigningOut },
    };
  if (state === "not_connected")
    return {
      state,
      kind: "stopped",
      bar: connection.signed_out_unexpectedly
        ? { kind: "warning", text: m.claudeSignedOutUnexpectedly }
        : { kind: "info", text: m.claudeNotConnected },
    };
  if (state === "expiring")
    return {
      state,
      kind: "waiting",
      bar: { kind: "warning", text: m.claudeExpiring(expires ? when(expires) : "") },
    };
  if (state === "expired")
    return { state, kind: "problem", bar: { kind: "problem", text: m.claudeExpired } };
  return { state, kind: "running", bar: null };
}

// The account as a sheet in a card: its address as the title with its state as a pill, what
// to know as a message bar, its facts as rows, the offer to connect, and the sign-out in a
// section of its own at the foot.
export function ClaudeConnection({
  connection,
  onChange,
}: {
  connection: Connection | null;
  onChange: (c: Connection) => void;
}) {
  const [connecting, setConnecting] = useState(false);
  const title = useId();
  if (!connection) return null;
  const { state, kind, bar } = standing(connection);
  const account = connection.account;
  const expires = connection.login_expires_at;
  const signingOut = connection.signing_out;
  const offer =
    connection.connection === "not_connected"
      ? m.connect
      : connection.connection === "connected" && !signingOut
        ? null
        : m.reconnect;
  const named = connection.connection !== "not_connected" && account?.email;

  return (
    <section aria-labelledby={title} className="card form-sheet claude-sheet">
      <div className="sheet-head">
        <h2 id={title}>{named ? account.email : m.claudeNoAccount}</h2>
        {named && <span className={`badge badge-${kind}`}>{m.claudeStates[state]}</span>}
      </div>
      {bar && (
        <MessageBar kind={bar.kind}>
          <p>{bar.text}</p>
        </MessageBar>
      )}
      {named && (
        <dl className="facts">
          <dt>{m.claudeOrg}</dt>
          <dd>{account.org_name ?? m.claudeUnknown}</dd>
          <dt>{m.claudePlan}</dt>
          <dd>{account.subscription_type ?? m.claudeUnknown}</dd>
          <dt>{m.claudeExpiresAt}</dt>
          <dd>{expires ? when(expires) : m.claudeUnknown}</dd>
        </dl>
      )}
      {offer && !connecting && (
        <div className="actions sheet-actions">
          <button type="button" className="primary" onClick={() => setConnecting(true)}>
            {offer}
          </button>
        </div>
      )}
      {connecting && (
        <Connect
          onConnected={(c) => {
            setConnecting(false);
            onChange(c);
          }}
        />
      )}
      {(connection.connection !== "not_connected" || signingOut) && !connecting && (
        <SignOutZone>
          {signingOut ? (
            <SignOutNow onSignedOut={onChange} />
          ) : (
            <ClaudeSignOut onSignedOut={onChange} />
          )}
        </SignOutZone>
      )}
    </section>
  );
}

// The sign-out, at the sheet's foot under a stroke: what cannot be undone, set apart.
function SignOutZone({ children }: { children: ReactNode }) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="danger-zone">
      <div className="danger-intro">
        <h3 id={heading}>{m.claudeSignOutTitle}</h3>
        <p className="muted">{m.claudeSignOutLine}</p>
      </div>
      {children}
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
      <div className="page-head">
        <h1>{m.claudeTitle}</h1>
      </div>
      <ProblemView error={error} />
      <ClaudeConnection connection={connection} onChange={setConnection} />
    </section>
  );
}
