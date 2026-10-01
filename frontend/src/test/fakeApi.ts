// A stand-in for the backend: answers fetch() from a table of routes, records
// every call, and answers problems in the backend's problem-details shape.
import { vi } from "vitest";

export type Call = { method: string; path: string; query: URLSearchParams; body: unknown };
type Handler = (call: Call) => unknown;
export type Reply = Handler | Problem | Record<string, unknown> | unknown[];

export class Problem {
  constructor(
    public status: number,
    public body: Record<string, unknown>,
  ) {}
}

export function problem(status: number, code: string, title: string, extra: Record<string, unknown> = {}) {
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
      return new Response(JSON.stringify({ code: "not_found", title: "no route " + key, status: 404 }), {
        status: 404,
        headers: { "content-type": "application/problem+json" },
      });
    }
    const route = routes[key];
    const reply = typeof route === "function" ? route(call) : route;
    if (reply instanceof Problem) {
      return new Response(JSON.stringify(reply.body), {
        status: reply.status,
        headers: { "content-type": "application/problem+json" },
      });
    }
    return new Response(JSON.stringify(reply), { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    routes,
    made: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path),
  };
}

export const ME = { email: "alice@example.com", linux: "alice", admin: false, prefix: "alice" };
export const ADMIN = { ...ME, admin: true };
export const CONNECTED = {
  connection: "connected",
  account: { email: "alice@claude.test", org_name: "Org", subscription_type: "max" },
  login_expires_at: "2030-01-01T00:00:00+00:00",
};
