import { useCallback, useEffect, useState } from "react";

import { type ApiError, api } from "./api";
import { utcMoment } from "./format";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { ROLE, TableScroll } from "./TableScroll";

type Event = {
  id: number;
  at: string;
  email: string;
  action: string;
  target: string | null;
  result: string;
};

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
      <span className="badge badge-problem">{m.auditFailed}</span> <code>{result}</code>
    </>
  );
}

export function Audit() {
  const [events, setEvents] = useState<Event[]>([]);
  const [more, setMore] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  // A page of events, the newest or those older than `before`; it reads nothing from a render,
  // so it is made once.
  const load = useCallback(async (before?: number) => {
    try {
      const page = (await api.get<{ events: Event[] }>("/api/audit", { before })).events;
      setEvents((current) => (before ? [...current, ...page] : page));
      setMore(page.length > 0);
      setLoaded(true);
    } catch (e) {
      setError(asApiError(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section>
      <div className="page-head">
        <h1>{m.auditTitle}</h1>
      </div>
      <ProblemView error={error} />
      {!loaded && !error && <p>{m.loading}</p>}
      {loaded && (
        <div className="card flush">
          {events.length === 0 && <p className="card-empty muted">{m.auditEmpty}</p>}
          {events.length > 0 && (
            <TableScroll label={m.tableTitle}>
              {/* on a phone a list of rows: the action and its result, who and on what, when */}
              <table className="stacked audit" {...ROLE.table}>
                <thead {...ROLE.rowgroup}>
                  <tr {...ROLE.row}>
                    <th {...ROLE.columnheader}>{m.auditWhen}</th>
                    <th {...ROLE.columnheader}>{m.auditWho}</th>
                    <th {...ROLE.columnheader}>{m.auditAction}</th>
                    <th {...ROLE.columnheader}>{m.auditTarget}</th>
                    <th {...ROLE.columnheader}>{m.auditResult}</th>
                  </tr>
                </thead>
                <tbody {...ROLE.rowgroup}>
                  {events.map((e) => (
                    <tr key={e.id} {...ROLE.row}>
                      <td className="when" {...ROLE.cell}>
                        <time dateTime={e.at}>{utcMoment(e.at)}</time>
                      </td>
                      <td className="wrap meta" {...ROLE.cell}>
                        {e.email}
                      </td>
                      <td className="lead" title={e.action} {...ROLE.cell}>
                        {m.auditActions[e.action] ?? e.action}
                      </td>
                      <td className="wrap meta" {...ROLE.cell}>
                        {e.target === null ? (
                          <span className="muted">{m.auditNoTarget}</span>
                        ) : (
                          targetText(e.target)
                        )}
                      </td>
                      <td
                        className={
                          m.auditResults[e.result] === undefined ? "result result-failed" : "result"
                        }
                        {...ROLE.cell}
                      >
                        <Result result={e.result} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
          {more && events.length > 0 && (
            <div className="card-foot">
              <button type="button" onClick={() => load(events[events.length - 1].id)}>
                {m.auditOlder}
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
