import { Fragment, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ADMIN } from "./AdminPages";
import { type ApiError, api } from "./api";
import { formatBytes, formatCount, formatPercent, formatSeconds, utcMoment } from "./format";
import { MonitorIcon } from "./Icons";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { WorkspaceBadge } from "./Servers";
import { ROLE, TableScroll } from "./TableScroll";

// What GET /api/monitor answers (backend/rcman/routers/monitor.py). A figure not known is null,
// never 0; a scope that is off is null.
type Usage = { cpu_percent: number | null; memory_bytes: number | null; tasks: number | null };
type PersonFigures = Usage & {
  id: number;
  email: string;
  workspaces: number;
  disk_bytes: number | null;
};
type ContainerFigures = Usage & {
  id: number;
  name: string;
  owner: string;
  state: string;
  disk_bytes: number | null;
};
type Filesystem = {
  path: string;
  roles: string[];
  size_bytes: number | null;
  used_bytes: number | null;
  available_bytes: number | null;
};
type Host = {
  cpu_percent: number | null;
  cpus: number | null;
  memory_used_bytes: number | null;
  memory_total_bytes: number | null;
  tasks: number | null;
  disks: Filesystem[];
};
type Round = { collected_at: string | null; error: string | null };
type Snapshot = {
  usage: Round;
  disk: Round;
  host: Host | null;
  people: PersonFigures[] | null;
  containers: ContainerFigures[] | null;
};

// The monitor's settings, as GET /api/monitor/settings has them: the page reads them only to
// say how often each round runs. They are changed on the Service settings page
// (src/ServiceSettings.tsx).
type Settings = { usage_interval_seconds: number; disk_interval_seconds: number };

// How often the page reads what is known again: it reads what rcman collected, and collects
// nothing itself, so a read costs rcman little.
export const MONITOR_POLL_MS = 5000;
// The Monitor page, for administrators: what the host, each person and each workspace container
// use now, as rcman last collected it, read again every MONITOR_POLL_MS; and how often each
// round runs, from the monitor's settings, read once.
export function Monitor() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  // Why the last read of the figures failed; the figures read before stay shown.
  const [readError, setReadError] = useState<ApiError | null>(null);
  // The settings as saved, for the live region to say how often each round runs.
  const [saved, setSaved] = useState<Settings | null>(null);
  const [settingsError, setSettingsError] = useState<ApiError | null>(null);
  // The read that is out, settled either way, which a read waits for: reads never overlap.
  const inflight = useRef<Promise<unknown> | null>(null);

  useEffect(() => {
    api.get<{ settings: Settings }>("/api/monitor/settings").then(
      (answer) => setSaved(answer.settings),
      (e) => setSettingsError(asApiError(e)),
    );
  }, []);

  // The figures, read now and then again a while after each answer, a read out already waited
  // for first, so that reads never overlap. The read as the page opens notes its failure for
  // rcman; every later one is quiet: its failure is shown here.
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async (first: boolean) => {
      await inflight.current;
      if (stopped) return;
      const request = first
        ? api.get<Snapshot>("/api/monitor")
        : api.poll<Snapshot>("/api/monitor");
      inflight.current = request.then(
        () => undefined,
        () => undefined,
      );
      try {
        const answer = await request;
        if (stopped) return;
        setSnapshot(answer);
        setReadError(null);
      } catch (e) {
        if (stopped) return;
        setReadError(asApiError(e));
      }
      if (!stopped) timer = setTimeout(() => read(false), MONITOR_POLL_MS);
    };
    read(true);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, []);

  // The live region's words change only when a round collects, or the intervals saved change:
  // a read that finds the same times leaves it as it is.
  const usageAt = snapshot?.usage.collected_at;
  const diskAt = snapshot?.disk.collected_at;
  const usageEvery = saved?.usage_interval_seconds;
  const diskEvery = saved?.disk_interval_seconds;
  const collected = useMemo(() => {
    if (usageAt === undefined || diskAt === undefined) return "";
    const said = (round: string, at: string | null, every: number | undefined) =>
      m.monitorCollected(
        round,
        at === null ? null : utcMoment(at),
        every === undefined ? null : formatSeconds(every),
      );
    return `${said(m.monitorUsage, usageAt, usageEvery)} ${said(m.monitorDisk, diskAt, diskEvery)}`;
  }, [usageAt, diskAt, usageEvery, diskEvery]);

  return (
    <section className="cards">
      <div className="page-head">
        <h1>{m.monitorTitle}</h1>
      </div>
      <p className="page-intro muted">{m.monitorIntro}</p>
      {/* the live region of the collection: when each round last collected */}
      <p role="status" className="monitor-collected muted">
        {collected}
      </p>
      <ProblemView error={readError} />
      <ProblemView error={settingsError} />
      {snapshot?.usage.error && (
        <RoundFailed said={m.monitorUsageFailed} error={snapshot.usage.error} />
      )}
      {snapshot?.disk.error && (
        <RoundFailed said={m.monitorDiskFailed} error={snapshot.disk.error} />
      )}
      {snapshot === null && !readError && <p>{m.loading}</p>}
      {snapshot && (
        <>
          <HostCard host={snapshot.host} />
          <PeopleCard people={snapshot.people} />
          <ContainersCard containers={snapshot.containers} />
        </>
      )}
    </section>
  );
}

// A round whose last run failed: that the figures are the last read, and what it failed with,
// as the server said it.
function RoundFailed({ said, error }: { said: string; error: string }) {
  return (
    <MessageBar kind="problem">
      <p>{said}</p>
      <p className="monitor-error">{error}</p>
    </MessageBar>
  );
}

// A figure, or that it is not known.
const known = <T,>(value: T | null, shown: (value: T) => string) =>
  value === null ? m.monitorNotKnown : shown(value);

// A main figure, large and in tabular numerals, with what it is of beside it, quieter.
function Figure({ value, aside }: { value: string | null; aside?: string | null }) {
  if (value === null) return <span className="muted">{m.monitorNotKnown}</span>;
  return (
    <>
      <span className="figure">{value}</span>
      {aside && (
        <>
          {" "}
          <span className="muted">{aside}</span>
        </>
      )}
    </>
  );
}

// A part whose scope is switched off: what is not there, and the way to the page that holds the
// monitoring settings.
function Off({ scope }: { scope: string }) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <MonitorIcon />
      </span>
      <strong>{m.monitorOff}</strong>
      <p>{m.monitorOffBody(scope)}</p>
      <Link className="button" to={ADMIN.settings.path}>
        {m.monitorOffLink}
      </Link>
    </div>
  );
}

const LIST = new Intl.ListFormat("en", { type: "conjunction" });

function HostCard({ host }: { host: Host | null }) {
  const heading = useId();
  const filesystems = useId();
  const rows: [string, ReactNode][] =
    host === null
      ? []
      : [
          [
            m.monitorCpu,
            <Figure
              key="cpu"
              value={host.cpu_percent === null ? null : formatPercent(host.cpu_percent)}
              aside={host.cpus === null ? null : m.monitorCores(host.cpus)}
            />,
          ],
          [
            m.monitorMemory,
            <Figure
              key="memory"
              value={host.memory_used_bytes === null ? null : formatBytes(host.memory_used_bytes)}
              aside={
                host.memory_total_bytes === null
                  ? null
                  : m.monitorMemoryOf(formatBytes(host.memory_total_bytes))
              }
            />,
          ],
          [
            m.monitorProcesses,
            <Figure key="tasks" value={host.tasks === null ? null : formatCount(host.tasks)} />,
          ],
        ];
  return (
    <section aria-labelledby={heading} className="card monitor-host">
      <h2 id={heading} className="card-title">
        {m.monitorHost}
      </h2>
      {host === null ? (
        <Off scope={m.monitorHost} />
      ) : (
        <>
          <dl className="facts monitor-figures">
            {rows.map(([term, value]) => (
              <Fragment key={term}>
                <dt>{term}</dt>
                <dd>{value}</dd>
              </Fragment>
            ))}
          </dl>
          {host.disks.length > 0 && (
            <section aria-labelledby={filesystems}>
              <h3 id={filesystems} className="monitor-subhead">
                {m.monitorFilesystems}
              </h3>
              <dl className="facts monitor-figures">
                {host.disks.map((disk) => (
                  <Fragment key={`${disk.path}|${disk.roles.join()}`}>
                    <dt>
                      <code translate="no">{disk.path}</code>
                    </dt>
                    <dd>
                      <Figure
                        value={disk.used_bytes === null ? null : formatBytes(disk.used_bytes)}
                        aside={
                          disk.size_bytes === null || disk.available_bytes === null
                            ? null
                            : m.monitorDiskOf(
                                formatBytes(disk.size_bytes),
                                formatBytes(disk.available_bytes),
                              )
                        }
                      />{" "}
                      <span className="filesystem-of muted">
                        {m.monitorFilesystemOf(
                          LIST.format(disk.roles.map((role) => m.monitorRoles[role] ?? role)),
                        )}
                      </span>
                    </dd>
                  </Fragment>
                ))}
              </dl>
            </section>
          )}
        </>
      )}
    </section>
  );
}

// A table in a card of its own, named by its heading, or the empty state of a scope that is off.
function TableCard({ title, off, children }: { title: string; off: boolean; children: ReactNode }) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="card flush">
      <div className="card-head">
        <h2 id={heading}>{title}</h2>
      </div>
      {off ? <Off scope={title} /> : children}
    </section>
  );
}

// The headings of the figures each table shows after its own columns.
function UsageHeadings() {
  return (
    <>
      <th className="number" {...ROLE.columnheader}>
        {m.monitorCpu}
      </th>
      <th className="number" {...ROLE.columnheader}>
        {m.monitorMemoryWithSwap}
      </th>
      <th className="number" {...ROLE.columnheader}>
        {m.monitorProcesses}
      </th>
      <th className="number" {...ROLE.columnheader}>
        {m.monitorDisk}
      </th>
    </>
  );
}

// A figure in a table, named by its column on the line a phone draws it in.
function Cell({ label, value }: { label: string; value: string }) {
  return (
    <td className="number meta named" data-label={label} {...ROLE.cell}>
      {value}
    </td>
  );
}

function UsageCells({ row }: { row: Usage & { disk_bytes: number | null } }) {
  return (
    <>
      <Cell label={m.monitorCpu} value={known(row.cpu_percent, formatPercent)} />
      <Cell label={m.monitorMemoryWithSwap} value={known(row.memory_bytes, formatBytes)} />
      <Cell label={m.monitorProcesses} value={known(row.tasks, formatCount)} />
      <Cell label={m.monitorDisk} value={known(row.disk_bytes, formatBytes)} />
    </>
  );
}

function PeopleCard({ people }: { people: PersonFigures[] | null }) {
  return (
    <TableCard title={m.monitorPeople} off={people === null}>
      <TableScroll label={m.tableTitle}>
        {/* on a phone a list of rows: the address, then the figures in a line */}
        <table className="stacked" {...ROLE.table}>
          <thead {...ROLE.rowgroup}>
            <tr {...ROLE.row}>
              <th {...ROLE.columnheader}>{m.monitorPerson}</th>
              <th className="number" {...ROLE.columnheader}>
                {m.usersWorkspaces}
              </th>
              <UsageHeadings />
            </tr>
          </thead>
          <tbody {...ROLE.rowgroup}>
            {(people ?? []).map((p) => (
              <tr key={p.id} {...ROLE.row}>
                <td className="strong lead" {...ROLE.cell}>
                  {p.email}
                </td>
                <Cell label={m.usersWorkspaces} value={formatCount(p.workspaces)} />
                <UsageCells row={p} />
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </TableCard>
  );
}

function ContainersCard({ containers }: { containers: ContainerFigures[] | null }) {
  return (
    <TableCard title={m.monitorContainers} off={containers === null}>
      {containers?.length === 0 ? (
        <p className="card-empty muted">{m.monitorNoContainers}</p>
      ) : (
        <TableScroll label={m.tableTitle}>
          {/* on a phone a list of rows: the workspace with its state at the right, then its
              owner and the figures in a line */}
          <table className="stacked" {...ROLE.table}>
            <thead {...ROLE.rowgroup}>
              <tr {...ROLE.row}>
                <th {...ROLE.columnheader}>{m.everyoneWorkspace}</th>
                <th {...ROLE.columnheader}>{m.versionsOwner}</th>
                <th {...ROLE.columnheader}>{m.monitorState}</th>
                <UsageHeadings />
              </tr>
            </thead>
            <tbody {...ROLE.rowgroup}>
              {(containers ?? []).map((c) => (
                <tr key={c.id} {...ROLE.row}>
                  <td className="strong lead" {...ROLE.cell}>
                    {c.name}
                  </td>
                  <td className="meta" {...ROLE.cell}>
                    {c.owner}
                  </td>
                  <td className="result" {...ROLE.cell}>
                    <WorkspaceBadge state={c.state} />
                  </td>
                  <UsageCells row={c} />
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </TableCard>
  );
}
