import { useEffect, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { type Server, StateBadge } from "./Servers";

export type Person = {
  linux: string;
  email: string;
  servers?: Server[];
  error?: { code: string; title: string };
};

// Everyone's servers, for administrators, to look at only: the server lets
// no one act on another person's servers, so nothing here offers to.
function PersonServers({ person }: { person: Person }) {
  const name = m.everyonePerson(person.linux, person.email);
  const error = person.error ? new ApiError(0, person.error.code, person.error.title) : null;
  return (
    <section aria-label={name}>
      <h2>{name}</h2>
      <ProblemView error={error} />
      {person.servers && person.servers.length === 0 && <p>{m.everyoneNone}</p>}
      {person.servers && person.servers.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>{m.everyoneFolder}</th>
              <th>{m.everyoneState}</th>
              <th>{m.sessionName}</th>
            </tr>
          </thead>
          <tbody>
            {person.servers.map((server) => (
              <tr key={server.path}>
                <td>{server.path}</td>
                <td>
                  <StateBadge state={server.state} />
                </td>
                <td>{server.session_name && <code>{server.session_name}</code>}</td>
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
        <PersonServers key={person.linux} person={person} />
      ))}
    </section>
  );
}
