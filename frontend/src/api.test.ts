import { ApiError, api, SIGNED_OUT, UNREACHABLE } from "./api";
import { takeReports } from "./clientErrors";
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
  fakeApi({ "GET /api/me": { email: "alice@example.com" } });
  expect(await api.get("/api/me")).toEqual({ email: "alice@example.com" });
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

// --- failures rcman could not see ------------------------------------------------

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  consoleError.mockRestore();
  vi.useRealTimers();
});

test("a request that cannot reach rcman is an ApiError of its own", async () => {
  fakeApi({ "GET /api/me": UNREACHABLE_REPLY });
  const error = await failure(api.get("/api/me"));
  expect(error).toBeInstanceOf(ApiError);
  expect([error.status, error.code, error.title]).toEqual([0, UNREACHABLE, m.unreachable]);
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

test("a sign-in that has ended is said so, not taken for rcman unreachable", async () => {
  fakeApi({ "GET /api/me": SIGNED_OUT_REPLY });
  const error = await failure(api.get("/api/me"));
  expect([error.status, error.code, error.title]).toEqual([0, SIGNED_OUT, m.signedOut]);
  const init = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock
    .calls[0][1];
  expect(init.redirect).toBe("manual");
  expect(takeReports().map((r) => [r.kind, r.path])).toEqual([[SIGNED_OUT, "/api/me"]]);
});
