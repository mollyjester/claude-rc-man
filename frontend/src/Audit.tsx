import { useEffect, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type Event = {
  id: number;
  at: string;
  email: string;
  linux: string;
  action: string;
  target: string | null;
  result: string;
};

export function Audit() {
  const [events, setEvents] = useState<Event[]>([]);
  const [more, setMore] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  async function load(before?: number) {
    try {
      const page = (await api.get<{ events: Event[] }>("/api/audit", { before })).events;
      setEvents((current) => (before ? [...current, ...page] : page));
      setMore(page.length > 0);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <section>
      <h1>{m.auditTitle}</h1>
      {events.length === 0 && <p>{m.auditEmpty}</p>}
      <table>
        <thead>
          <tr>
            <th>{m.auditWhen}</th>
            <th>{m.auditWho}</th>
            <th>{m.auditAction}</th>
            <th>{m.auditTarget}</th>
            <th>{m.auditResult}</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td>{new Date(e.at).toLocaleString()}</td>
              <td>
                {e.email} ({e.linux})
              </td>
              <td>{e.action}</td>
              <td>{e.target}</td>
              <td>{e.result}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {more && events.length > 0 && (
        <button type="button" onClick={() => load(events[events.length - 1].id)}>
          {m.auditOlder}
        </button>
      )}
      <ProblemView error={error} />
    </section>
  );
}
