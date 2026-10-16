import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import { TableScroll } from "./TableScroll";

// The three limits as the server takes them back: text, null for no limit.
type Held = { memory: string | null; cpu: string | null; pids: string | null };
// Either figure is null where the server cannot tell.
type Used = { memory: string | null; memory_bytes: number | null; tasks: number | null };
type ScopeReport = { limits: Held; usage: Used | null };
type WorkspaceReport = ScopeReport & { id: number; name: string; state: string };
type Report = { person: ScopeReport; workspaces: WorkspaceReport[] };

type Person = {
  id: number;
  email: string;
  admin: boolean;
  state: string;
  workspaces: number;
  limits: Held;
};

// The fields of the limits, in order, by the server's name for each.
const LIMIT_FIELDS = [
  ["memory", m.usersMemory],
  ["cpu", m.usersCpu],
  ["pids", m.usersProcesses],
] as const;
const LIMIT_LABELS = Object.fromEntries(LIMIT_FIELDS);

// A refusal for being below what is in use names the field it is about; shown under that
// field, its title, which says what is in use, is the field's message.
function underItsField(failure: ApiError | null): ApiError | null {
  const field = failure?.extra.limit;
  if (failure?.code !== "limit_below_use" || typeof field !== "string") return failure;
  if (!LIMIT_FIELDS.some(([name]) => name === field)) return failure;
  return new ApiError(
    failure.status,
    failure.code,
    failure.title,
    [{ field, message: failure.title }],
    failure.extra,
  );
}

type Typed = Record<keyof Held, string>;
const typed = (held: Held): Typed => ({
  memory: held.memory ?? "",
  cpu: held.cpu ?? "",
  pids: held.pids ?? "",
});
// What is sent: all three, exactly as typed, null for a field left empty.
const sent = (values: Typed): Held => ({
  memory: values.memory === "" ? null : values.memory,
  cpu: values.cpu === "" ? null : values.cpu,
  pids: values.pids === "" ? null : values.pids,
});

// Asks before removing someone, whose workspaces are deleted with them.
function RemoveDialog({
  person,
  onDone,
  onCancel,
}: {
  person: Person;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function remove(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.delete(`/api/people/${person.id}`);
      onDone();
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
    }
  }

  return (
    <div role="dialog" aria-label={m.usersRemoveTitle(person.email)} className="dialog">
      <h2>{m.usersRemoveTitle(person.email)}</h2>
      <form noValidate onSubmit={remove}>
        <p>{m.usersRemoveBody}</p>
        <div className="actions">
          <button type="submit" className="danger" disabled={busy}>
            {m.usersRemoveConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
      <ProblemView error={error} />
    </div>
  );
}

// One scope's limits (a person's, or one of their workspaces') in a form of its own, beside
// what the scope uses now. A refusal is the dialog's to show, one at a time.
function ScopeForm({
  title,
  path,
  report,
  failure,
  onFailed,
  onSaving,
  onSaved,
  onSettled,
}: {
  title: string;
  path: string;
  report: ScopeReport;
  failure: ApiError | null;
  onFailed: (error: ApiError | null) => void;
  onSaving: () => void;
  onSaved: () => void;
  onSettled: () => void;
}) {
  const heading = useId();
  const hints = useId();
  const refused = useMemo(() => underItsField(failure), [failure]);
  const fields = useFieldErrors(refused);
  const [values, setValues] = useState(typed(report.limits));
  const [status, setStatus] = useState<"idle" | "busy" | "saved">("idle");

  async function save(event: FormEvent) {
    event.preventDefault();
    setStatus("busy");
    onFailed(null);
    onSaving();
    try {
      const stored = await api.put<{ limits: Held }>(path, sent(values));
      setValues(typed(stored.limits));
      setStatus("saved");
      onSaved();
    } catch (e) {
      onFailed(asApiError(e));
      setStatus("idle");
    }
    onSettled();
  }

  return (
    <form
      noValidate
      onSubmit={save}
      ref={fields.form}
      aria-labelledby={heading}
      aria-busy={status === "busy"}
      className="limits"
    >
      <h3 id={heading}>{title}</h3>
      <p>
        {report.usage && report.usage.memory !== null && report.usage.tasks !== null
          ? m.limitsUsage(report.usage.memory, report.usage.tasks)
          : m.limitsUsageUnknown}
      </p>
      <div className="limits-fields">
        {LIMIT_FIELDS.map(([name, label]) => (
          <div key={name} className="limits-field">
            <label>
              {label}
              <input
                type="text"
                name={name}
                value={values[name]}
                onChange={(e) => {
                  setValues({ ...values, [name]: e.target.value });
                  setStatus("idle");
                }}
                {...fields.input(name, `${hints}-${name}`)}
              />
            </label>
            {fields.message(name)}
            <p id={`${hints}-${name}`} className="muted">
              {m.limitsHints[name]}
            </p>
          </div>
        ))}
      </div>
      <div className="actions">
        <button
          type="submit"
          className="primary"
          disabled={status === "busy"}
          aria-label={m.limitsSaveFor(title)}
        >
          {m.save}
        </button>
      </div>
      <p role="status">
        {status === "busy" ? m.limitsSaving : status === "saved" ? m.limitsSaved(title) : ""}
      </p>
      <ProblemView error={refused} labels={LIMIT_LABELS} inline />
    </form>
  );
}

// The limits on a person's workspaces together and on each workspace, with what each uses
// now, read when the dialog opens and again after every save, whatever came of it. Each scope
// is saved on its own, and the dialog cannot be left while one is.
function LimitsDialog({
  person,
  onSaved,
  onClose,
}: {
  person: Person;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [failure, setFailure] = useState<{ path: string; error: ApiError } | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const base = `/api/people/${person.id}`;

  const [saving, setSaving] = useState(0); // saves in flight
  const waits = useId();
  const asked = useRef(0);

  // Reads the limits and use again; an answer older than one asked for since is dropped.
  const load = useCallback(() => {
    const mine = ++asked.current;
    api.get<Report>(`${base}/limits`).then(
      (r) => {
        if (mine !== asked.current) return;
        setReport(r);
        setLoadError(null);
      },
      (e) => {
        if (mine === asked.current) setLoadError(asApiError(e));
      },
    );
  }, [base]);

  useEffect(() => dialog.current?.focus(), []);
  useEffect(load, [load]);

  // A save is not left running unseen: the dialog stays until it is answered.
  function leave(event: KeyboardEvent) {
    if (event.key === "Escape" && saving === 0) onClose();
  }

  function scope(title: string, path: string, held: ScopeReport) {
    return (
      <ScopeForm
        key={path}
        title={title}
        path={path}
        report={held}
        failure={failure?.path === path ? failure.error : null}
        onFailed={(error) => setFailure(error && { path, error })}
        onSaving={() => setSaving((n) => n + 1)}
        onSaved={onSaved}
        onSettled={() => {
          setSaving((n) => n - 1);
          load();
        }}
      />
    );
  }

  return (
    <div
      role="dialog"
      aria-label={m.limitsTitle(person.email)}
      className="dialog"
      tabIndex={-1}
      ref={dialog}
      onKeyDown={leave}
    >
      <h2>{m.limitsTitle(person.email)}</h2>
      {report === null && loadError === null && <p>{m.loading}</p>}
      <ProblemView error={loadError} />
      {report && (
        <>
          {scope(m.limitsPersonScope(person.email), `${base}/limits`, report.person)}
          {report.workspaces.map((w) =>
            scope(
              m.limitsWorkspaceScope(
                w.name,
                w.state === "ready" ? null : (m.workspaceStates[w.state] ?? w.state),
              ),
              `${base}/workspaces/${w.id}/limits`,
              w,
            ),
          )}
        </>
      )}
      <details className="how">
        <summary>{m.limitsHowTitle}</summary>
        <p>{m.limitsHeldToPerson}</p>
        <p>{m.limitsMemoryHard}</p>
        <p>{m.limitsBelowUse}</p>
        <p>{m.limitsNotLimited}</p>
      </details>
      <div className="actions">
        <button
          type="button"
          disabled={saving > 0}
          aria-describedby={saving > 0 ? waits : undefined}
          onClick={onClose}
        >
          {m.close}
        </button>
      </div>
      {saving > 0 && <p id={waits}>{m.limitsCloseWaits}</p>}
    </div>
  );
}

export function People() {
  const [people, setPeople] = useState<Person[]>([]);
  const [email, setEmail] = useState("");
  const [admin, setAdmin] = useState(false);
  const [removing, setRemoving] = useState<Person | null>(null);
  const [limiting, setLimiting] = useState<Person | null>(null);
  const openers = useRef(new Map<number, HTMLButtonElement>());
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const fields = useFieldErrors(error);

  const load = useCallback(() => {
    api.get<{ people: Person[] }>("/api/people").then(
      (r) => setPeople(r.people),
      (e) => setError(asApiError(e)),
    );
  }, []);
  useEffect(load, [load]);

  async function add(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    try {
      await api.post("/api/people", { email, admin });
      setNotice(m.usersAdded(email));
      setEmail("");
      setAdmin(false);
      load();
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <section>
      <h1>{m.usersTitle}</h1>
      <TableScroll label={m.tableTitle}>
        <table>
          <thead>
            <tr>
              <th>{m.usersEmail}</th>
              <th>{m.usersRole}</th>
              <th>{m.usersWorkspaces}</th>
              <th>{m.usersMemory}</th>
              <th>{m.usersCpu}</th>
              <th>{m.usersProcesses}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {people.map((p) => (
              <tr key={p.id}>
                <td>{p.email}</td>
                <td>{p.admin ? m.usersAdmin : m.usersUser}</td>
                <td>{p.workspaces}</td>
                <td>{p.limits.memory ?? m.usersNoLimit}</td>
                <td>{p.limits.cpu ?? m.usersNoLimit}</td>
                <td>{p.limits.pids ?? m.usersNoLimit}</td>
                <td>
                  <div className="actions">
                    <button
                      type="button"
                      ref={(button) => {
                        if (button) openers.current.set(p.id, button);
                        else openers.current.delete(p.id);
                      }}
                      onClick={() => {
                        setNotice(null);
                        setRemoving(null);
                        setLimiting(p);
                      }}
                    >
                      {m.usersLimits}
                    </button>
                    <button
                      type="button"
                      className="secondary-danger"
                      onClick={() => {
                        setNotice(null);
                        setLimiting(null);
                        setRemoving(p);
                      }}
                    >
                      {m.usersRemove}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      {limiting && (
        <LimitsDialog
          key={limiting.id}
          person={limiting}
          onSaved={load}
          onClose={() => {
            openers.current.get(limiting.id)?.focus();
            setLimiting(null);
          }}
        />
      )}
      {removing && (
        <RemoveDialog
          key={removing.id}
          person={removing}
          onCancel={() => setRemoving(null)}
          onDone={() => {
            setNotice(m.usersRemoved(removing.email));
            setRemoving(null);
            load();
          }}
        />
      )}
      <h2>{m.usersAdd}</h2>
      <form noValidate onSubmit={add} ref={fields.form}>
        <label>
          {m.usersEmail}
          <input
            type="text"
            name="email"
            value={email}
            placeholder={m.examples.email}
            onChange={(e) => setEmail(e.target.value)}
            {...fields.input("email")}
          />
        </label>
        {fields.message("email")}
        <label>
          <input
            type="checkbox"
            name="admin"
            checked={admin}
            onChange={(e) => setAdmin(e.target.checked)}
            {...fields.input("admin")}
          />
          {m.usersAddAdmin}
        </label>
        {fields.message("admin")}
        <button type="submit" className="primary">
          {m.usersAdd}
        </button>
      </form>
      {notice && <p role="status">{notice}</p>}
      <ProblemView error={error} labels={{ email: m.usersEmail, admin: m.usersAddAdmin }} inline />
    </section>
  );
}
