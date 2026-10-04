// Failures rcman could not see — it was not reached, its answer could not be
// read, or the page itself went wrong — kept in memory until a request works,
// when api.ts sends them to rcman's journal. Alike failures are one report,
// counted; the queue holds at most MAX_QUEUED of them.

// A fault in the page itself, not in rcman or the way to it.
export const PAGE_ERROR = "page_error";
export const MAX_QUEUED = 20;

// One failure, or several alike, as rcman takes it (routers/client_errors.py).
export type Report = {
  kind: string;
  method: string | null;
  path: string | null;
  status: number | null;
  name: string | null;
  message: string | null;
  content_type: string | null;
  page: string;
  visibility: string;
  online: boolean;
  elapsed_ms: number | null;
  count: number;
  first_at: number;
  last_at: number;
};

export type Failure = {
  kind: string;
  method?: string;
  path?: string;
  status?: number;
  contentType?: string | null;
  elapsedMs?: number;
  cause: unknown;
};

const queue = new Map<string, Report>();

// What makes failures alike.
const keyOf = (r: Report) => [r.kind, r.method, r.path, r.status, r.name].join("|");

function add(report: Report) {
  const key = keyOf(report);
  const known = queue.get(key);
  if (known) {
    known.count += report.count;
    known.first_at = Math.min(known.first_at, report.first_at);
    known.last_at = Math.max(known.last_at, report.last_at);
  } else if (queue.size < MAX_QUEUED) {
    queue.set(key, { ...report });
  }
}

// Note a failure: on the console, as it was, and in the queue, as a report
// without the query of the request or of the page.
export function noteFailure({
  kind,
  method,
  path,
  status,
  contentType,
  elapsedMs,
  cause,
}: Failure) {
  console.error("rcman:", kind, method, path, cause);
  const now = Date.now();
  add({
    kind,
    method: method ?? null,
    path: path === undefined ? null : path.split("?")[0],
    status: status ?? null,
    name: cause instanceof Error ? cause.name : null,
    message: cause instanceof Error ? cause.message : null,
    content_type: contentType ?? null,
    page: window.location.pathname,
    visibility: document.visibilityState,
    online: navigator.onLine,
    elapsed_ms: elapsedMs === undefined ? null : Math.max(0, Math.round(elapsedMs)),
    count: 1,
    first_at: now,
    last_at: now,
  });
}

// Every report queued, leaving the queue empty.
export function takeReports(): Report[] {
  const reports = [...queue.values()];
  queue.clear();
  return reports;
}

// Reports taken but not sent, back in the queue with any alike noted meanwhile.
export function requeue(reports: Report[]) {
  for (const report of reports) add(report);
}

// Empties the queue, for the tests.
export function forgetReports() {
  queue.clear();
}
