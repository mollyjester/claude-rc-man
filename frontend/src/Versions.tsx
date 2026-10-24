import {
  type FormEvent,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import { type ApiError, api } from "./api";
import { Dialog } from "./Dialog";
import { utcMoment } from "./format";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import { TableScroll } from "./TableScroll";

type Installed = {
  version: string;
  approved: boolean;
  previous: boolean;
  in_use: number;
  // Whether an administrator marked it as failed, which keeps it from being used again.
  failed: boolean;
};
type Drift = {
  workspace: number;
  name: string;
  email: string;
  version: string;
  approved: string;
  failed: boolean;
};
// A version marked as failed: when (ISO 8601), by whom, and why, null where no reason was given.
type Failure = { version: string; at: string; by: string; reason: string | null };

type Report = {
  approved: string | null;
  previous: string | null;
  tested: string;
  installed: Installed[];
  drift: Drift[];
  // The marks, the newest first; a version marked may no longer be installed.
  failed: Failure[];
  upgrading: boolean;
  // Whether rcman is being upgraded, while which no version is rolled out.
  rcman_upgrading?: boolean;
};

// How often the report is read again while a roll-out runs, to see it end.
export const UPGRADE_POLL_MS = 3000;

// Refusals that say the report shown is out of date: another roll-out runs, a version was marked
// as failed, or its mark removed, by another administrator; the report is read again.
const STALE = new Set([
  "claude_upgrade_busy",
  "claude_version_failed",
  "claude_version_not_failed",
]);

// Whether a version is marked as failed, installed or not.
const isFailed = (report: Report, version: string) =>
  report.failed.some((f) => f.version === version);

// The Claude Code versions page, for administrators: what is approved, what is installed and
// running, the servers on another version, the versions marked as failed, and the roll-out of a
// version. A roll-out is one request that lasts as long as the checks do, so the page says it is
// working, and offers no second one meanwhile; the server refuses a second one regardless. A
// mark, and its removal, may be made at any time, a roll-out under way included: the server
// stops the approval of a version marked while it is checked.
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
  const latestHeading = useId();
  const factsHeading = useId();
  const rolloutHeading = useId();
  const fields = useFieldErrors(error);
  // The dialog that marks a version as failed, with the version it opened with ("" for none).
  const [marking, setMarking] = useState<string | null>(null);
  // What was last done to a mark, and a removal refused, both said in the Failed versions card.
  const [markNotice, setMarkNotice] = useState<string | null>(null);
  const [markError, setMarkError] = useState<ApiError | null>(null);
  const clearing = useRef(false);
  // The version whose mark is being removed while its request is out; one at a time.
  const [removing, setRemoving] = useState<string | null>(null);
  // The Failed versions card's heading, which focus moves to after a mark or a removal: the
  // button that had it may have gone with its row.
  const failedHeading = useRef<HTMLHeadingElement>(null);
  const [moveFocus, setMoveFocus] = useState(false);
  useEffect(() => {
    if (moveFocus) {
      failedHeading.current?.focus();
      setMoveFocus(false);
    }
  }, [moveFocus]);

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
      setMarkNotice(null); // what was last done to a mark is no longer the news
      try {
        const result = await api.post<Report>("/api/claude/upgrade", { version: which });
        setReport(result);
        setNotice(m.versionsUpgraded(result.approved ?? which));
        return true;
      } catch (e) {
        const failed = asApiError(e);
        setError(failed);
        // Another roll-out, or a mark, is what refused it: the report says so.
        if (STALE.has(failed.code)) load();
        return false;
      } finally {
        working.current = false;
        setRolling(null);
      }
    },
    [load],
  );

  // Done to a mark: the report as it is now, said in the card, focus on its heading.
  const marked = (answer: Report, said: string) => {
    setReport(answer);
    setMarkError(null);
    setMarkNotice(said);
    setMoveFocus(true);
  };

  // Removes a version's mark at once: a mark is put back as easily, so nothing is asked first.
  async function clearMark(which: string) {
    if (clearing.current) return;
    clearing.current = true;
    setRemoving(which);
    setMarkError(null);
    setMarkNotice(null);
    try {
      const answer = await api.delete<Report>(`/api/claude/failures/${encodeURIComponent(which)}`);
      marked(answer, m.versionsCleared(which));
    } catch (e) {
      const failed = asApiError(e);
      setMarkError(failed);
      if (STALE.has(failed.code)) load();
      // the refusal is said under the card's heading; the button may go with the report read again
      setMoveFocus(true);
    } finally {
      clearing.current = false;
      setRemoving(null);
    }
  }

  async function upgrade(event: FormEvent) {
    event.preventDefault();
    if (await rollOut(version.trim())) setVersion("");
  }

  // Nothing more is offered while a roll-out runs, here or by another administrator, nor while
  // rcman is being upgraded.
  const shut = rolling !== null || report?.upgrading === true || report?.rcman_upgrading === true;
  const status =
    rolling !== null
      ? m.versionsRolling(rolling)
      : (notice ??
        (report?.upgrading
          ? m.versionsBusyElsewhere
          : report?.rcman_upgrading
            ? m.versionsRcmanUpgrading
            : null));

  return (
    <section className="cards">
      <div className="page-head">
        <h1>{m.versionsTitle}</h1>
      </div>
      <p className="page-intro muted">{m.versionsIntro}</p>
      <ProblemView error={loadError} />
      {report === null && !loadError && <p>{m.loading}</p>}
      <div className="versions-top">
        {report && (
          <section aria-labelledby={factsHeading} className="card">
            <h2 id={factsHeading} className="card-title">
              {m.versionsFactsTitle}
            </h2>
            <dl className="facts">
              <dt>{m.versionsApproved}</dt>
              <dd>{report.approved ?? m.versionsNone}</dd>
              <dt>{m.versionsPrevious}</dt>
              <dd>{report.previous ?? m.versionsNone}</dd>
              <dt>{m.versionsTested}</dt>
              <dd>{report.tested}</dd>
            </dl>
          </section>
        )}
        <div className="card">
          <section aria-labelledby={latestHeading} className="card-part">
            <h2 id={latestHeading}>{m.versionsLatestTitle}</h2>
            {latest === null && <p className="muted">{m.versionsLatestUnknown}</p>}
            {latest && report && latest === report.approved && (
              <p className="muted">{m.versionsLatestApproved}</p>
            )}
            {latest && report && latest !== report.approved && isFailed(report, latest) && (
              <p className="muted">{m.versionsLatestFailed(latest)}</p>
            )}
            {latest && report && latest !== report.approved && !isFailed(report, latest) && (
              <div className="latest">
                <p>{m.versionsLatest(latest)}</p>
                <button
                  type="button"
                  className="secondary"
                  aria-label={m.versionsRolloutVersion(latest)}
                  disabled={shut}
                  onClick={() => rollOut(latest)}
                >
                  {m.versionsRollout}
                </button>
              </div>
            )}
          </section>
          <section aria-labelledby={rolloutHeading} className="card-part">
            <h2 id={rolloutHeading}>{m.versionsRolloutTitle}</h2>
            <form noValidate onSubmit={upgrade} ref={fields.form}>
              <label>
                {m.versionsUpgrade}
                <input
                  type="text"
                  name="version"
                  value={version}
                  onChange={(e) => setVersion(e.target.value)}
                  {...fields.input("version", help)}
                />
              </label>
              {fields.message("version")}
              <p id={help} className="help muted">
                {m.versionsUpgradeHelp}
              </p>
              <div className="actions">
                <button type="submit" className="primary" disabled={shut}>
                  {rolling === null ? m.versionsUpgradeGo : m.versionsUpgradeBusy}
                </button>
              </div>
            </form>
            <p role="status">{status}</p>
            <ProblemView error={error} labels={{ version: m.versionsUpgrade }} inline />
          </section>
        </div>
      </div>
      {report && (
        <>
          <InstalledVersions
            installed={report.installed}
            shut={shut}
            onUse={rollOut}
            onMark={setMarking}
          />
          <FailedVersions
            failed={report.failed}
            heading={failedHeading}
            notice={markNotice}
            error={markError}
            onMark={() => setMarking("")}
            removing={removing !== null}
            onClear={clearMark}
          />
          <DriftingServers drift={report.drift} />
        </>
      )}
      {marking !== null && (
        <MarkFailedDialog
          version={marking}
          onMarked={(answer, which) => {
            setMarking(null);
            marked(answer, m.versionsMarked(which));
          }}
          onStale={load}
          onCancel={() => setMarking(null)}
        />
      )}
    </section>
  );
}

// The pill of a version marked as failed.
const FailedBadge = () => <span className="badge badge-problem">{m.versionsStandingFailed}</span>;

// A table in a card of its own, named by its heading, with what is done to all of it, if
// anything, at the head's other end; the heading takes focus where the page moves it there.
function TableCard({
  title,
  action,
  heading,
  children,
}: {
  title: string;
  action?: ReactNode;
  heading?: RefObject<HTMLHeadingElement | null>;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="card flush">
      <div className="card-head">
        <h2 id={id} ref={heading} tabIndex={heading ? -1 : undefined}>
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function InstalledVersions({
  installed,
  shut,
  onUse,
  onMark,
}: {
  installed: Installed[];
  shut: boolean;
  onUse: (version: string) => void;
  onMark: (version: string) => void;
}) {
  return (
    <TableCard title={m.versionsInstalledTitle}>
      <TableScroll label={m.tableTitle}>
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
                  {i.approved ? (
                    m.versionsStandingApproved
                  ) : i.previous ? (
                    m.versionsStandingPrevious
                  ) : i.failed ? (
                    <FailedBadge />
                  ) : (
                    ""
                  )}
                </td>
                <td>{m.versionsServers(i.in_use)}</td>
                <td className="row-actions">
                  {/* the approved version can be neither used again nor marked; a marked one
                      is used again only once its mark is removed */}
                  {!i.approved && !i.failed && (
                    <div className="actions">
                      <button
                        type="button"
                        className="secondary"
                        aria-label={m.versionsUseVersion(i.version)}
                        disabled={shut}
                        onClick={() => onUse(i.version)}
                      >
                        {m.versionsUse}
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        aria-label={m.versionsMarkFailedVersion(i.version)}
                        onClick={() => onMark(i.version)}
                      >
                        {m.versionsMarkFailed}
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </TableCard>
  );
}

function DriftingServers({ drift }: { drift: Drift[] }) {
  return (
    <TableCard title={m.versionsDriftTitle}>
      {drift.length === 0 && <p className="card-empty muted">{m.versionsDriftNone}</p>}
      {drift.length > 0 && (
        <TableScroll label={m.tableTitle}>
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
                  <td>
                    {d.version}
                    {d.failed && (
                      <>
                        {" "}
                        <FailedBadge />
                      </>
                    )}
                  </td>
                  <td>{d.approved}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </TableCard>
  );
}

// The versions marked as failed, the newest mark first, each with why, by whom and when, and
// Remove mark; at the head, Mark a version as failed, for one that is not installed. What was
// last done to a mark is said in the card's own live region, and a removal refused under it.
function FailedVersions({
  failed,
  heading,
  notice,
  error,
  removing,
  onMark,
  onClear,
}: {
  failed: Failure[];
  heading: RefObject<HTMLHeadingElement | null>;
  notice: string | null;
  error: ApiError | null;
  // Whether a removal is out, while which Remove mark waits.
  removing: boolean;
  onMark: () => void;
  onClear: (version: string) => void;
}) {
  return (
    <TableCard
      title={m.versionsFailedTitle}
      heading={heading}
      action={
        <button type="button" className="secondary" onClick={onMark}>
          {m.versionsMarkAnyFailed}
        </button>
      }
    >
      <p role="status" className="card-status">
        {notice}
      </p>
      {error && (
        <div className="card-status">
          <ProblemView error={error} />
        </div>
      )}
      {failed.length === 0 && <p className="card-empty muted">{m.versionsFailedNone}</p>}
      {failed.length > 0 && (
        <TableScroll label={m.tableTitle}>
          <table>
            <thead>
              <tr>
                <th>{m.versionsVersion}</th>
                <th>{m.versionsReason}</th>
                <th>{m.versionsMarkedBy}</th>
                <th>{m.versionsMarkedAt}</th>
                <th>{m.versionsActions}</th>
              </tr>
            </thead>
            <tbody>
              {failed.map((f) => (
                <tr key={f.version}>
                  <td>{f.version}</td>
                  <td className="wrap">
                    {f.reason ?? <span className="muted">{m.versionsNoReason}</span>}
                  </td>
                  <td className="wrap">{f.by}</td>
                  <td className="when">
                    <time dateTime={f.at}>{utcMoment(f.at)}</time>
                  </td>
                  <td className="row-actions">
                    <div className="actions">
                      <button
                        type="button"
                        className="secondary"
                        aria-label={m.versionsClearFailedVersion(f.version)}
                        disabled={removing}
                        onClick={() => onClear(f.version)}
                      >
                        {m.versionsClearFailed}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </TableCard>
  );
}

// Marks a version as failed, with an optional reason: the version a row opened it with, or one
// typed. The server judges both; a refusal is shown in the dialog, which keeps what was typed.
// A refusal that says the report is out of date, a mark made meanwhile elsewhere, reads it again.
function MarkFailedDialog({
  version: opened,
  onMarked,
  onStale,
  onCancel,
}: {
  version: string;
  onMarked: (report: Report, version: string) => void;
  onStale: () => void;
  onCancel: () => void;
}) {
  const [version, setVersion] = useState(opened);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const [error, setError] = useState<ApiError | null>(null);
  const fields = useFieldErrors(error);
  const help = useId();

  async function mark(event: FormEvent) {
    event.preventDefault();
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    const which = version.trim();
    try {
      // the version as typed but for the spaces around it, as the roll-out form sends its
      // own, empty too, for the server to judge; the reason only where one was typed
      const answer = await api.put<Report>(
        "/api/claude/failures",
        reason === "" ? { version: which } : { version: which, reason },
      );
      onMarked(answer, which);
    } catch (e) {
      const failed = asApiError(e);
      setError(failed);
      if (STALE.has(failed.code) || failed.code === "claude_version_approved") onStale();
    } finally {
      working.current = false;
      setBusy(false);
    }
  }

  return (
    <Dialog
      owner="MarkFailedDialog"
      title={m.versionsMarkAnyFailed}
      onClose={onCancel}
      closable={!busy}
    >
      <form noValidate onSubmit={mark} ref={fields.form}>
        <p>{m.versionsMarkExplain}</p>
        <label>
          {m.versionsVersion}
          <input
            type="text"
            name="version"
            value={version}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setVersion(e.target.value)}
            {...fields.input("version")}
          />
        </label>
        {fields.message("version")}
        <label>
          {m.versionsReason}
          <input
            type="text"
            name="reason"
            value={reason}
            autoComplete="off"
            onChange={(e) => setReason(e.target.value)}
            {...fields.input("reason", help)}
          />
        </label>
        {fields.message("reason")}
        <p id={help} className="help muted">
          {m.versionsReasonHelp}
        </p>
        <div className="actions footer">
          <button type="submit" className="primary" disabled={busy}>
            {busy ? m.versionsMarkBusy : m.versionsMarkFailed}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
      <ProblemView
        error={error}
        labels={{ version: m.versionsVersion, reason: m.versionsReason }}
        inline
      />
    </Dialog>
  );
}
