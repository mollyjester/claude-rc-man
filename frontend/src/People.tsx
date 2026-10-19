import { type FormEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { Dialog } from "./Dialog";
import { MoreIcon, PlusIcon } from "./Icons";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import { ROLE, TableScroll } from "./TableScroll";
import { usePopup } from "./usePopup";

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
    <Dialog
      owner="RemoveDialog"
      title={m.usersRemoveTitle(person.email)}
      onClose={onCancel}
      closable={!busy}
    >
      <form noValidate onSubmit={remove}>
        <p>{m.usersRemoveBody}</p>
        <div className="actions footer">
          <button type="submit" className="danger" disabled={busy}>
            {m.usersRemoveConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
      <ProblemView error={error} />
    </Dialog>
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
      <div className="limits-head">
        <h3 id={heading}>{title}</h3>
        <p>
          {report.usage && report.usage.memory !== null && report.usage.tasks !== null
            ? m.limitsUsage(report.usage.memory, report.usage.tasks)
            : m.limitsUsageUnknown}
        </p>
      </div>
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
            <p id={`${hints}-${name}`} className="help muted">
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
        <p role="status" className="muted">
          {status === "busy" ? m.limitsSaving : status === "saved" ? m.limitsSaved(title) : ""}
        </p>
      </div>
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

  useEffect(load, [load]);

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

  // Focus starts on the dialog, above the forms, and a save is not left running unseen: the
  // dialog stays until it is answered.
  return (
    <Dialog
      owner="LimitsDialog"
      title={m.limitsTitle(person.email)}
      onClose={onClose}
      closable={saving === 0}
      focusDialog
      large
    >
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
      <div className="actions footer">
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
    </Dialog>
  );
}

// Adds a person by address, as a user or an administrator; the server judges what was typed.
function AddPersonDialog({
  onAdded,
  onCancel,
}: {
  onAdded: (email: string) => void;
  onCancel: () => void;
}) {
  const [email, setEmail] = useState("");
  const [admin, setAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const fields = useFieldErrors(error);

  async function add(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/people", { email, admin });
      onAdded(email);
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
    }
  }

  return (
    <Dialog owner="AddPersonDialog" title={m.usersAdd} onClose={onCancel} closable={!busy}>
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
        <div className="actions footer">
          <button type="submit" className="primary" disabled={busy}>
            {m.usersAddGo}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
      <ProblemView error={error} labels={{ email: m.usersEmail, admin: m.usersAddAdmin }} inline />
    </Dialog>
  );
}

// A row's actions on a phone, where the rows are a list: one subtle button, named for its
// person, whose menu holds Limits and Remove, as the row's own buttons do on a wider screen
// (src/styles.css shows one or the other). Choosing one closes the menu, focus back on its
// button, which the dialog then opened gives it back to as it goes.
function PersonMenu({
  person,
  onLimits,
  onRemove,
}: {
  person: Person;
  onLimits: (from: HTMLElement | null) => void;
  onRemove: () => void;
}) {
  const menu = usePopup("PersonMenu");
  const button = menu.buttonProps.ref;
  return (
    <div className="popup-anchor person-menu" ref={menu.rootRef}>
      <button
        type="button"
        className="icon-button subtle"
        aria-label={m.usersMoreActionsFor(person.email)}
        title={m.usersMoreActions}
        {...menu.buttonProps}
      >
        <MoreIcon />
      </button>
      {menu.open && (
        <div className="popover menu-popover row-menu" {...menu.panelProps}>
          <button
            type="button"
            onClick={() => {
              menu.close(true);
              onLimits(button.current);
            }}
          >
            {m.usersLimits}
          </button>
          <button
            type="button"
            className="danger-item"
            onClick={() => {
              menu.close(true);
              onRemove();
            }}
          >
            {m.usersRemove}
          </button>
        </div>
      )}
    </div>
  );
}

export function People() {
  const [people, setPeople] = useState<Person[]>([]);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Person | null>(null);
  const [limiting, setLimiting] = useState<Person | null>(null);
  // What opened the Limits dialog, for focus to go back to as it closes: the row's Limits, or
  // on a phone its More actions.
  const opener = useRef<HTMLElement | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(() => {
    api.get<{ people: Person[] }>("/api/people").then(
      (r) => setPeople(r.people),
      (e) => setError(asApiError(e)),
    );
  }, []);
  useEffect(load, [load]);

  function limit(p: Person, from: HTMLElement | null) {
    opener.current = from;
    setNotice(null);
    setRemoving(null);
    setLimiting(p);
  }

  function remove(p: Person) {
    setNotice(null);
    setLimiting(null);
    setRemoving(p);
  }

  return (
    <section>
      <div className="page-head">
        <h1>{m.usersTitle}</h1>
        <button
          type="button"
          className="primary"
          onClick={() => {
            setNotice(null);
            setAdding(true);
          }}
        >
          <PlusIcon />
          {m.usersAdd}
        </button>
      </div>
      {/* there before it says anything, so that what it says is announced */}
      <div role="status">
        {notice && (
          <MessageBar kind="success">
            <p>{notice}</p>
          </MessageBar>
        )}
      </div>
      <ProblemView error={error} />
      <div className="card flush">
        <TableScroll label={m.tableTitle}>
          {/* on a phone a list of rows: the address and the actions, then the rest in a line */}
          <table className="stacked people" {...ROLE.table}>
            <thead {...ROLE.rowgroup}>
              <tr {...ROLE.row}>
                <th {...ROLE.columnheader}>{m.usersEmail}</th>
                <th {...ROLE.columnheader}>{m.usersRole}</th>
                <th className="number" {...ROLE.columnheader}>
                  {m.usersWorkspaces}
                </th>
                <th {...ROLE.columnheader}>{m.usersMemory}</th>
                <th {...ROLE.columnheader}>{m.usersCpu}</th>
                <th {...ROLE.columnheader}>{m.usersProcesses}</th>
                <th {...ROLE.columnheader}>
                  <span className="visually-hidden">{m.usersActions}</span>
                </th>
              </tr>
            </thead>
            <tbody {...ROLE.rowgroup}>
              {people.map((p) => (
                <tr key={p.id} {...ROLE.row}>
                  <td className="strong lead" {...ROLE.cell}>
                    {p.email}
                  </td>
                  <td className="meta" {...ROLE.cell}>
                    {p.admin ? m.usersAdmin : m.usersUser}
                  </td>
                  <td className="number meta named" data-label={m.usersWorkspaces} {...ROLE.cell}>
                    {p.workspaces}
                  </td>
                  <Limit label={m.usersMemory} value={p.limits.memory} />
                  <Limit label={m.usersCpu} value={p.limits.cpu} />
                  <Limit label={m.usersProcesses} value={p.limits.pids} />
                  <td className="row-actions" {...ROLE.cell}>
                    <div className="actions">
                      <button
                        type="button"
                        className="subtle"
                        onClick={(event) => limit(p, event.currentTarget)}
                      >
                        {m.usersLimits}
                      </button>
                      <button type="button" className="secondary-danger" onClick={() => remove(p)}>
                        {m.usersRemove}
                      </button>
                    </div>
                    <PersonMenu
                      person={p}
                      onLimits={(from) => limit(p, from)}
                      onRemove={() => remove(p)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </div>
      {adding && (
        <AddPersonDialog
          onCancel={() => setAdding(false)}
          onAdded={(email) => {
            setAdding(false);
            setNotice(m.usersAdded(email));
            load();
          }}
        />
      )}
      {limiting && (
        <LimitsDialog
          key={limiting.id}
          person={limiting}
          onSaved={load}
          onClose={() => {
            opener.current?.focus();
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
    </section>
  );
}

// A stored limit, named by its column for the line a phone draws it in, or that there is none,
// which is quieter, and which that line leaves out.
function Limit({ label, value }: { label: string; value: string | null }) {
  return value === null ? (
    <td className="muted meta unset" {...ROLE.cell}>
      {m.usersNoLimit}
    </td>
  ) : (
    <td className="meta named" data-label={label} {...ROLE.cell}>
      {value}
    </td>
  );
}
