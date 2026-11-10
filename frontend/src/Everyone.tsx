import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { POWERING, StateBadge, WorkspaceStateBadge } from "./Servers";
import { StopWorkspace } from "./StopWorkspace";

type Workspace = {
  id: number;
  name: string;
  device: string;
  state: string;
  server: { state: string; session_name: string | null; exit_code: number | null } | null;
  server_error: { code: string; title: string } | null;
  claude_version: string | null;
  // Whether an administrator may stop it now, and whether a Stop of it is under way.
  stoppable: boolean;
  stopping: boolean;
};

export type Person = { email: string; workspaces: Workspace[] };

// A workspace's state as this page shows it, as a pill: while it is not ready, that; while it is
// being stopped or started, that; once it is ready, its server's state, or that it has none; why
// its server could not be read is said in the row instead.
function StateOf({ workspace }: { workspace: Workspace }) {
  const { state, server } = workspace;
  // the entry says whether it is being stopped; that it is being started, its server's code
  const starting = workspace.server_error?.code === "workspace_starting";
  if (state !== "ready" || workspace.stopping || starting)
    return <WorkspaceStateBadge workspace={{ state, stopping: workspace.stopping, starting }} />;
  if (workspace.server_error) return null;
  if (server) return <StateBadge state={server.state} />;
  return <span className="badge badge-stopped">{m.everyoneNoServer}</span>;
}

// Everyone's workspaces, for administrators, to look at: an administrator's Stop of another
// person's workspace is the one action this page offers.
// Once a workspace is stopped its Stop workspace goes, so focus goes to the person's heading.
function PersonWorkspaces({
  person,
  onStopped,
  onChange,
}: {
  person: Person;
  onStopped: (workspace: Workspace) => void;
  onChange: () => void;
}) {
  const heading = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  return (
    <section aria-labelledby={heading} className="card flush person">
      <div className="card-head">
        <h2 id={heading} tabIndex={-1} ref={headingRef}>
          {person.email}
        </h2>
        <span className="muted">{m.workspaceCount(person.workspaces.length)}</span>
      </div>
      {person.workspaces.length === 0 && <p className="card-empty muted">{m.everyoneNone}</p>}
      {person.workspaces.length > 0 && (
        <ul className="person-workspaces">
          {person.workspaces.map((workspace) => {
            const error = workspace.server_error;
            const unread =
              workspace.state === "ready" && error && !POWERING.includes(error.code) ? error : null;
            return (
              <li key={workspace.id} className="list-row">
                <span className="ws-name">{workspace.name}</span>
                <code className="muted" translate="no" title={m.device}>
                  {workspace.device}
                </code>
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
                <StopWorkspace<Workspace>
                  workspace={workspace}
                  stoppable={workspace.stoppable === true}
                  stopping={workspace.stopping === true}
                  focusAfter={headingRef}
                  onStopped={onStopped}
                  onFailed={onChange}
                />
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

  const load = useCallback(() => {
    api.get<{ people: Person[] }>("/api/servers/everyone").then(
      (r) => {
        setPeople(r.people);
        setError(null);
      },
      (e) => setError(asApiError(e)),
    );
  }, []);
  useEffect(load, [load]);

  // A workspace stopped, as the answer gave it, in its row's place, and everyone read again.
  function stopped(workspace: Workspace) {
    setPeople(
      (current) =>
        current?.map((person) => ({
          ...person,
          workspaces: person.workspaces.map((w) => (w.id === workspace.id ? workspace : w)),
        })) ?? null,
    );
    load();
  }

  return (
    <section className="cards">
      <div className="page-head">
        <h1>{m.everyoneTitle}</h1>
      </div>
      <p className="page-intro muted">{m.everyoneIntro}</p>
      <ProblemView error={error} />
      {people === null && !error && <p>{m.loading}</p>}
      {people?.map((person) => (
        <PersonWorkspaces key={person.email} person={person} onStopped={stopped} onChange={load} />
      ))}
    </section>
  );
}
