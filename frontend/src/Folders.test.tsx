import { act, renderHook, waitFor } from "@testing-library/react";

import { useFolderTree } from "./Folders";
import { fakeApi } from "./test/fakeApi";

test("revealing a folder whose parents are open already changes nothing", async () => {
  const fake = fakeApi({ "GET /api/folders": (c) => ({ path: c.query.get("path") ?? "", entries: [] }) });
  const { result } = renderHook(() => useFolderTree());
  act(() => result.current.reveal("projects/alpha"));
  const open = result.current.open;
  expect(open).toEqual(new Set(["projects"])); // positive control: revealing opened its parent
  await waitFor(() => expect(fake.made("GET", "/api/folders")).toHaveLength(2));

  act(() => result.current.reveal("projects/beta"));
  expect(result.current.open).toBe(open);
  act(() => result.current.reveal("other"));
  expect(result.current.open).toBe(open);
});

test("reloading a folder fetches its contents again, though they were fetched before", async () => {
  let round = 1;
  const fake = fakeApi({
    "GET /api/folders": (c) => ({ path: c.query.get("path") ?? "", entries: [{ name: `r${round}`, path: `r${round}`, has_children: false }] }),
  });
  const { result } = renderHook(() => useFolderTree());
  await waitFor(() => expect(result.current.contents.get("")).toEqual([expect.objectContaining({ name: "r1" })]));
  round = 2;
  act(() => result.current.reveal("projects/alpha")); // opening another folder asks nothing again of home
  await waitFor(() => expect(fake.made("GET", "/api/folders")).toHaveLength(2));
  expect(result.current.contents.get("")).toEqual([expect.objectContaining({ name: "r1" })]);

  act(() => result.current.reload(""));
  await waitFor(() => expect(result.current.contents.get("")).toEqual([expect.objectContaining({ name: "r2" })]));
  expect(fake.made("GET", "/api/folders").filter((c) => !c.query.get("path"))).toHaveLength(2);
});

test("forgetting a folder drops it and everything below it, and nothing else", async () => {
  const fake = fakeApi({ "GET /api/folders": (c) => ({ path: c.query.get("path") ?? "", entries: [] }) });
  const { result } = renderHook(() => useFolderTree());
  act(() => result.current.reveal("projects/alpha/deep/x"));
  act(() => result.current.reveal("projects/alpha2/x"));
  const known = ["", "projects", "projects/alpha", "projects/alpha/deep", "projects/alpha2"];
  await waitFor(() => expect([...result.current.contents.keys()].sort()).toEqual(known)); // positive control
  expect(fake.made("GET", "/api/folders")).toHaveLength(known.length);

  act(() => result.current.forget("projects/alpha"));
  expect([...result.current.contents.keys()].sort()).toEqual(["", "projects", "projects/alpha2"]);
  expect([...result.current.open].sort()).toEqual(["projects", "projects/alpha2"]);

  // forgotten, so a folder made there again under the same name is fetched afresh
  act(() => result.current.reveal("projects/alpha/x"));
  await waitFor(() =>
    expect(fake.made("GET", "/api/folders").filter((c) => c.query.get("path") === "projects/alpha")).toHaveLength(2),
  );
});
