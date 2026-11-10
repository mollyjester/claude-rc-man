import { readFrame } from "./terminalSocket";

test("rcman's frames are read, and anything else is unknown", () => {
  expect(readFrame(JSON.stringify({ type: "exit", code: 3 }))).toEqual({ type: "exit", code: 3 });
  expect(readFrame(JSON.stringify({ type: "exit" }))).toEqual({ type: "exit", code: null });
  expect(readFrame(JSON.stringify({ type: "closed", reason: "x" }))).toEqual({
    type: "closed",
    reason: "x",
  });
  const problem = { code: "c", title: "t", status: 404 };
  expect(readFrame(JSON.stringify({ type: "problem", problem }))).toEqual({
    type: "problem",
    problem,
  });
  for (const text of ["{x", "null", "[1]", '{"type":"closed"}', '{"type":"paste"}'])
    expect(readFrame(text), text).toEqual({ type: "unknown" });
});
