import { type FormEvent, Fragment, type ReactNode, useEffect, useId, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";

// The monitor's settings, as GET and PUT /api/monitor/settings have them; their defaults and
// bounds are the server's (backend/rcman/monitor.py).
type Monitoring = {
  usage_interval_seconds: number;
  disk_interval_seconds: number;
  host: boolean;
  people: boolean;
  containers: boolean;
};
type Interval = "usage_interval_seconds" | "disk_interval_seconds";
type Scope = "host" | "people" | "containers";

// Port forwarding's settings, as GET and PUT /api/forwarding/settings have them, in the units
// the labels name; their defaults and bounds are the server's
// (backend/rcman/forwarding_settings.py).
type Forwarding = { enabled: boolean; idle_minutes: number; absolute_hours: number };
type Timeout = "idle_minutes" | "absolute_hours";

const INTERVALS: [Interval, string, string][] = [
  ["usage_interval_seconds", m.monitorUsageInterval, m.monitorUsageIntervalHelp],
  ["disk_interval_seconds", m.monitorDiskInterval, m.monitorDiskIntervalHelp],
];
const SCOPES: [Scope, string][] = [
  ["host", m.monitorHost],
  ["people", m.monitorPeople],
  ["containers", m.monitorContainers],
];
const MONITORING_LABELS = Object.fromEntries([
  ...INTERVALS.map(([name, label]) => [name, label]),
  ...SCOPES,
]) as Record<string, string>;

const TIMEOUTS: [Timeout, string, string][] = [
  ["idle_minutes", m.forwardingIdle, m.forwardingIdleHelp],
  ["absolute_hours", m.forwardingAbsolute, m.forwardingAbsoluteHelp],
];
const FORWARDING_LABELS: Record<string, string> = {
  enabled: m.forwardingEnabled,
  ...Object.fromEntries(TIMEOUTS.map(([name, label]) => [name, label])),
};

// A number as it is sent: a whole number as a number, anything else as typed, for the server
// to judge.
const WHOLE = /^\s*-?\d+\s*$/;
const asSent = (text: string): number | string => (WHOLE.test(text) ? Number(text) : text);

type Status = "idle" | "busy" | "saved";

// The administrators' page of rcman's own settings, which hold for everyone: a card for each
// part, each read as the page opens and saved on its own.
export function ServiceSettings() {
  return (
    <section className="cards">
      <div className="page-head">
        <h1>{m.serviceSettingsTitle}</h1>
      </div>
      <p className="page-intro muted">{m.serviceSettingsIntro}</p>
      <MonitoringCard />
      <ForwardingCard />
    </section>
  );
}

// The settings a card shows, read once as it opens, or why they could not be.
function useRead<T>(path: string): [T | null, ApiError | null] {
  const [read, setRead] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    api.get<{ settings: T }>(path).then(
      (answer) => setRead(answer.settings),
      (e) => setError(asApiError(e)),
    );
  }, [path]);
  return [read, error];
}

// A part of the page, as a card: its title and one line on what it is for, beside its form on
// a wide screen and over it on a narrow one, as a Settings page part is (src/styles.css).
function SettingsCard({
  title,
  line,
  error,
  children,
}: {
  title: string;
  line: string;
  error: ApiError | null;
  children: ReactNode;
}) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="card setting">
      <div className="setting-intro">
        <h2 id={heading}>{title}</h2>
        <p className="muted">{line}</p>
      </div>
      <div className="setting-body">
        <ProblemView error={error} />
        {children}
      </div>
    </section>
  );
}

// A whole number typed into a text field that asks for a number pad, its unit in its label,
// with one line of help under it and the server's refusal of it between the two.
function NumberField({
  name,
  label,
  help,
  value,
  onChange,
  fields,
}: {
  name: string;
  label: string;
  help: string;
  value: string;
  onChange: (value: string) => void;
  fields: ReturnType<typeof useFieldErrors>;
}) {
  const helpId = useId();
  return (
    <div className="field">
      <label>
        {label}
        <input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          spellCheck={false}
          name={name}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          {...fields.input(name, helpId)}
        />
      </label>
      {fields.message(name)}
      <p id={helpId} className="help muted">
        {help}
      </p>
    </div>
  );
}

// A card's Save, its one primary button, named for what it saves, the words it shows among
// them, and disabled while its save is out.
function SaveRow({ name, status }: { name: string; status: Status }) {
  return (
    <div className="actions">
      <button type="submit" className="primary" disabled={status === "busy"} aria-label={name}>
        {m.save}
      </button>
    </div>
  );
}

// A card's live region, after its form, as a Settings page part's is: Saving… while its save is
// out, then that it is saved, until a field of it changes.
function SaveStatus({ status, saved }: { status: Status; saved: string }) {
  return <p role="status">{status === "busy" ? m.saving : status === "saved" ? saved : ""}</p>;
}

function MonitoringCard() {
  const [read, error] = useRead<Monitoring>("/api/monitor/settings");
  return (
    <SettingsCard title={m.monitorSettingsTitle} line={m.monitorSettingsLine} error={error}>
      {read && <MonitoringForm initial={read} />}
    </SettingsCard>
  );
}

// What the monitoring form holds: the intervals as typed.
type MonitoringTyped = Record<Interval, string> & Record<Scope, boolean>;
const monitoringTyped = (settings: Monitoring): MonitoringTyped => ({
  ...settings,
  usage_interval_seconds: String(settings.usage_interval_seconds),
  disk_interval_seconds: String(settings.disk_interval_seconds),
});

function MonitoringForm({ initial }: { initial: Monitoring }) {
  const [values, setValues] = useState(() => monitoringTyped(initial));
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const collectHelp = useId();
  const fields = useFieldErrors(error);

  const change = (next: Partial<MonitoringTyped>) => {
    setValues((current) => ({ ...current, ...next }));
    setStatus("idle");
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    setStatus("busy");
    setError(null);
    try {
      const answer = await api.put<{ settings: Monitoring }>("/api/monitor/settings", {
        usage_interval_seconds: asSent(values.usage_interval_seconds),
        disk_interval_seconds: asSent(values.disk_interval_seconds),
        host: values.host,
        people: values.people,
        containers: values.containers,
      });
      setValues(monitoringTyped(answer.settings));
      setStatus("saved");
    } catch (e) {
      setError(asApiError(e));
      setStatus("idle");
    }
  }

  return (
    <>
      <form noValidate onSubmit={save} ref={fields.form} aria-busy={status === "busy"}>
        {INTERVALS.map(([name, label, help]) => (
          <NumberField
            key={name}
            name={name}
            label={label}
            help={help}
            value={values[name]}
            onChange={(value) => change({ [name]: value })}
            fields={fields}
          />
        ))}
        <fieldset className="choices" aria-describedby={collectHelp}>
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
          <p id={collectHelp} className="help muted">
            {m.monitorCollectHelp}
          </p>
        </fieldset>
        <SaveRow name={m.monitorSave} status={status} />
      </form>
      <SaveStatus status={status} saved={m.monitorSaved} />
      <ProblemView error={error} labels={MONITORING_LABELS} inline />
    </>
  );
}

function ForwardingCard() {
  const [read, error] = useRead<Forwarding>("/api/forwarding/settings");
  return (
    <SettingsCard title={m.forwardingTitle} line={m.forwardingLine} error={error}>
      {read && <ForwardingForm initial={read} />}
    </SettingsCard>
  );
}

// What the forwarding form holds: the timeouts as typed.
type ForwardingTyped = Record<Timeout, string> & { enabled: boolean };
const forwardingTyped = (settings: Forwarding): ForwardingTyped => ({
  enabled: settings.enabled,
  idle_minutes: String(settings.idle_minutes),
  absolute_hours: String(settings.absolute_hours),
});

function ForwardingForm({ initial }: { initial: Forwarding }) {
  const [values, setValues] = useState(() => forwardingTyped(initial));
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const enabledHelp = useId();
  const fields = useFieldErrors(error);

  const change = (next: Partial<ForwardingTyped>) => {
    setValues((current) => ({ ...current, ...next }));
    setStatus("idle");
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    setStatus("busy");
    setError(null);
    try {
      const answer = await api.put<{ settings: Forwarding }>("/api/forwarding/settings", {
        enabled: values.enabled,
        idle_minutes: asSent(values.idle_minutes),
        absolute_hours: asSent(values.absolute_hours),
      });
      setValues(forwardingTyped(answer.settings));
      setStatus("saved");
    } catch (e) {
      setError(asApiError(e));
      setStatus("idle");
    }
  }

  return (
    <>
      <form noValidate onSubmit={save} ref={fields.form} aria-busy={status === "busy"}>
        <div className="field">
          <label>
            <input
              type="checkbox"
              name="enabled"
              checked={values.enabled}
              onChange={(e) => change({ enabled: e.target.checked })}
              {...fields.input("enabled", enabledHelp)}
            />
            {m.forwardingEnabled}
          </label>
          {fields.message("enabled")}
          <p id={enabledHelp} className="help muted">
            {m.forwardingEnabledHelp}
          </p>
        </div>
        {TIMEOUTS.map(([name, label, help]) => (
          <NumberField
            key={name}
            name={name}
            label={label}
            help={help}
            value={values[name]}
            onChange={(value) => change({ [name]: value })}
            fields={fields}
          />
        ))}
        <SaveRow name={m.forwardingSave} status={status} />
      </form>
      <SaveStatus status={status} saved={m.forwardingSaved} />
      <ProblemView error={error} labels={FORWARDING_LABELS} inline />
    </>
  );
}
