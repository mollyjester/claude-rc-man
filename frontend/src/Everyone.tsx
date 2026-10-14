import { useEffect, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { StateBadge } from "./Servers";

type Workspace = {
  id: number;
  name: string;
  state: string;
  server: { state: string; session_name: string | null; exit_code: number | null } | null;
  server_error: { code: string; title: string } | null;
};

export type Person = { email: string; workspaces: Workspace[] };

// A workspace's state as this page shows it: while it is not ready, that; once it
// is, its server's state, why it could not be read, or that it has none.
function StateOf({ workspace }: { workspace: Workspace }) {
  const { state, server, server_error: unread } = workspace;
  if (state !== "ready") return <>{m.workspaceStates[state] ?? state}</>;
  if (unread) return <ProblemView error={new ApiError(0, unread.code, unread.title)} />;
  if (server) return <StateBadge state={server.state} />;
  return <>{m.everyoneNoServer}</>;
}

// Everyone's workspaces, for administrators, to look at only: the server lets
// no one act on another person's workspace, so nothing here offers to.
function PersonWorkspaces({ person }: { person: Person }) {
  return (
    <section aria-label={person.email}>
      <h2>{person.email}</h2>
      {person.workspaces.length === 0 && <p>{m.everyoneNone}</p>}
      {person.workspaces.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>{m.everyoneWorkspace}</th>
              <th>{m.everyoneState}</th>
              <th>{m.sessionName}</th>
            </tr>
          </thead>
          <tbody>
            {person.workspaces.map((workspace) => (
              <tr key={workspace.id}>
                <td>{workspace.name}</td>
                <td>
                  <StateOf workspace={workspace} />
                </td>
                <td>
                  {workspace.server?.session_name && <code>{workspace.server.session_name}</code>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
    <section>
      <h1>{m.everyoneTitle}</h1>
      <p>{m.everyoneIntro}</p>
      <ProblemView error={error} />
      {people === null && !error && <p>{m.loading}</p>}
      {people?.map((person) => (
        <PersonWorkspaces key={person.email} person={person} />
      ))}
    </section>
  );
}
