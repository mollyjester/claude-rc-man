import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";

type Person = { linux: string; email: string; admin: boolean; account: boolean };

function RemoveDialog({ linux, onDone, onCancel }: { linux: string; onDone: () => void; onCancel: () => void }) {
  const [home, setHome] = useState<"keep" | "delete">("keep");
  const [error, setError] = useState<ApiError | null>(null);

  async function remove(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await api.delete(`/api/users/${encodeURIComponent(linux)}`, { home });
      onDone();
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <div role="dialog" aria-label={m.usersRemoveTitle(linux)} className="dialog">
      <h2>{m.usersRemoveTitle(linux)}</h2>
      <form noValidate onSubmit={remove}>
        <p>{m.usersRemoveBody}</p>
        <label>
          <input type="radio" name="home" checked={home === "keep"} onChange={() => setHome("keep")} />
          {m.usersKeepHome}
        </label>
        <label>
          <input type="radio" name="home" checked={home === "delete"} onChange={() => setHome("delete")} />
          {m.usersDeleteHome}
        </label>
        <div className="actions">
          <button type="submit" className="danger">
            {m.usersRemoveConfirm}
          </button>
          <button type="button" onClick={onCancel}>
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
  const [linux, setLinux] = useState("");
  const [email, setEmail] = useState("");
  const [admin, setAdmin] = useState(false);
  const [adopt, setAdopt] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(() => {
    api.get<{ users: Person[] }>("/api/users").then((r) => setPeople(r.users), (e) => setError(asApiError(e)));
  }, []);
  useEffect(load, [load]);

  async function add(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    try {
      await api.post("/api/users", { linux, email, admin, adopt });
      setNotice(m.usersAdded(linux));
      setLinux("");
      setEmail("");
      setAdmin(false);
      setAdopt(false);
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
            <th>{m.usersLinux}</th>
            <th>{m.usersEmail}</th>
            <th>{m.usersRole}</th>
            <th>{m.usersAccount}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {people.map((p) => (
            <tr key={p.linux}>
              <td>{p.linux}</td>
              <td>{p.email}</td>
              <td>{p.admin ? m.usersAdmin : m.usersUser}</td>
              <td>{p.account ? m.usersAccountOk : m.usersAccountMissing}</td>
              <td>
                <button type="button" className="secondary-danger" onClick={() => setRemoving(p.linux)}>
                  {m.usersRemove}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {removing && (
        <RemoveDialog
          linux={removing}
          onCancel={() => setRemoving(null)}
          onDone={() => {
            setNotice(m.usersRemoved(removing));
            setRemoving(null);
            load();
          }}
        />
      )}
      <h2>{m.usersAdd}</h2>
      <form noValidate onSubmit={add}>
        <label>
          {m.usersLinux}
          <input type="text" value={linux} onChange={(e) => setLinux(e.target.value)} />
        </label>
        <label>
          {m.usersEmail}
          <input type="text" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />
          {m.usersAddAdmin}
        </label>
        <label>
          <input type="checkbox" checked={adopt} onChange={(e) => setAdopt(e.target.checked)} />
          {m.usersAddAdopt}
        </label>
        <button type="submit" className="primary">
          {m.usersAdd}
        </button>
      </form>
      {notice && <p role="status">{notice}</p>}
      <ProblemView error={error} />
    </section>
  );
}
