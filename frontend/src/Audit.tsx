import { useCallback, useEffect, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { TableScroll } from "./TableScroll";

type Event = {
  id: number;
  at: string;
  email: string;
  action: string;
  target: string | null;
  result: string;
};

// A moment as a date and a time in UTC, the same for everyone; what cannot be read as one is
// shown as it is.
function when(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toISOString().slice(0, 19).replace("T", " ");
}

// A target as it was recorded, except that the settings someone changed, which are recorded as
// a JSON object, are read as a list of each setting and what it was set to.
function targetText(target: string): string {
  if (!target.startsWith("{")) return target;
  try {
    const sent: unknown = JSON.parse(target);
    if (typeof sent !== "object" || sent === null || Array.isArray(sent)) return target;
    return Object.entries(sent)
      .map(([name, value]) => `${name}: ${String(value)}`)
      .join(", ");
  } catch {
    return target;
  }
}

// A result: what the audit guide says it is, or a failure with the code the request failed with.
function Result({ result }: { result: string }) {
  const said = m.auditResults[result];
  if (said !== undefined) return <>{said}</>;
  return (
    <>
      <strong>{m.auditFailed}</strong> <code>{result}</code>
    </>
  );
}

export function Audit() {
  const [events, setEvents] = useState<Event[]>([]);
  const [more, setMore] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  // A page of events, the newest or those older than `before`; it reads nothing from a render,
  // so it is made once.
  const load = useCallback(async (before?: number) => {
    try {
      const page = (await api.get<{ events: Event[] }>("/api/audit", { before })).events;
      setEvents((current) => (before ? [...current, ...page] : page));
      setMore(page.length > 0);
    } catch (e) {
      setError(asApiError(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section>
      <h1>{m.auditTitle}</h1>
      {events.length === 0 && <p>{m.auditEmpty}</p>}
      <TableScroll label={m.tableTitle}>
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
                <td className="when">
                  <time dateTime={e.at}>{when(e.at)}</time>
                </td>
                <td className="wrap">{e.email}</td>
                <td title={e.action}>{m.auditActions[e.action] ?? e.action}</td>
                <td className="wrap">
                  {e.target === null ? m.auditNoTarget : targetText(e.target)}
                </td>
                <td
                  className={m.auditResults[e.result] === undefined ? "result-failed" : undefined}
                >
                  <Result result={e.result} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      {more && events.length > 0 && (
        <button type="button" onClick={() => load(events[events.length - 1].id)}>
          {m.auditOlder}
        </button>
      )}
      <ProblemView error={error} />
    </section>
  );
}
