import {
  type FormEvent,
  Fragment,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Link } from "react-router-dom";

import { type ApiError, api } from "./api";
import { browser } from "./browser";
import { Dialog } from "./Dialog";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type Report = {
  release: string | null;
  enabled: boolean;
  available: boolean;
  // The problem a request would be refused with now, as the server words it.
  reason: { title: string; reason: string } | null;
  state: string;
  started: string | null;
  finished: string | null;
  from: string | null;
  to: string | null;
  notices: string[];
  error: string | null;
};

// How often the report is read again while an upgrade is under way, to see it through.
export const RCMAN_POLL_MS = 3000;
// Where the installation guide says how the option is turned on, and how to roll back.
export const GUIDE_WEB_UPGRADE = "/help/installation#upgrading-from-the-web-interface";
export const GUIDE_ROLLBACK = "/help/installation#upgrading-and-rolling-back";

const UNDER_WAY = new Set(["requested", "running"]);

// A moment as a date and a time in UTC, as the audit trail shows it.
function when(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toISOString().slice(0, 19).replace("T", " ");
}

// A commit, whole, as code that no translation touches and that breaks where it must.
const Commit = ({ commit }: { commit: string }) => (
  <code className="commit" translate="no">
    {commit}
  </code>
);

// The Upgrade rcman page, for administrators: the release rcman runs, the last upgrade, and
// asking for another, after a question that says what it cuts off. While one is under way the
// page reads the report again quietly, through the restart of rcman it makes, which it does not
// report as a failure, and says how it goes in its one live region; once it is done, it offers
// to reload the page, which the new rcman serves.
export function Upgrade() {
  const [report, setReport] = useState<Report | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [asking, setAsking] = useState(false);
  // Whether this page saw an upgrade under way, so that it offers the reload at its end.
  const [followed, setFollowed] = useState(false);
  // Whether the last reading while following failed, as while rcman restarts.
  const [unanswered, setUnanswered] = useState(false);
  const factsHeading = useId();
  // The card's heading, which focus moves to once a request is admitted: the button that had
  // it goes, with the dialog it was in.
  const cardHeading = useRef<HTMLHeadingElement>(null);
  const [moveFocus, setMoveFocus] = useState(false);
  useEffect(() => {
    if (moveFocus && report !== null) {
      cardHeading.current?.focus();
      setMoveFocus(false);
    }
  }, [moveFocus, report]);

  const show = useCallback((answer: Report) => {
    setReport(answer);
    setUnanswered(false);
    if (UNDER_WAY.has(answer.state)) setFollowed(true);
  }, []);

  useEffect(() => {
    api.get<Report>("/api/rcman/upgrade").then(show, (e) => setLoadError(asApiError(e)));
  }, [show]);

  // While an upgrade is under way the report is read again until it is not. A reading that
  // fails is rcman restarting, or not reached for a moment: the next one tries again.
  const underWay = report !== null && UNDER_WAY.has(report.state);
  useEffect(() => {
    if (!underWay) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const next = () => {
      timer = setTimeout(async () => {
        try {
          const answer = await api.poll<Report>("/api/rcman/upgrade");
          if (stopped) return;
          show(answer);
          if (UNDER_WAY.has(answer.state)) next();
        } catch {
          if (stopped) return;
          setUnanswered(true);
          next();
        }
      }, RCMAN_POLL_MS);
    };
    next();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [underWay, show]);

  const progress =
    report === null || !underWay
      ? null
      : unanswered
        ? m.upgradeRestarting
        : report.state === "requested"
          ? m.upgradeRequested
          : m.upgradeRunning;

  return (
    <section className="cards">
      <div className="page-head">
        <h1>{m.upgradeRcman}</h1>
        {/* offered only while the server would take it */}
        {report?.enabled && report.available && !underWay && (
          <button type="button" className="primary" onClick={() => setAsking(true)}>
            {m.upgradeRcman}
          </button>
        )}
      </div>
      <p className="page-intro muted">{m.upgradeIntro}</p>
      <ProblemView error={loadError} />
      {report === null && !loadError && <p>{m.loading}</p>}
      {report && (
        <section aria-labelledby={factsHeading} className="card">
          <h2 id={factsHeading} className="card-title" tabIndex={-1} ref={cardHeading}>
            {m.upgradeFactsTitle}
          </h2>
          <Facts report={report} />
          <div className="upgrade-act">
            {/* the page's one live region: how an upgrade goes, and how it ended */}
            <div role="status" className="upgrade-progress">
              {progress ? (
                <MessageBar kind="info">
                  <p>{progress}</p>
                </MessageBar>
              ) : (
                <Outcome report={report} followed={followed} />
              )}
            </div>
            {report.state === "succeeded" && followed && (
              <div className="actions">
                <button type="button" className="primary" onClick={() => browser.reload()}>
                  {m.upgradeReload}
                </button>
              </div>
            )}
            {!report.enabled && (
              <MessageBar kind="info">
                <p>
                  {m.upgradeOff} <Link to={GUIDE_WEB_UPGRADE}>{m.upgradeOffLink}</Link>.
                </p>
              </MessageBar>
            )}
            {/* why Upgrade rcman is not beside the heading */}
            {report.enabled && !underWay && !report.available && report.reason && (
              <p className="muted">{report.reason.title}</p>
            )}
          </div>
        </section>
      )}
      {asking && (
        <UpgradeDialog
          onAsked={(answer) => {
            setAsking(false);
            show(answer);
            setMoveFocus(true);
          }}
          onCancel={() => setAsking(false)}
        />
      )}
    </section>
  );
}

// The release rcman runs and the last upgrade, as rows of a definition list.
function Facts({ report }: { report: Report }) {
  const rows: [string, ReactNode][] = [
    [
      m.upgradeRelease,
      report.release ? <Commit key="release" commit={report.release} /> : m.upgradeReleaseUnknown,
    ],
    [m.upgradeLast, m.upgradeStates[report.state] ?? report.state],
  ];
  if (report.started) rows.push([m.upgradeStarted, when(report.started)]);
  if (report.finished) rows.push([m.upgradeFinished, when(report.finished)]);
  if (report.from) rows.push([m.upgradeFrom, <Commit key="from" commit={report.from} />]);
  if (report.to) rows.push([m.upgradeTo, <Commit key="to" commit={report.to} />]);
  if (report.notices.length > 0)
    rows.push([
      m.upgradeNotices,
      <ul key="notices" className="upgrade-notices">
        {report.notices.map((notice) => (
          <li key={notice}>{notice}</li>
        ))}
      </ul>,
    ]);
  return (
    <dl className="facts">
      {rows.map(([term, value]) => (
        <Fragment key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

// How the last upgrade ended, where there is something to do about it: a failure, with what it
// said and the way back, which is from a shell; a request nothing acted on; or, for an upgrade
// this page followed, its success, after which the page offers the reload that brings the new
// page.
function Outcome({ report, followed }: { report: Report; followed: boolean }) {
  if (report.state === "failed")
    return (
      <MessageBar kind="problem">
        {report.error ? (
          <>
            <p>{m.upgradeFailed}</p>
            <p className="upgrade-error">{report.error}</p>
          </>
        ) : (
          <p>{m.upgradeFailedUnsaid}</p>
        )}
        <p>
          {m.upgradeRollBack} <Link to={GUIDE_ROLLBACK}>{m.upgradeRollBackLink}</Link>.
        </p>
      </MessageBar>
    );
  if (report.state === "not_started")
    return (
      <MessageBar kind="warning">
        <p>{m.upgradeNotStarted}</p>
      </MessageBar>
    );
  if (report.state === "succeeded" && followed)
    return (
      <MessageBar kind="success">
        <p>{m.upgradeSucceeded}</p>
      </MessageBar>
    );
  return null;
}

// The question before an upgrade: what it cuts off, and Upgrade rcman, which asks for it. While
// the request is out the dialog stays, its button keeping its words; a refusal is shown in it.
function UpgradeDialog({
  onAsked,
  onCancel,
}: {
  onAsked: (report: Report) => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function confirm(event: FormEvent) {
    event.preventDefault();
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      onAsked(await api.post<Report>("/api/rcman/upgrade"));
    } catch (e) {
      setError(asApiError(e));
    } finally {
      working.current = false;
      setBusy(false);
    }
  }

  return (
    <Dialog owner="UpgradeDialog" title={m.upgradeRcman} onClose={onCancel} closable={!busy}>
      <form noValidate onSubmit={confirm}>
        <p>{m.upgradeConfirm}</p>
        <ul className="upgrade-consequences">
          {m.upgradeConsequences.map((consequence) => (
            <li key={consequence}>{consequence}</li>
          ))}
        </ul>
        <ProblemView error={error} />
        <div className="actions footer">
          <button type="button" onClick={onCancel} disabled={busy}>
            {m.cancel}
          </button>
          <button type="submit" className="primary" disabled={busy} aria-busy={busy}>
            {m.upgradeRcman}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
