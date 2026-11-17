import { type FormEvent, Fragment, type ReactNode, useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";

import { type ApiError, api } from "./api";
import { MessageBar } from "./MessageBar";
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
  storage: boolean;
};
type Interval = "usage_interval_seconds" | "disk_interval_seconds";
type Scope = "host" | "people" | "containers" | "storage";

// Port forwarding's settings, as GET and PUT /api/forwarding/settings have them, in the units
// the labels name; their defaults and bounds are the server's
// (backend/rcman/forwarding_settings.py).
type Forwarding = { enabled: boolean; idle_minutes: number; absolute_hours: number };
type Timeout = "idle_minutes" | "absolute_hours";

// The settings of a workspace's files, as GET and PUT /api/files/settings have them, the limit
// in megabytes of a million bytes, 0 for none; its default and bounds are the server's
// (backend/rcman/files_settings.py).
type FilesSettings = { folder_download_limit_mb: number };

// Microsoft sign-in's settings, as GET /api/microsoft/settings has them: never the client
// secret, only whether one is stored, and whether rcman has the key it needs to keep one
// (backend/rcman/microsoft_settings.py, backend/rcman/routers/microsoft_settings.py).
type Microsoft = {
  enabled: boolean;
  client_id: string;
  tenant_id: string;
  secret_set: boolean;
  redirect_uri: string;
  available: boolean;
};
// Where the guide says how to set up the app registration.
const MICROSOFT_GUIDE = "/help/service-settings#microsoft-sign-in";

const INTERVALS: [Interval, string, string][] = [
  ["usage_interval_seconds", m.monitorUsageInterval, m.monitorUsageIntervalHelp],
  ["disk_interval_seconds", m.monitorDiskInterval, m.monitorDiskIntervalHelp],
];
const SCOPES: [Scope, string][] = [
  ["host", m.monitorHost],
  ["people", m.monitorPeople],
  ["containers", m.monitorContainers],
  ["storage", m.monitorStorage],
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
const FILES_LABELS: Record<string, string> = { folder_download_limit_mb: m.filesFolderLimit };
const MICROSOFT_LABELS: Record<string, string> = {
  client_id: m.microsoftClientId,
  tenant_id: m.microsoftTenantId,
  client_secret: m.microsoftSecret,
  enabled: m.microsoftEnabled,
};

// A number as it is sent: a whole number as a number, anything else as typed, for the server
// to judge.
const WHOLE = /^\s*-?\d+\s*$/;
const asSent = (text: string): number | string => (WHOLE.test(text) ? Number(text) : text);

// A card's state, which its status line says: a save out, a save done, or, on the Microsoft
// sign-in card, the redirect URI copied.
type Status = "idle" | "busy" | "saved" | "copied";

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
      <FilesCard />
      <MicrosoftCard />
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
// out, then that it is saved, or what else the card did (``copied``), until a field of it
// changes.
function SaveStatus({
  status,
  saved,
  copied = "",
}: {
  status: Status;
  saved: string;
  copied?: string;
}) {
  const said = { idle: "", busy: m.saving, saved, copied };
  return <p role="status">{said[status]}</p>;
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
        storage: values.storage,
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

function FilesCard() {
  const [read, error] = useRead<FilesSettings>("/api/files/settings");
  return (
    <SettingsCard title={m.filesTitle} line={m.filesLine} error={error}>
      {read && <FilesForm initial={read} />}
    </SettingsCard>
  );
}

function FilesForm({ initial }: { initial: FilesSettings }) {
  const [limit, setLimit] = useState(() => String(initial.folder_download_limit_mb));
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const fields = useFieldErrors(error);

  async function save(event: FormEvent) {
    event.preventDefault();
    setStatus("busy");
    setError(null);
    try {
      const answer = await api.put<{ settings: FilesSettings }>("/api/files/settings", {
        folder_download_limit_mb: asSent(limit),
      });
      setLimit(String(answer.settings.folder_download_limit_mb));
      setStatus("saved");
    } catch (e) {
      setError(asApiError(e));
      setStatus("idle");
    }
  }

  return (
    <>
      <form noValidate onSubmit={save} ref={fields.form} aria-busy={status === "busy"}>
        <NumberField
          name="folder_download_limit_mb"
          label={m.filesFolderLimit}
          help={m.filesFolderLimitHelp}
          value={limit}
          onChange={(value) => {
            setLimit(value);
            setStatus("idle");
          }}
          fields={fields}
        />
        <SaveRow name={m.filesSave} status={status} />
      </form>
      <SaveStatus status={status} saved={m.filesSaved} />
      <ProblemView error={error} labels={FILES_LABELS} inline />
    </>
  );
}

function MicrosoftCard() {
  const [read, error] = useRead<Microsoft>("/api/microsoft/settings");
  return (
    <SettingsCard title={m.microsoftTitle} line={m.microsoftLine} error={error}>
      {read &&
        (read.available ? (
          <MicrosoftForm initial={read} />
        ) : (
          // Without rcman's secret key nothing can be set up here: no form is offered.
          <MessageBar kind="warning">
            <p>{m.microsoftUnavailable}</p>
          </MessageBar>
        ))}
    </SettingsCard>
  );
}

// An address with a place to wrap after each slash that ends a part of it, so a narrow card
// breaks it between its parts rather than inside one.
const breakable = (address: string): ReactNode[] => {
  let start = 0;
  return address.split(/(?<=\/)(?!\/)/).map((part) => {
    const at = start;
    start += part.length;
    return (
      <Fragment key={at}>
        {at > 0 && <wbr />}
        {part}
      </Fragment>
    );
  });
};

// What the Microsoft sign-in form holds: the IDs as typed, a new client secret, empty to keep
// the one stored, and the switch.
type MicrosoftTyped = { enabled: boolean; client_id: string; tenant_id: string; secret: string };
const microsoftTyped = (settings: Microsoft): MicrosoftTyped => ({
  enabled: settings.enabled,
  client_id: settings.client_id,
  tenant_id: settings.tenant_id,
  secret: "",
});

// The settings in the order of setting up the app registration in Microsoft Entra: the
// redirect URI to give it, first, then its IDs and secret, and last the switch, which the
// server refuses until they are there.
function MicrosoftForm({ initial }: { initial: Microsoft }) {
  const [shown, setShown] = useState(initial);
  const [values, setValues] = useState(() => microsoftTyped(initial));
  const [status, setStatus] = useState<Status>("idle");
  const [copyFailed, setCopyFailed] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const ids = { client: useId(), tenant: useId(), claim: useId(), secret: useId() };
  const secretState = useId();
  const enabledHelp = useId();
  const redirectHelp = useId();
  const fields = useFieldErrors(error);

  const change = (next: Partial<MicrosoftTyped>) => {
    setValues((current) => ({ ...current, ...next }));
    setStatus("idle");
    setCopyFailed(false);
  };

  async function copy() {
    setCopyFailed(false);
    try {
      await navigator.clipboard.writeText(shown.redirect_uri);
      if (status !== "busy") setStatus("copied");
    } catch {
      setCopyFailed(true);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setStatus("busy");
    setCopyFailed(false);
    setError(null);
    try {
      const answer = await api.put<{ settings: Microsoft }>("/api/microsoft/settings", {
        enabled: values.enabled,
        client_id: values.client_id,
        tenant_id: values.tenant_id,
        // Sent only to replace the one stored.
        ...(values.secret === "" ? {} : { client_secret: values.secret }),
      });
      setShown(answer.settings);
      setValues(microsoftTyped(answer.settings));
      setStatus("saved");
    } catch (e) {
      setError(asApiError(e));
      setStatus("idle");
    }
  }

  const idField = (
    name: "client_id" | "tenant_id",
    label: string,
    help: ReactNode,
    helpId: string,
  ) => (
    <div className="field">
      <label>
        {label}
        <input
          type="text"
          name={name}
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          translate="no"
          value={values[name]}
          onChange={(e) => change({ [name]: e.target.value })}
          {...fields.input(name, helpId)}
        />
      </label>
      {fields.message(name)}
      {help}
    </div>
  );

  return (
    <>
      <dl className="facts redirect-uri">
        <dt>{m.microsoftRedirect}</dt>
        <dd className="redirect-uri-row">
          <code className="redirect-uri-address" translate="no">
            {breakable(shown.redirect_uri)}
          </code>
          <button
            type="button"
            className="secondary redirect-uri-copy"
            aria-label={m.microsoftCopyName}
            aria-describedby={redirectHelp}
            onClick={copy}
          >
            {m.microsoftCopy}
          </button>
        </dd>
      </dl>
      {copyFailed && (
        <MessageBar kind="problem">
          <p>{m.microsoftCopyFailed}</p>
        </MessageBar>
      )}
      <p id={redirectHelp} className="help muted">
        {m.microsoftRedirectHelp}
      </p>
      <form noValidate onSubmit={save} ref={fields.form} aria-busy={status === "busy"}>
        {idField(
          "client_id",
          m.microsoftClientId,
          <p id={ids.client} className="help muted">
            {m.microsoftClientIdHelp}
          </p>,
          ids.client,
        )}
        {idField(
          "tenant_id",
          m.microsoftTenantId,
          <>
            <p id={ids.tenant} className="help muted">
              {m.microsoftTenantIdHelp}
            </p>
            <p id={ids.claim} className="help muted">
              {m.microsoftClaimHelp} <Link to={MICROSOFT_GUIDE}>{m.microsoftClaimGuide}</Link>.
            </p>
          </>,
          `${ids.tenant} ${ids.claim}`,
        )}
        <div className="field">
          <label>
            {m.microsoftSecret}
            <input
              type="password"
              name="client_secret"
              autoComplete="new-password"
              spellCheck={false}
              autoCapitalize="off"
              value={values.secret}
              onChange={(e) => change({ secret: e.target.value })}
              {...fields.input("client_secret", `${secretState} ${ids.secret}`)}
            />
          </label>
          {fields.message("client_secret")}
          <p id={secretState} className="help">
            {shown.secret_set ? m.microsoftSecretStored : m.microsoftSecretNotStored}
          </p>
          <p id={ids.secret} className="help muted">
            {m.microsoftSecretHelp}
          </p>
        </div>
        <div className="field">
          <label>
            <input
              type="checkbox"
              name="enabled"
              checked={values.enabled}
              onChange={(e) => change({ enabled: e.target.checked })}
              {...fields.input("enabled", enabledHelp)}
            />
            {m.microsoftEnabled}
          </label>
          {fields.message("enabled")}
          <p id={enabledHelp} className="help muted">
            {m.microsoftEnabledHelp}
          </p>
        </div>
        <SaveRow name={m.microsoftSave} status={status} />
      </form>
      <SaveStatus status={status} saved={m.microsoftSaved} copied={m.microsoftCopied} />
      <ProblemView error={error} labels={MICROSOFT_LABELS} inline />
    </>
  );
}
