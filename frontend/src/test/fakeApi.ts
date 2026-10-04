// A stand-in for the backend: answers fetch() from a table of routes, records
// every call, and answers problems in the backend's problem-details shape.
import { vi } from "vitest";

export type Call = { method: string; path: string; query: URLSearchParams; body: unknown };
type Handler = (call: Call) => unknown;
export type Reply =
  | Handler
  | Problem
  | Unreachable
  | SignedOut
  | Record<string, unknown>
  | unknown[];

class Unreachable {}
// What a route answers to stand for rcman not reached: fetch rejects, as a browser's does.
export const UNREACHABLE_REPLY = new Unreachable();
class SignedOut {}
// What a route answers to stand for a sign-in that has ended: the proxy in front of
// rcman redirects to sign in, which a fetch that does not follow redirects sees as
// an opaque redirect.
export const SIGNED_OUT_REPLY = new SignedOut();

function opaqueRedirect(): Response {
  const response = new Response(null, { status: 200 });
  Object.defineProperty(response, "type", { value: "opaqueredirect" });
  Object.defineProperty(response, "status", { value: 0 });
  Object.defineProperty(response, "ok", { value: false });
  return response;
}

// A reply that ends, as a browser's fetch does, when the request's signal aborts.
function abortable<T>(reply: T | Promise<T>, signal: AbortSignal | null | undefined): Promise<T> {
  if (!signal) return Promise.resolve(reply);
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    Promise.resolve(reply).then(resolve, reject);
  });
}

export class Problem {
  constructor(
    public status: number,
    public body: Record<string, unknown>,
  ) {}
}

export function problem(
  status: number,
  code: string,
  title: string,
  extra: Record<string, unknown> = {},
) {
  return new Problem(status, { type: `urn:rcman:problem:${code}`, title, status, code, ...extra });
}

export type Routes = Record<string, Reply>;

export function fakeApi(routes: Routes) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://rcman.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const call = { method, path: url.pathname, query: url.searchParams, body };
    calls.push(call);
    const key = `${method} ${url.pathname}`;
    if (!(key in routes)) {
      return new Response(
        JSON.stringify({ code: "not_found", title: `no route ${key}`, status: 404 }),
        {
          status: 404,
          headers: { "content-type": "application/problem+json" },
        },
      );
    }
    const route = routes[key];
    // A handler may answer later, by returning a promise.
    const reply = await abortable(typeof route === "function" ? route(call) : route, init?.signal);
    if (reply === UNREACHABLE_REPLY) throw new TypeError("Failed to fetch");
    if (reply === SIGNED_OUT_REPLY) return opaqueRedirect();
    if (reply instanceof Problem) {
      return new Response(JSON.stringify(reply.body), {
        status: reply.status,
        headers: { "content-type": "application/problem+json" },
      });
    }
    return new Response(JSON.stringify(reply), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    routes,
    made: (method: string, path: string) =>
      calls.filter((c) => c.method === method && c.path === path),
  };
}

export const ME = { email: "alice@example.com", linux: "alice", admin: false, prefix: "alice" };
export const ADMIN_USER = { ...ME, admin: true };
export const CONNECTED = {
  connection: "connected",
  account: { email: "alice@claude.test", org_name: "Org", subscription_type: "max" },
  login_expires_at: "2030-01-01T00:00:00+00:00",
};
// What /api/claude answers before anyone has connected a Claude account.
export const NOT_CONNECTED = { connection: "not_connected", account: null, login_expires_at: null };
// What /api/me/settings answers for someone who has set nothing.
export const SETTINGS = {
  prefix: "alice",
  author_name: "",
  author_email: "",
  effective_author: null,
};
// What /api/attention answers when nothing needs an administrator's attention.
export const NOTHING_TO_ATTEND = {
  unrecognised: [],
  versions_differ: [],
  untested: false,
  versions_error: null,
};
