import {
  ApiError,
  api,
  forgetSending,
  problemFrom,
  REPORT_TIMEOUT_MS,
  SIGNED_OUT,
  UNEXPECTED_RESPONSE,
  UNREACHABLE,
} from "./api";
import { type Report, takeReports } from "./clientErrors";
import { m } from "./messages";
import { fakeApi, problem, SIGNED_OUT_REPLY, UNREACHABLE_REPLY } from "./test/fakeApi";

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("expected a failure");
}

test("returns the JSON body on success", async () => {
  fakeApi({ "GET /api/me": { linux: "alice" } });
  expect(await api.get("/api/me")).toEqual({ linux: "alice" });
});

test("sends JSON bodies and the query", async () => {
  const fake = fakeApi({ "POST /api/servers": { ok: true }, "GET /api/folders": { entries: [] } });
  await api.post("/api/servers", { path: "projects/x" });
  await api.get("/api/folders", { path: "projects", depth: 1 });
  expect(fake.calls[0].body).toEqual({ path: "projects/x" });
  expect(fake.calls[1].query.get("path")).toBe("projects");
  const init = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock
    .calls[0][1];
  expect(new Headers(init.headers).get("content-type")).toBe("application/json");
  expect(init.credentials).toBe("same-origin");
});

test("turns a problem into an ApiError carrying everything the server said", async () => {
  fakeApi({
    "PATCH /api/me/settings": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      {
        errors: [{ field: "prefix", message: "planted reason, as the server sent it" }],
        screen: ["raw"],
      },
    ),
  });
  const error = await failure(api.patch("/api/me/settings", { prefix: "" }));
  expect(error).toBeInstanceOf(ApiError);
  expect(error.status).toBe(422);
  expect(error.code).toBe("validation_failed");
  expect(error.title).toBe("Some of what was sent is not valid.");
  expect(error.errors).toEqual([
    { field: "prefix", message: "planted reason, as the server sent it" },
  ]);
  expect(error.extra.screen).toEqual(["raw"]);
});

test("an error that is not a problem still becomes an ApiError", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })),
  );
  const error = await failure(api.get("/api/me"));
  expect(error).toBeInstanceOf(ApiError);
  expect(error.status).toBe(502);
  expect(error.title).toBeTruthy();
});

test("problem details in an answer become the ApiError a failed request would be", () => {
  const error = problemFrom(
    {
      type: "urn:rcman:problem:git_failed",
      title: "planted title",
      status: 409,
      code: "git_failed",
      git: ["words"],
    },
    409,
  );
  expect(error).toBeInstanceOf(ApiError);
  expect([error.status, error.code, error.title]).toEqual([409, "git_failed", "planted title"]);
  expect(error.errors).toEqual([]);
  expect(error.extra).toEqual({ git: ["words"] });
  const odd = problemFrom(undefined, 502);
  expect([odd.status, odd.code]).toEqual([502, "unexpected_response"]);
  expect(odd.title).toBeTruthy();
});

// --- failures rcman could not see ------------------------------------------------

// Every field of a report, as the page sends it and rcman takes it.
const REPORT_FIELDS = [
  "content_type",
  "count",
  "elapsed_ms",
  "first_at",
  "kind",
  "last_at",
  "message",
  "method",
  "name",
  "online",
  "page",
  "path",
  "status",
  "visibility",
];

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  consoleError.mockRestore();
  vi.useRealTimers();
});

// fetch answering every request with one response.
function answering(make: () => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => make()),
  );
}

// Whatever the page has to do once a request has ended: sending its reports.
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("a request that cannot reach rcman is an ApiError of its own", async () => {
  fakeApi({ "GET /api/me": UNREACHABLE_REPLY });
  const error = await failure(api.get("/api/me"));
  expect(error).toBeInstanceOf(ApiError);
  expect([error.status, error.code, error.title]).toEqual([0, UNREACHABLE, m.unreachable]);
});

test("an answer cut off on the way is rcman not reached", async () => {
  answering(() => {
    const response = new Response("{}", { status: 200 });
    vi.spyOn(response, "text").mockRejectedValue(new TypeError("network error"));
    return response;
  });
  const error = await failure(api.get("/api/me"));
  expect([error.status, error.code]).toEqual([200, UNREACHABLE]);
  expect(takeReports().map((r) => [r.kind, r.status])).toEqual([[UNREACHABLE, 200]]);
});

test("a 502, 503 or 504 that is not JSON is rcman not reached, and reported with its status", async () => {
  for (const [status, text] of [
    [502, "<html>bad gateway</html>"],
    [503, ""],
    [504, "<html>gateway timeout</html>"],
  ] as const) {
    answering(() => new Response(text, { status, headers: { "content-type": "text/html" } }));
    const error = await failure(api.get("/api/servers"));
    expect([error.status, error.code, error.title]).toEqual([status, UNREACHABLE, m.unreachable]);
  }
  const reports = takeReports();
  expect(reports.map((r) => [r.kind, r.status, r.content_type])).toEqual([
    [UNREACHABLE, 502, "text/html"],
    [UNREACHABLE, 503, "text/html"],
    [UNREACHABLE, 504, "text/html"],
  ]);
});

test("another status that is not JSON is an answer the page does not understand", async () => {
  answering(() => new Response("<html>oops</html>", { status: 500 }));
  const html = await failure(api.get("/api/me"));
  expect([html.status, html.code, html.title]).toEqual([
    500,
    UNEXPECTED_RESPONSE,
    m.unexpectedResponse,
  ]);
  answering(() => new Response(JSON.stringify({ detail: "no code" }), { status: 502 }));
  const codeless = await failure(api.get("/api/me"));
  expect([codeless.status, codeless.code]).toEqual([502, UNEXPECTED_RESPONSE]);
  expect(takeReports().map((r) => [r.kind, r.status])).toEqual([
    [UNEXPECTED_RESPONSE, 500],
    [UNEXPECTED_RESPONSE, 502],
  ]);
});

test("a 502 or 504 problem from rcman is the server's, and not reported", async () => {
  fakeApi({
    "GET /api/servers": problem(502, "helper_unavailable", "rcman could not reach its helper."),
    "GET /api/claude": problem(504, "helper_timeout", "rcman's helper took too long to answer."),
  });
  const unavailable = await failure(api.get("/api/servers"));
  const slow = await failure(api.get("/api/claude"));
  expect([unavailable.code, unavailable.title]).toEqual([
    "helper_unavailable",
    "rcman could not reach its helper.",
  ]);
  expect(slow.code).toBe("helper_timeout");
  expect(takeReports()).toEqual([]);
  expect(consoleError).not.toHaveBeenCalled();
});

test("a successful answer that is not JSON is unreadable", async () => {
  answering(() => new Response("<html>a sign-in page</html>", { status: 200 }));
  const error = await failure(api.get("/api/me"));
  expect([error.status, error.code, error.title]).toEqual([
    200,
    UNEXPECTED_RESPONSE,
    m.unexpectedResponse,
  ]);
  expect(takeReports().map((r) => [r.kind, r.status, r.name])).toEqual([
    [UNEXPECTED_RESPONSE, 200, "SyntaxError"],
  ]);
});

test("the original error goes to the console", async () => {
  const cause = new TypeError("planted cause");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw cause;
    }),
  );
  await failure(api.get("/api/me"));
  expect(consoleError).toHaveBeenCalledWith("rcman:", UNREACHABLE, "GET", "/api/me", cause);
});

test("a problem rcman sent is not reported", async () => {
  fakeApi({
    "PATCH /api/me/settings": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
    ),
  });
  expect((await failure(api.patch("/api/me/settings", {}))).code).toBe("validation_failed");
  expect(takeReports()).toEqual([]);
  expect(consoleError).not.toHaveBeenCalled();
});

test("an unreachable request is reported after the next request that works", async () => {
  let down = true;
  const fake = fakeApi({
    "GET /api/me": () => (down ? UNREACHABLE_REPLY : { linux: "alice" }),
    "POST /api/client-errors": {},
  });
  await failure(api.get("/api/me"));
  await settle();
  expect(fake.made("POST", "/api/client-errors")).toHaveLength(0);
  down = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(fake.made("POST", "/api/client-errors")).toHaveLength(1));
  const sent = fake.made("POST", "/api/client-errors")[0].body as Report;
  expect(Object.keys(sent).sort()).toEqual(REPORT_FIELDS);
  expect(sent).toMatchObject({
    kind: UNREACHABLE,
    method: "GET",
    path: "/api/me",
    status: null,
    name: "TypeError",
    message: "Failed to fetch",
    content_type: null,
    count: 1,
  });
  expect(typeof sent.elapsed_ms).toBe("number");
  expect(sent.first_at).toBe(sent.last_at);
  expect(takeReports()).toEqual([]);
});

test("similar failures are one report with a count and first and last times", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  fakeApi({ "GET /api/me": UNREACHABLE_REPLY, "GET /api/servers": UNREACHABLE_REPLY });
  vi.setSystemTime(1_000);
  await failure(api.get("/api/me"));
  vi.setSystemTime(5_000);
  await failure(api.get("/api/me"));
  await failure(api.get("/api/servers"));
  const reports = takeReports();
  expect(reports.map((r) => [r.path, r.count, r.first_at, r.last_at])).toEqual([
    ["/api/me", 2, 1_000, 5_000],
    ["/api/servers", 1, 5_000, 5_000],
  ]);
});

test("a query is left out of the paths reported", async () => {
  window.history.pushState({}, "", "/somewhere?folder=planted-page-query");
  fakeApi({ "GET /api/folders": UNREACHABLE_REPLY });
  await failure(api.get("/api/folders", { path: "planted-request-query" }));
  const [report] = takeReports();
  expect([report.path, report.page]).toEqual(["/api/folders", "/somewhere"]);
  expect(JSON.stringify(report)).not.toContain("planted");
  window.history.pushState({}, "", "/");
});

test("a report that cannot be sent waits for the next success, and is not reported itself", async () => {
  let rcman = false;
  let reports = false;
  const fake = fakeApi({
    "GET /api/me": () => (rcman ? { linux: "alice" } : UNREACHABLE_REPLY),
    "POST /api/client-errors": () => (reports ? {} : UNREACHABLE_REPLY),
  });
  await failure(api.get("/api/me"));
  rcman = true;
  await api.get("/api/me");
  await vi.waitFor(() => expect(fake.made("POST", "/api/client-errors")).toHaveLength(1));
  await settle();
  reports = true;
  await api.get("/api/me");
  await vi.waitFor(() => expect(fake.made("POST", "/api/client-errors")).toHaveLength(2));
  await settle();
  const sent = fake.made("POST", "/api/client-errors").map((c) => c.body as Report);
  expect(sent.map((r) => [r.path, r.count])).toEqual([
    ["/api/me", 1],
    ["/api/me", 1],
  ]);
  expect(takeReports()).toEqual([]);
});

test("a report rcman refuses is dropped", async () => {
  let down = true;
  const fake = fakeApi({
    "GET /api/me": () => (down ? UNREACHABLE_REPLY : { linux: "alice" }),
    "POST /api/client-errors": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
    ),
  });
  await failure(api.get("/api/me"));
  down = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(fake.made("POST", "/api/client-errors")).toHaveLength(1));
  await settle();
  await api.get("/api/me");
  await settle();
  expect(fake.made("POST", "/api/client-errors")).toHaveLength(1);
  expect(takeReports()).toEqual([]);
});

test("reports are sent one at a time, by one sender, and those noted meanwhile follow", async () => {
  let down = true;
  let outstanding = 0;
  let mostOutstanding = 0;
  const answers: (() => void)[] = [];
  const fake = fakeApi({
    "GET /api/me": () => (down ? UNREACHABLE_REPLY : { linux: "alice" }),
    "GET /api/servers": UNREACHABLE_REPLY,
    "POST /api/client-errors": () => {
      outstanding += 1;
      mostOutstanding = Math.max(mostOutstanding, outstanding);
      return new Promise((resolve) => {
        answers.push(() => {
          outstanding -= 1;
          resolve({});
        });
      });
    },
  });
  const posts = () => fake.made("POST", "/api/client-errors");
  await failure(api.get("/api/me"));
  down = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(posts()).toHaveLength(1));
  // While the first report is out: a new failure, then a request that works.
  await failure(api.get("/api/servers"));
  await api.get("/api/me");
  await settle();
  expect(posts()).toHaveLength(1);
  answers[0]();
  await vi.waitFor(() => expect(posts()).toHaveLength(2));
  answers[1]();
  await settle();
  expect(posts().map((c) => (c.body as Report).path)).toEqual(["/api/me", "/api/servers"]);
  expect(mostOutstanding).toBe(1);
});

test("a sign-in that has ended is said so, not taken for rcman unreachable", async () => {
  fakeApi({ "GET /api/me": SIGNED_OUT_REPLY });
  const error = await failure(api.get("/api/me"));
  expect([error.status, error.code, error.title]).toEqual([0, SIGNED_OUT, m.signedOut]);
  const init = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock
    .calls[0][1];
  expect(init.redirect).toBe("manual");
  expect(takeReports().map((r) => [r.kind, r.path])).toEqual([[SIGNED_OUT, "/api/me"]]);
});

test("a report that meets an ended sign-in waits to be sent", async () => {
  let down = true;
  const fake = fakeApi({
    "GET /api/me": () => (down ? UNREACHABLE_REPLY : { linux: "alice" }),
    "POST /api/client-errors": SIGNED_OUT_REPLY,
  });
  await failure(api.get("/api/me"));
  down = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(fake.made("POST", "/api/client-errors")).toHaveLength(1));
  await settle();
  expect(takeReports().map((r) => [r.kind, r.path])).toEqual([[UNREACHABLE, "/api/me"]]);
});

test("a body the page cannot write is a fault in the page, not rcman unreachable", async () => {
  const fake = fakeApi({ "POST /api/servers": {} });
  let thrown: unknown;
  try {
    await api.post("/api/servers", { planted: BigInt(1) });
  } catch (error) {
    thrown = error;
  }
  expect(thrown).toBeInstanceOf(TypeError); // a fault the page's own code made
  expect(fake.calls).toEqual([]);
  expect(takeReports()).toEqual([]);
});

test("a report that gets no answer in time waits for the next success", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let down = true;
  let hang = true;
  const fake = fakeApi({
    "GET /api/me": () => (down ? UNREACHABLE_REPLY : { linux: "alice" }),
    "POST /api/client-errors": () => (hang ? new Promise(() => {}) : {}),
  });
  const posts = () => fake.made("POST", "/api/client-errors");
  await failure(api.get("/api/me"));
  down = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(posts()).toHaveLength(1));
  await vi.advanceTimersByTimeAsync(REPORT_TIMEOUT_MS + 1);
  hang = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(posts()).toHaveLength(2));
  await vi.advanceTimersByTimeAsync(0);
  expect((posts()[1].body as Report).path).toBe("/api/me");
  expect(takeReports()).toEqual([]);
});

test("how long a request took is measured on a clock that does not go back", async () => {
  const wallClock = vi.spyOn(Date, "now");
  wallClock.mockReturnValueOnce(1_000_000).mockReturnValue(900_000); // set back meanwhile
  const steady = vi.spyOn(performance, "now");
  steady.mockReturnValueOnce(5_000).mockReturnValue(5_120);
  try {
    fakeApi({ "GET /api/me": UNREACHABLE_REPLY });
    await failure(api.get("/api/me"));
    const [report] = takeReports();
    expect(report.elapsed_ms).toBe(120);
  } finally {
    wallClock.mockRestore();
    steady.mockRestore();
  }
});

test("a sender forgotten while a report is out drops it, rather than queueing it again", async () => {
  let down = true;
  let fail = () => {};
  const fake = fakeApi({
    "GET /api/me": () => (down ? UNREACHABLE_REPLY : { linux: "alice" }),
    "POST /api/client-errors": () =>
      new Promise((resolve) => {
        fail = () => resolve(UNREACHABLE_REPLY);
      }),
  });
  await failure(api.get("/api/me"));
  down = false;
  await api.get("/api/me");
  await vi.waitFor(() => expect(fake.made("POST", "/api/client-errors")).toHaveLength(1));
  forgetSending();
  fail();
  await settle();
  expect(takeReports()).toEqual([]);
});
