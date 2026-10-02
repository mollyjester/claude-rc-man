import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";

type Report = {
  unrecognised: { linux: string; path: string }[];
  versions_differ: string[];
  untested: boolean;
  versions_error: { code: string; title: string } | null;
};

// What administrators should look at, fetched when the app loads and on every
// change of page; never on a timer, and never kept between pages.
export function Attention() {
  const location = useLocation();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    let current = true;
    api.get<Report>("/api/attention").then(
      (r) => {
        if (current) {
          setReport(r);
          setError(null);
        }
      },
      (e) => {
        if (current) {
          setReport(null);
          setError(asApiError(e));
        }
      },
    );
    return () => {
      current = false;
    };
  }, [location.pathname]);

  if (error) return <ProblemView error={error} />;
  if (!report) return null;
  const { unrecognised, versions_differ: differ, untested, versions_error: failed } = report;
  if (unrecognised.length === 0 && differ.length === 0 && !untested && !failed) return null;
  return (
    <section aria-label={m.attentionTitle} className="attention">
      {unrecognised.length > 0 && (
        <p>
          {m.attentionUnrecognised(unrecognised.map((s) => `${s.linux}: ${s.path}`))}{" "}
          <Link to="/admin/servers">{m.navEveryone}</Link>
        </p>
      )}
      {differ.length > 0 && (
        <p>
          {m.attentionVersionsDiffer(differ)} <Link to="/admin/claude">{m.navVersions}</Link>
        </p>
      )}
      {untested && (
        <p>
          {m.attentionUntested} <Link to="/admin/claude">{m.navVersions}</Link>
        </p>
      )}
      {failed && (
        <>
          <p>
            {m.attentionVersionsFailed} <Link to="/admin/claude">{m.navVersions}</Link>
          </p>
          <ProblemView error={new ApiError(0, failed.code, failed.title)} />
        </>
      )}
    </section>
  );
}
