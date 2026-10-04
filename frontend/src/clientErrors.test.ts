import { MAX_QUEUED, noteFailure, requeue, takeReports } from "./clientErrors";

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const cause = new TypeError("Failed to fetch");

test("the queue keeps at most MAX_QUEUED different failures", () => {
  for (let i = 0; i < MAX_QUEUED + 5; i++) {
    noteFailure({ kind: "unreachable", method: "GET", path: `/api/planted/${i}`, cause });
  }
  noteFailure({ kind: "unreachable", method: "GET", path: "/api/planted/0", cause });
  const reports = takeReports();
  expect(reports).toHaveLength(MAX_QUEUED);
  expect(reports[0]).toMatchObject({ path: "/api/planted/0", count: 2 }); // a known one still counts
  expect(reports.map((r) => r.path)).not.toContain(`/api/planted/${MAX_QUEUED}`);
  expect(takeReports()).toEqual([]); // taking empties the queue
});

test("requeued reports merge with those noted meanwhile", () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(1_000);
  noteFailure({ kind: "unreachable", method: "GET", path: "/api/me", cause });
  const taken = takeReports();
  vi.setSystemTime(2_000);
  noteFailure({ kind: "unreachable", method: "GET", path: "/api/me", cause });
  noteFailure({ kind: "unreachable", method: "GET", path: "/api/me", cause });
  requeue(taken);
  expect(takeReports().map((r) => [r.count, r.first_at, r.last_at])).toEqual([[3, 1_000, 2_000]]);
});

test("a report holds the page, its visibility and whether the browser thought it was online", () => {
  window.history.pushState({}, "", "/settings?planted=query");
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  noteFailure({
    kind: "unexpected_response",
    method: "POST",
    path: "/api/servers",
    status: 500,
    contentType: "text/html",
    elapsedMs: 12.6,
    cause,
  });
  expect(takeReports()).toEqual([
    expect.objectContaining({
      kind: "unexpected_response",
      method: "POST",
      path: "/api/servers",
      status: 500,
      content_type: "text/html",
      elapsed_ms: 13,
      name: "TypeError",
      message: "Failed to fetch",
      page: "/settings",
      visibility: "hidden",
      online: false,
      count: 1,
    }),
  ]);
  online.mockRestore();
  visibility.mockRestore();
  window.history.pushState({}, "", "/");
});
