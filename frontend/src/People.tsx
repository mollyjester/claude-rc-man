import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type Person = { id: number; email: string; admin: boolean; state: string; workspaces: number };

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

export function People() {
  const [people, setPeople] = useState<Person[]>([]);
  const [email, setEmail] = useState("");
  const [admin, setAdmin] = useState(false);
  const [removing, setRemoving] = useState<Person | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

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
      <table>
        <thead>
          <tr>
            <th>{m.usersEmail}</th>
            <th>{m.usersRole}</th>
            <th>{m.usersWorkspaces}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {people.map((p) => (
            <tr key={p.id}>
              <td>{p.email}</td>
              <td>{p.admin ? m.usersAdmin : m.usersUser}</td>
              <td>{p.workspaces}</td>
              <td>
                <button
                  type="button"
                  className="secondary-danger"
                  onClick={() => {
                    setNotice(null);
                    setRemoving(p);
                  }}
                >
                  {m.usersRemove}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
      <form noValidate onSubmit={add}>
        <label>
          {m.usersEmail}
          <input
            type="text"
            name="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label>
          <input
            type="checkbox"
            name="admin"
            checked={admin}
            onChange={(e) => setAdmin(e.target.checked)}
          />
          {m.usersAddAdmin}
        </label>
        <button type="submit" className="primary">
          {m.usersAdd}
        </button>
      </form>
      {notice && <p role="status">{notice}</p>}
      <ProblemView error={error} labels={{ email: m.usersEmail, admin: m.usersAddAdmin }} />
    </section>
  );
}
