import { useEffect, useId, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { StateBadge, WorkspaceBadge } from "./Servers";

type Workspace = {
  id: number;
  name: string;
  state: string;
  server: { state: string; session_name: string | null; exit_code: number | null } | null;
  server_error: { code: string; title: string } | null;
  claude_version: string | null;
};

export type Person = { email: string; workspaces: Workspace[] };

// A workspace's state as this page shows it, as a pill: while it is not ready, that; once it
// is, its server's state, or that it has none; why its server could not be read is said in
// the row instead.
function StateOf({ workspace }: { workspace: Workspace }) {
  const { state, server } = workspace;
  if (state !== "ready") return <WorkspaceBadge state={state} />;
  if (workspace.server_error) return null;
  if (server) return <StateBadge state={server.state} />;
  return <span className="badge badge-stopped">{m.everyoneNoServer}</span>;
}

// Everyone's workspaces, for administrators, to look at only: the server lets
// no one act on another person's workspace, so nothing here offers to.
function PersonWorkspaces({ person }: { person: Person }) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="card flush person">
      <div className="card-head">
        <h2 id={heading}>{person.email}</h2>
        <span className="muted">{m.workspaceCount(person.workspaces.length)}</span>
      </div>
      {person.workspaces.length === 0 && <p className="card-empty muted">{m.everyoneNone}</p>}
      {person.workspaces.length > 0 && (
        <ul className="person-workspaces">
          {person.workspaces.map((workspace) => {
            const unread = workspace.state === "ready" ? workspace.server_error : null;
            return (
              <li key={workspace.id} className="list-row">
                <span className="ws-name">{workspace.name}</span>
                <StateOf workspace={workspace} />
                {workspace.server?.session_name && (
                  <code className="muted" title={m.sessionName}>
                    {workspace.server.session_name}
                  </code>
                )}
                {workspace.claude_version && (
                  <span className="ws-version muted">
                    {m.serverClaudeVersion(workspace.claude_version)}
                  </span>
                )}
                {unread && (
                  <div className="ws-problem">
                    <ProblemView error={new ApiError(0, unread.code, unread.title)} />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export function Everyone() {
  const [people, setPeople] = useState<Person[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    api.get<{ people: Person[] }>("/api/servers/everyone").then(
      (r) => setPeople(r.people),
      (e) => setError(asApiError(e)),
    );
  }, []);

  return (
    <section className="cards">
      <div className="page-head">
        <h1>{m.everyoneTitle}</h1>
      </div>
      <p className="page-intro muted">{m.everyoneIntro}</p>
      <ProblemView error={error} />
      {people === null && !error && <p>{m.loading}</p>}
      {people?.map((person) => (
        <PersonWorkspaces key={person.email} person={person} />
      ))}
    </section>
  );
}
