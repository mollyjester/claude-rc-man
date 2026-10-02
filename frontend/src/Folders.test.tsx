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
