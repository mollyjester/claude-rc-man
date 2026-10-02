import { ApiError, api } from "./api";
import { fakeApi, problem } from "./test/fakeApi";

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
  const init = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0][1];
  expect(new Headers(init.headers).get("content-type")).toBe("application/json");
  expect(init.credentials).toBe("same-origin");
});

test("turns a problem into an ApiError carrying everything the server said", async () => {
  fakeApi({
    "PATCH /api/me/settings": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors: [{ field: "prefix", message: "String should match pattern" }],
      screen: ["raw"],
    }),
  });
  const error = await failure(api.patch("/api/me/settings", { prefix: "" }));
  expect(error).toBeInstanceOf(ApiError);
  expect(error.status).toBe(422);
  expect(error.code).toBe("validation_failed");
  expect(error.title).toBe("Some of what was sent is not valid.");
  expect(error.errors).toEqual([{ field: "prefix", message: "String should match pattern" }]);
  expect(error.extra.screen).toEqual(["raw"]);
});

test("an error that is not a problem still becomes an ApiError", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })));
  const error = await failure(api.get("/api/me"));
  expect(error).toBeInstanceOf(ApiError);
  expect(error.status).toBe(502);
  expect(error.title).toBeTruthy();
});
