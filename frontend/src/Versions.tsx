import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type Versions = {
  approved: string;
  tested: string;
  untested: boolean;
  differs: string[];
  users: { linux: string; installed: string | null }[];
};

export function Versions() {
  const [versions, setVersions] = useState<Versions | null>(null);
  const [version, setVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(() => {
    api.get<Versions>("/api/claude/versions").then(setVersions, (e) => setError(asApiError(e)));
  }, []);
  useEffect(load, [load]);

  async function upgrade(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<{ approved: string }>("/api/claude/upgrade", { version });
      setNotice(m.versionsUpgraded(result.approved));
      load();
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h1>{m.versionsTitle}</h1>
      {versions && (
        <>
          <dl>
            <dt>{m.versionsApproved}</dt>
            <dd>{versions.approved}</dd>
            <dt>{m.versionsTested}</dt>
            <dd>{versions.tested}</dd>
          </dl>
          {versions.untested && <p className="warning">{m.versionsUntested}</p>}
          <table>
            <thead>
              <tr>
                <th>{m.versionsLinux}</th>
                <th>{m.versionsInstalled}</th>
              </tr>
            </thead>
            <tbody>
              {versions.users.map((u) => (
                <tr key={u.linux}>
                  <td>{u.linux}</td>
                  <td>
                    {u.installed ?? m.versionsNotInstalled}
                    {versions.differs.includes(u.linux) && ` (${m.versionsDiffers})`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <form noValidate onSubmit={upgrade}>
        <label>
          {m.versionsUpgrade}
          <input
            type="text"
            name="version"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
          />
        </label>
        <button type="submit" className="primary" disabled={busy}>
          {m.versionsUpgradeGo}
        </button>
      </form>
      {notice && <p role="status">{notice}</p>}
      <ProblemView error={error} labels={{ version: m.versionsUpgrade }} />
    </section>
  );
}
