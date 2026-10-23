import {
  type FormEvent,
  Fragment,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { type ApiError, api } from "./api";
import { formatBytes, formatCount, formatPercent, formatSeconds, utcMoment } from "./format";
import { MonitorIcon } from "./Icons";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
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

// The monitor's settings, as GET and PUT /api/monitor/settings have them; their defaults and
// bounds are the server's (backend/rcman/monitor.py).
type Settings = {
  usage_interval_seconds: number;
  disk_interval_seconds: number;
  host: boolean;
  people: boolean;
  containers: boolean;
};
type Interval = "usage_interval_seconds" | "disk_interval_seconds";
type Scope = "host" | "people" | "containers";

// How often the page reads what is known again: it reads what rcman collected, and collects
// nothing itself, so a read costs rcman little.
export const MONITOR_POLL_MS = 5000;
// The settings card's id, which a part switched off links to: the guide's section of the same
// name (src/monitor-guide.test.ts).
export const SETTINGS_ANCHOR = "monitoring-settings";

const INTERVALS: [Interval, string, string][] = [
  ["usage_interval_seconds", m.monitorUsageInterval, m.monitorUsageIntervalHelp],
  ["disk_interval_seconds", m.monitorDiskInterval, m.monitorDiskIntervalHelp],
];
const SCOPES: [Scope, string][] = [
  ["host", m.monitorHost],
  ["people", m.monitorPeople],
  ["containers", m.monitorContainers],
];
const LABELS = Object.fromEntries([
  ...INTERVALS.map(([name, label]) => [name, label]),
  ...SCOPES,
]) as Record<string, string>;

// The Monitor page, for administrators: what the host, each person and each workspace container
// use now, as rcman last collected it, read again every MONITOR_POLL_MS; and the monitor's
// settings, read once, which a read of the figures never overwrites.
export function Monitor() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  // Why the last read of the figures failed; the figures read before stay shown.
  const [readError, setReadError] = useState<ApiError | null>(null);
  // The settings as saved, for the live region to say how often each round runs.
  const [saved, setSaved] = useState<Settings | null>(null);
  const [settingsError, setSettingsError] = useState<ApiError | null>(null);
  // Bumped after a save, so that the figures are read again at once.
  const [reading, setReading] = useState(0);
  // The read that is out, settled either way, which a read waits for: reads never overlap, a
  // save's among them.
  const inflight = useRef<Promise<unknown> | null>(null);

  useEffect(() => {
    api.get<{ settings: Settings }>("/api/monitor/settings").then(
      (answer) => setSaved(answer.settings),
      (e) => setSettingsError(asApiError(e)),
    );
  }, []);

  // The figures, read now and then again a while after each answer, a read out already, as when
  // a save asks for one, waited for first, so that reads never overlap. The read as the page
  // opens notes its failure for rcman; every later one is quiet: its failure is shown here.
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
    read(reading === 0);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [reading]);

  const onSaved = useCallback((settings: Settings) => {
    setSaved(settings);
    setReading((n) => n + 1);
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
      <SettingsCard saved={saved} error={settingsError} onSaved={onSaved} />
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

// A part whose scope is switched off: what is not there, and the way to the settings.
function Off({ scope }: { scope: string }) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <MonitorIcon />
      </span>
      <strong>{m.monitorOff}</strong>
      <p>{m.monitorOffBody(scope)}</p>
      {/* moves focus to the settings card without putting a fragment in the address, as the skip link does */}
      <a
        className="button"
        href={`#${SETTINGS_ANCHOR}`}
        onClick={(event) => {
          event.preventDefault();
          document.getElementById(SETTINGS_ANCHOR)?.focus();
        }}
      >
        {m.monitorOffLink}
      </a>
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

// The settings card: shown once the settings are read, and its form filled from them once.
function SettingsCard({
  saved,
  error,
  onSaved,
}: {
  saved: Settings | null;
  error: ApiError | null;
  onSaved: (settings: Settings) => void;
}) {
  const heading = useId();
  return (
    <section id={SETTINGS_ANCHOR} tabIndex={-1} aria-labelledby={heading} className="card setting">
      <div className="setting-intro">
        <h2 id={heading}>{m.monitorSettingsTitle}</h2>
        <p className="muted">{m.monitorSettingsLine}</p>
      </div>
      <div className="setting-body">
        <ProblemView error={error} />
        {saved && <SettingsForm initial={saved} onSaved={onSaved} />}
      </div>
    </section>
  );
}

// What the form holds: the intervals as typed.
type Typed = Record<Interval, string> & Record<Scope, boolean>;
const typed = (settings: Settings): Typed => ({
  ...settings,
  usage_interval_seconds: String(settings.usage_interval_seconds),
  disk_interval_seconds: String(settings.disk_interval_seconds),
});
// An interval as it is sent: a whole number as a number, anything else as typed, for the server
// to judge.
const WHOLE = /^\s*-?\d+\s*$/;
const asSent = (text: string): number | string => (WHOLE.test(text) ? Number(text) : text);

function SettingsForm({
  initial,
  onSaved,
}: {
  initial: Settings;
  onSaved: (settings: Settings) => void;
}) {
  const [values, setValues] = useState(() => typed(initial));
  const [status, setStatus] = useState<"idle" | "busy" | "saved">("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const help = useId();
  const fields = useFieldErrors(error);

  const change = (next: Partial<Typed>) => {
    setValues((current) => ({ ...current, ...next }));
    setStatus("idle");
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    setStatus("busy");
    setError(null);
    try {
      const answer = await api.put<{ settings: Settings }>("/api/monitor/settings", {
        usage_interval_seconds: asSent(values.usage_interval_seconds),
        disk_interval_seconds: asSent(values.disk_interval_seconds),
        host: values.host,
        people: values.people,
        containers: values.containers,
      });
      setValues(typed(answer.settings));
      setStatus("saved");
      onSaved(answer.settings);
    } catch (e) {
      setError(asApiError(e));
      setStatus("idle");
    }
  }

  return (
    <>
      <form noValidate onSubmit={save} ref={fields.form} aria-busy={status === "busy"}>
        {INTERVALS.map(([name, label, line]) => (
          <div key={name} className="field">
            <label>
              {label}
              <input
                type="text"
                inputMode="numeric"
                autoComplete="off"
                spellCheck={false}
                name={name}
                value={values[name]}
                onChange={(e) => change({ [name]: e.target.value })}
                {...fields.input(name, `${help}-${name}`)}
              />
            </label>
            {fields.message(name)}
            <p id={`${help}-${name}`} className="help muted">
              {line}
            </p>
          </div>
        ))}
        <fieldset className="choices">
          <legend>{m.monitorCollect}</legend>
          {SCOPES.map(([name, label]) => (
            <Fragment key={name}>
              <label>
                <input
                  type="checkbox"
                  name={name}
                  checked={values[name]}
                  onChange={(e) => change({ [name]: e.target.checked })}
                  {...fields.input(name)}
                />
                {label}
              </label>
              {fields.message(name)}
            </Fragment>
          ))}
        </fieldset>
        <div className="actions">
          <button
            type="submit"
            className="primary"
            disabled={status === "busy"}
            aria-label={m.monitorSave}
          >
            {m.save}
          </button>
        </div>
      </form>
      <p role="status">
        {status === "busy" ? m.monitorSaving : status === "saved" ? m.monitorSaved : ""}
      </p>
      <ProblemView error={error} labels={LABELS} inline />
    </>
  );
}
