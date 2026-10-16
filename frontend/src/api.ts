// The one way the frontend talks to the backend. Every failure becomes an
// ApiError: the server's problem details as sent, or the page's own — rcman not
// reached, the sign-in ended, or an answer it could not read — which is also
// reported to rcman (clientErrors.ts).
import { noteFailure, requeue, takeReports } from "./clientErrors";
import { m } from "./messages";

// The page's own codes: rcman was not reached, the sign-in has ended, or rcman
// answered in a way the page does not understand.
export const UNREACHABLE = "unreachable";
export const SIGNED_OUT = "signed_out";
export const UNEXPECTED_RESPONSE = "unexpected_response";
// How long a report may take to send before it is given up, to be sent later.
export const REPORT_TIMEOUT_MS = 10_000;
// The statuses at which an answer that is not JSON is the proxy in front of
// rcman saying it could not reach it; rcman's own answers are JSON.
const PROXY_FAILED = new Set([502, 503, 504]);

export type FieldError = { field: string; message: string };

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public title: string,
    public errors: FieldError[] = [],
    public extra: Record<string, unknown> = {},
  ) {
    super(title);
  }
}

type Query = Record<string, string | number | boolean | undefined>;

// Problem details as the server sent them, in a failed response or in an
// answer that reports one, as an ApiError.
export function problemFrom(data: unknown, status: number): ApiError {
  const {
    status: _status,
    code,
    title,
    errors,
    type: _type,
    ...extra
  } = (data ?? {}) as Record<string, unknown>;
  void _status;
  void _type;
  return new ApiError(
    status,
    typeof code === "string" ? code : UNEXPECTED_RESPONSE,
    typeof title === "string" ? title : m.unexpectedResponse,
    Array.isArray(errors) ? (errors as FieldError[]) : [],
    extra,
  );
}

// A request; a quiet one, reporting failures itself, neither notes a failure
// nor sends the reports.
async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  query?: Query,
  { quiet = false }: { quiet?: boolean } = {},
): Promise<T> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const url = params.size ? `${path}?${params}` : path;
  // Written before anything is sent: a body that cannot be written is the page's fault.
  const sent = body === undefined ? undefined : JSON.stringify(body);
  const started = performance.now();
  const note = (kind: string, cause: unknown, answer?: Response) => {
    if (quiet) return;
    const status = answer?.status;
    const contentType = answer?.headers.get("content-type");
    noteFailure({
      kind,
      method,
      path,
      status,
      contentType,
      elapsedMs: performance.now() - started,
      cause,
    });
  };
  // A report is given up after REPORT_TIMEOUT_MS, so that one never answered
  // does not hold up those after it.
  const abort = new AbortController();
  const timer = quiet ? setTimeout(() => abort.abort(), REPORT_TIMEOUT_MS) : undefined;
  let response: Response | undefined;
  let text: string;
  try {
    response = await fetch(url, {
      method,
      credentials: "same-origin",
      // Signed out, the proxy in front of rcman redirects to sign in, elsewhere;
      // rcman's own routes never redirect.
      redirect: "manual",
      headers: sent === undefined ? {} : { "content-type": "application/json" },
      body: sent,
      signal: abort.signal,
    });
    if (response.type === "opaqueredirect") {
      note(SIGNED_OUT, undefined);
      throw new ApiError(0, SIGNED_OUT, m.signedOut);
    }
    text = await response.text();
  } catch (cause) {
    if (cause instanceof ApiError) throw cause;
    note(UNREACHABLE, cause, response);
    throw new ApiError(response?.status ?? 0, UNREACHABLE, m.unreachable);
  } finally {
    clearTimeout(timer);
  }
  const { status } = response;
  let data: unknown;
  let unreadable: unknown;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch (cause) {
    unreadable = cause;
  }
  if (!response.ok) {
    if (data === undefined && PROXY_FAILED.has(status)) {
      note(UNREACHABLE, unreadable, response);
      throw new ApiError(status, UNREACHABLE, m.unreachable);
    }
    const problem = problemFrom(data, status);
    if (problem.code === UNEXPECTED_RESPONSE) note(UNEXPECTED_RESPONSE, unreadable, response);
    throw problem;
  }
  if (unreadable !== undefined) {
    note(UNEXPECTED_RESPONSE, unreadable, response);
    throw new ApiError(status, UNEXPECTED_RESPONSE, m.unexpectedResponse);
  }
  if (!quiet) void sendReports();
  return data as T;
}

// The sender sending now, if one is; each is numbered, so that one the tests
// forgot (forgetSending) stops at its next report.
let sending: number | null = null;
let senders = 0;

// The failures noted, sent to rcman one at a time, by one sender at a time,
// until none are left. A report rcman refuses is dropped; once rcman is not
// reached, or the sign-in has ended, it and those not yet sent wait for the
// next request that works.
async function sendReports() {
  if (sending !== null) return;
  const sender = ++senders;
  sending = sender;
  try {
    for (let reports = takeReports(); reports.length > 0; reports = takeReports()) {
      for (const [i, report] of reports.entries()) {
        if (sending !== sender) return;
        try {
          await request("POST", "/api/client-errors", report, undefined, { quiet: true });
        } catch (error) {
          if (
            error instanceof ApiError &&
            (error.code === UNREACHABLE || error.code === SIGNED_OUT)
          ) {
            // A sender forgotten meanwhile drops what it holds.
            if (sending === sender) requeue(reports.slice(i));
            return;
          }
        }
      }
    }
  } finally {
    if (sending === sender) sending = null;
  }
}

// Forgets the sender sending now, for the tests.
export function forgetSending() {
  sending = null;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, undefined, query),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string, query?: Query) => request<T>("DELETE", path, undefined, query),
};
