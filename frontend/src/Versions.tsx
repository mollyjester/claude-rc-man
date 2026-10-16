import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type Installed = { version: string; approved: boolean; previous: boolean; in_use: number };
type Drift = { workspace: number; name: string; email: string; version: string; approved: string };

type Report = {
  approved: string | null;
  previous: string | null;
  tested: string;
  installed: Installed[];
  drift: Drift[];
  upgrading: boolean;
};

// How often the report is read again while a roll-out runs, to see it end.
export const UPGRADE_POLL_MS = 3000;

// The Claude Code versions page, for administrators: what is approved, what is installed and
// running, and the servers on another version, and the roll-out of a version. A roll-out is one
// request that lasts as long as the checks do, so the page says it is working, and offers no
// second one meanwhile; the server refuses a second one regardless.
export function Versions() {
  const [report, setReport] = useState<Report | null>(null);
  // The newest release the release site names: undefined until asked, null where it is not known.
  const [latest, setLatest] = useState<string | null | undefined>(undefined);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [version, setVersion] = useState("");
  // The version being rolled out while its request is out.
  const [rolling, setRolling] = useState<string | null>(null);
  const working = useRef(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const help = useId();

  const load = useCallback(() => {
    api.get<Report>("/api/claude/versions").then(
      (answer) => {
        setReport(answer);
        setLoadError(null);
      },
      (e) => setLoadError(asApiError(e)),
    );
  }, []);

  useEffect(() => {
    load();
    api.get<{ latest: string | null }>("/api/claude/latest").then(
      (answer) => setLatest(answer.latest),
      (e) => setLoadError(asApiError(e)),
    );
  }, [load]);

  // While a roll-out runs, here or by another administrator, the report is read again until
  // it says none does.
  const upgrading = report?.upgrading === true;
  useEffect(() => {
    if (!upgrading) return;
    const timer = setInterval(load, UPGRADE_POLL_MS);
    return () => clearInterval(timer);
  }, [upgrading, load]);

  // Rolls a version out; whether it was approved.
  const rollOut = useCallback(
    async (which: string): Promise<boolean> => {
      if (working.current) return false;
      working.current = true;
      setRolling(which);
      setError(null);
      setNotice(null);
      try {
        const result = await api.post<Report>("/api/claude/upgrade", { version: which });
        setReport(result);
        setNotice(m.versionsUpgraded(result.approved ?? which));
        return true;
      } catch (e) {
        const failed = asApiError(e);
        setError(failed);
        // Another roll-out is what refused it: the report says so.
        if (failed.code === "claude_upgrade_busy") load();
        return false;
      } finally {
        working.current = false;
        setRolling(null);
      }
    },
    [load],
  );

  async function upgrade(event: FormEvent) {
    event.preventDefault();
    if (await rollOut(version.trim())) setVersion("");
  }

  // Nothing more is offered while a roll-out runs, here or by another administrator.
  const shut = rolling !== null || report?.upgrading === true;
  const status =
    rolling !== null
      ? m.versionsRolling(rolling)
      : (notice ?? (report?.upgrading ? m.versionsBusyElsewhere : null));

  return (
    <section>
      <h1>{m.versionsTitle}</h1>
      <p>{m.versionsIntro}</p>
      <ProblemView error={loadError} />
      {report === null && !loadError && <p>{m.loading}</p>}
      {report && (
        <>
          <dl>
            <dt>{m.versionsApproved}</dt>
            <dd>{report.approved ?? m.versionsNone}</dd>
            <dt>{m.versionsPrevious}</dt>
            <dd>{report.previous ?? m.versionsNone}</dd>
            <dt>{m.versionsTested}</dt>
            <dd>{report.tested}</dd>
          </dl>
          {report.approved !== null && report.approved !== report.tested && (
            <p className="warning">{m.versionsUntested}</p>
          )}
        </>
      )}
      <section aria-label={m.versionsLatestTitle}>
        <h2>{m.versionsLatestTitle}</h2>
        {latest === null && <p>{m.versionsLatestUnknown}</p>}
        {latest && report && latest === report.approved && <p>{m.versionsLatestApproved}</p>}
        {latest && report && latest !== report.approved && (
          <p>
            {m.versionsLatest(latest)}{" "}
            <button
              type="button"
              className="secondary"
              aria-label={m.versionsRolloutVersion(latest)}
              disabled={shut}
              onClick={() => rollOut(latest)}
            >
              {m.versionsRollout}
            </button>
          </p>
        )}
      </section>
      <section aria-label={m.versionsRolloutTitle}>
        <h2>{m.versionsRolloutTitle}</h2>
        <form noValidate onSubmit={upgrade}>
          <label>
            {m.versionsUpgrade}
            <input
              type="text"
              name="version"
              value={version}
              aria-describedby={help}
              onChange={(e) => setVersion(e.target.value)}
            />
          </label>
          <p id={help} className="muted">
            {m.versionsUpgradeHelp}
          </p>
          <div className="actions">
            <button type="submit" className="primary" disabled={shut}>
              {rolling === null ? m.versionsUpgradeGo : m.versionsUpgradeBusy}
            </button>
          </div>
        </form>
        <p role="status">{status}</p>
        <ProblemView error={error} labels={{ version: m.versionsUpgrade }} />
      </section>
      {report && (
        <>
          <InstalledVersions installed={report.installed} shut={shut} onUse={rollOut} />
          <DriftingServers drift={report.drift} />
        </>
      )}
    </section>
  );
}

function InstalledVersions({
  installed,
  shut,
  onUse,
}: {
  installed: Installed[];
  shut: boolean;
  onUse: (version: string) => void;
}) {
  return (
    <section aria-label={m.versionsInstalledTitle}>
      <h2>{m.versionsInstalledTitle}</h2>
      <table>
        <thead>
          <tr>
            <th>{m.versionsVersion}</th>
            <th>{m.versionsStanding}</th>
            <th>{m.versionsServersUsing}</th>
            <th>{m.versionsActions}</th>
          </tr>
        </thead>
        <tbody>
          {installed.map((i) => (
            <tr key={i.version}>
              <td>{i.version}</td>
              <td>
                {i.approved
                  ? m.versionsStandingApproved
                  : i.previous
                    ? m.versionsStandingPrevious
                    : ""}
              </td>
              <td>{m.versionsServers(i.in_use)}</td>
              <td>
                {!i.approved && (
                  <button
                    type="button"
                    className="secondary"
                    aria-label={m.versionsUseVersion(i.version)}
                    disabled={shut}
                    onClick={() => onUse(i.version)}
                  >
                    {m.versionsUse}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function DriftingServers({ drift }: { drift: Drift[] }) {
  return (
    <section aria-label={m.versionsDriftTitle}>
      <h2>{m.versionsDriftTitle}</h2>
      {drift.length === 0 && <p>{m.versionsDriftNone}</p>}
      {drift.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>{m.everyoneWorkspace}</th>
              <th>{m.versionsOwner}</th>
              <th>{m.versionsRunning}</th>
              <th>{m.versionsApproved}</th>
            </tr>
          </thead>
          <tbody>
            {drift.map((d) => (
              <tr key={d.workspace}>
                <td>{d.name}</td>
                <td>{d.email}</td>
                <td>{d.version}</td>
                <td>{d.approved}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
