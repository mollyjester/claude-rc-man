import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { ADMIN } from "./AdminPages";
import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type Report = { unrecognised: { email: string; workspace: string }[] };

// What administrators should look at, fetched when the app loads and on every
// change of page; never on a timer, and never kept between pages.
export function Attention() {
  const location = useLocation();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: location.pathname is what sets this off, not what it reads: the report is fetched again on every change of page
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
  if (!report || report.unrecognised.length === 0) return null;
  return (
    <section aria-label={m.attentionTitle} className="attention">
      <p>
        {m.attentionUnrecognised(report.unrecognised.map((s) => `${s.email}: ${s.workspace}`))}{" "}
        <Link to={ADMIN.everyone.path}>{ADMIN.everyone.label}</Link>
      </p>
    </section>
  );
}
